"""Shared I/O and helpers for the INES <-> PyPSA transformations."""

import json
import math
import os
import re
import warnings
from collections import defaultdict

import numpy as np
import pandas as pd
import yaml
from spinedb_api import (
    Array,
    DatabaseMapping,
    DateTime,
    Duration,
    Map,
    TimeSeries,
    TimeSeriesVariableResolution,
    export_data,
    import_data,
)
from spinedb_api.parameter_value import from_database, from_dict

HERE = os.path.dirname(os.path.abspath(__file__))
INES_SPEC_FILE = os.path.join(HERE, "ines-spec.json")


def load_yaml(name):
    with open(os.path.join(HERE, name), "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_mapping():
    """All yaml companion files in one dictionary."""
    return {
        "entities": load_yaml("pypsa_to_ines_entities.yaml"),
        "parameters": load_yaml("pypsa_to_ines_parameters.yaml"),
        "methods": load_yaml("pypsa_to_ines_parameter_methods.yaml"),
        "relationships": load_yaml("pypsa_to_ines_parameters_to_relationships.yaml"),
        "settings": load_yaml("pypsa_to_ines_settings.yaml"),
    }


def is_url(path):
    return "://" in path


def ines_url(path):
    return path if is_url(path) else "sqlite:///" + os.path.abspath(path)


# ---------------------------------------------------------------- INES side

def _parse_json_value(v):
    if isinstance(v, dict):
        return from_dict(v)
    return v


def _prepare_import(data):
    data = dict(data)
    data.pop("parameter_types", None)
    if "parameter_values" in data:
        data["parameter_values"] = [
            [*row[:3], _parse_json_value(row[3]), *row[4:]] for row in data["parameter_values"]
        ]
    if "parameter_definitions" in data:
        data["parameter_definitions"] = [
            [row[0], row[1], _parse_json_value(row[2]) if len(row) > 2 else None, *row[3:]]
            for row in data["parameter_definitions"]
        ]
    return data


def open_ines_source(path):
    """DatabaseMapping for an INES sqlite file / url or an INES JSON export."""
    if not is_url(path) and path.lower().endswith(".json"):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        db = DatabaseMapping("sqlite://", create=True)
        _, errors = import_data(db, **_prepare_import(data))
        if errors:
            raise RuntimeError(f"Errors importing {path}: {errors[:5]}")
        db.commit_session("Imported INES json")
        return db
    return DatabaseMapping(ines_url(path))


class InesData:
    """INES entities and the parameter values of the selected alternatives.

    Every read through get() is recorded so that unused parameters can be reported.
    """

    def __init__(self, db, scenario=None, alternatives=None):
        self.alternative_order = self._alternative_order(db, scenario, alternatives)
        rank = {alt: i for i, alt in enumerate(self.alternative_order)}
        state = {}
        for ea in db.get_entity_alternative_items():
            if ea["alternative_name"] in rank:
                key = (ea["entity_class_name"], tuple(ea["entity_byname"]))
                prev = state.get(key)
                if prev is None or rank[ea["alternative_name"]] >= prev[0]:
                    state[key] = (rank[ea["alternative_name"]], bool(ea["active"]))
        self.entities = defaultdict(list)
        self.inactive = set()
        for item in db.get_entity_items():
            key = (item["entity_class_name"], tuple(item["entity_byname"]))
            if not state.get(key, (0, True))[1]:
                self.inactive.add(key)
                continue
            self.entities[item["entity_class_name"]].append(tuple(item["entity_byname"]))
        self.values = {}
        chosen = {}
        for pv in db.get_parameter_value_items():
            alt = pv["alternative_name"]
            if alt not in rank:
                continue
            key = (pv["entity_class_name"], tuple(pv["entity_byname"]), pv["parameter_definition_name"])
            if (key[0], key[1]) in self.inactive:
                continue
            if key in chosen and chosen[key] > rank[alt]:
                continue
            chosen[key] = rank[alt]
            self.values[key] = pv["parsed_value"]
        self.used = set()

    @staticmethod
    def _alternative_order(db, scenario, alternatives):
        if alternatives:
            return list(alternatives)
        scenarios = {s["name"] for s in db.get_scenario_items()}
        if scenario is None and len(scenarios) == 1:
            scenario = next(iter(scenarios))
        if scenario is not None:
            if scenario not in scenarios:
                raise ValueError(f"Scenario {scenario} not found. Available: {sorted(scenarios)}")
            sas = [sa for sa in db.get_scenario_alternative_items() if sa["scenario_name"] == scenario]
            return [sa["alternative_name"] for sa in sorted(sas, key=lambda sa: sa["rank"])]
        return [a["name"] for a in db.get_alternative_items()]

    @staticmethod
    def _key(cls, byname, param):
        if isinstance(byname, str):
            byname = (byname,)
        return (cls, tuple(byname), param)

    def get(self, cls, byname, param, default=None):
        key = self._key(cls, byname, param)
        if key in self.values:
            self.used.add(key)
            return self.values[key]
        return default

    def has(self, cls, byname, param):
        return self._key(cls, byname, param) in self.values

    def mark(self, cls, byname, param):
        key = self._key(cls, byname, param)
        if key in self.values:
            self.used.add(key)

    def mark_all(self, cls, byname):
        if isinstance(byname, str):
            byname = (byname,)
        for key in self.values:
            if key[0] == cls and key[1] == tuple(byname):
                self.used.add(key)

    def bynames(self, cls):
        return list(self.entities.get(cls, []))

    def names(self, cls):
        return [b[0] for b in self.entities.get(cls, [])]

    def unused(self):
        out = defaultdict(list)
        for key in self.values:
            if key not in self.used:
                out[(key[0], key[2])].append(key[1])
        return out


def write_ines(target, entities, values, alternative="Base", scenario="base", entity_alternatives=None):
    """Write INES data to a sqlite file / url or to a JSON file.

    entities: iterable of (class, byname tuple); values: iterable of (class, byname, param, value);
    entity_alternatives: iterable of (class, byname, active).
    """
    with open(INES_SPEC_FILE, "r", encoding="utf-8") as f:
        template = json.load(f)
    is_json = not is_url(target) and target.lower().endswith(".json")
    url = "sqlite://" if is_json else ines_url(target)
    with DatabaseMapping(url, create=True) as db:
        # merging the template also adds definitions missing from a database made with an older spec
        _, errors = import_data(db, **_prepare_import(template))
        if errors:
            raise RuntimeError(f"Errors importing the INES specification: {errors[:5]}")
        db.purge_items("parameter_value")
        db.purge_items("entity_alternative")
        db.purge_items("entity")
        db.purge_items("scenario")
        db.purge_items("alternative")
        data = {
            "alternatives": [[alternative, ""]],
            "scenarios": [[scenario, False, ""]],
            "scenario_alternatives": [[scenario, alternative, None]],
            "entities": [[cls, _byname_out(byname), None] for cls, byname in entities],
            "parameter_values": [[cls, _byname_out(byname), p, v, alternative] for cls, byname, p, v in values],
            "entity_alternatives": [[cls, _byname_out(byname), alternative, active]
                                    for cls, byname, active in (entity_alternatives or [])],
        }
        _, errors = import_data(db, **data)
        if errors:
            raise RuntimeError(f"Errors writing INES data: {errors[:10]}")
        db.commit_session("Imported from PyPSA")
        if is_json:
            exported = export_data(db, parse_value=from_database)
            exported.pop("parameter_types", None)
            for key in ("parameter_values", "parameter_definitions"):
                rows = []
                for row in exported.get(key, []):
                    row = list(row)
                    idx = 3 if key == "parameter_values" else 2
                    if idx < len(row):
                        row[idx] = jsonable(row[idx])
                    rows.append(row)
                exported[key] = rows
            with open(target, "w", encoding="utf-8") as f:
                json.dump(exported, f, indent=2, default=str)


def _byname_out(byname):
    return list(byname) if len(byname) > 1 else byname[0]


def jsonable(v):
    if hasattr(v, "to_dict"):
        d = v.to_dict()
        d["type"] = v.type_()
        return d
    if isinstance(v, (np.floating, np.integer)):
        return v.item()
    return v


# ---------------------------------------------------------------- PyPSA side

def is_pypsa_spine_db(path):
    return is_url(path) or path.lower().endswith((".sqlite", ".db"))


def load_network(source):
    """PyPSA network from a netcdf / hdf5 file, a csv folder or a Spine DB made by pypsa_to_spine.py."""
    import pypsa

    if os.path.isdir(source):
        n = pypsa.Network()
        n.import_from_csv_folder(source)
        return n
    if is_pypsa_spine_db(source):
        return network_from_spine_db(ines_url(source))
    return pypsa.Network(source)


def save_network(n, target):
    """Write a PyPSA network to .nc, .h5 or (otherwise) a csv folder."""
    lower = target.lower()
    if lower.endswith(".nc"):
        n.export_to_netcdf(target)
    elif lower.endswith((".h5", ".hdf5")):
        n.export_to_hdf5(target)
    else:
        n.export_to_csv_folder(target)


def network_from_spine_db(url, alternative=None):
    """Rebuild a PyPSA network from a Spine DB that holds PyPSA classes (see spine-pypsa/pypsa_to_spine.py)."""
    import pypsa

    n = pypsa.Network()
    comps = {name: n.components[name] for name in n.all_components}
    values = defaultdict(dict)
    entities = defaultdict(list)
    with DatabaseMapping(url) as db:
        for item in db.get_entity_items():
            entities[item["entity_class_name"]].append(item["name"])
        for pv in db.get_parameter_value_items():
            if alternative and pv["alternative_name"] != alternative:
                continue
            values[(pv["entity_class_name"], pv["entity_byname"][0])][pv["parameter_definition_name"]] = pv["parsed_value"]
    time = values.get(("Network", "Time"), {})
    snapshots = time.get("snapshots")
    if snapshots is not None:
        stamps = [pd.Timestamp(v.value if isinstance(v, DateTime) else v) for v in snapshots.values]
        n.set_snapshots(pd.DatetimeIndex(stamps))
    weightings = time.get("snapshot_weightings")
    if isinstance(weightings, Map):
        for col, sub in zip(weightings.indexes, weightings.values):
            if isinstance(sub, Map) and str(col) in n.snapshot_weightings.columns:
                s = pd.Series(list(sub.values), index=pd.to_datetime([str(i) for i in sub.indexes]))
                n.snapshot_weightings[str(col)] = s.reindex(n.snapshots).fillna(1.0).values
    periods = time.get("investment_periods")
    if isinstance(periods, Array) and len(periods.values):
        n.set_investment_periods([int(p) for p in periods.values])
    skip = ("Carrier", "Bus", "Network", "SubNetwork", "Shape", "LineType", "TransformerType")
    order = ["Carrier", "Bus"] + [c for c in comps if c not in skip]
    for comp in order:
        if comp not in entities:
            continue
        attrs = comps[comp].attrs
        inputs = set(attrs.index[attrs.status.str.startswith("Input")])
        for name in entities[comp]:
            kwargs = {}
            for attr, v in values.get((comp, name), {}).items():
                if attr == "name" or (attr not in inputs and not re.fullmatch(r"(bus|efficiency|rate|delay|cyclic_delay)\d+", attr)):
                    continue
                if isinstance(v, TimeSeries):
                    s = ts_to_series(v)
                    if n.has_investment_periods:
                        continue
                    kwargs[attr] = s.reindex(n.snapshots)
                elif v is not None:
                    kwargs[attr] = v
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                n.add(comp, name, **kwargs)
    return n


def component_attrs(n, comp):
    return n.components[comp].attrs


class PypsaData:
    """Read access to a PyPSA network that records which attributes were used."""

    STRUCTURAL = {"name", "bus", "bus0", "bus1", "carrier"}

    def __init__(self, n):
        self.n = n
        self.used = set()

    def static(self, c):
        return self.n.components[c].static

    def dynamic(self, c):
        return self.n.components[c].dynamic

    def names(self, c):
        return [str(x) for x in self.static(c).index]

    def default(self, c, attr):
        attrs = self.n.components[c].attrs
        if attr not in attrs.index:
            base = re.sub(r"\d+$", "", attr)
            if base == "bus":
                return ""
            if base == "rate":
                return 0.0
            if base in attrs.index:
                attr = base
            else:
                return None
        return attrs.at[attr, "default"]

    def raw(self, c, name, attr):
        dyn = self.dynamic(c)
        if attr in dyn and name in dyn[attr].columns:
            return dyn[attr][name].reindex(self.n.snapshots)
        st = self.static(c)
        if attr in st.columns:
            v = st.at[name, attr]
            return v.item() if isinstance(v, np.generic) else v
        return None

    def get(self, c, name, attr):
        self.used.add((c, name, attr))
        return self.raw(c, name, attr)

    def mark(self, c, name, *attrs):
        for attr in attrs:
            self.used.add((c, name, attr))

    def is_default(self, c, attr, v):
        if isinstance(v, pd.Series):
            d = self.default(c, attr)
            return d is not None and is_number(d) and bool(np.allclose(v.astype(float).values, float(d), equal_nan=True))
        if v is None or isnull(v):
            return True
        d = self.default(c, attr)
        if d is None or (not isinstance(d, str) and isnull(d)):
            return v == "" if isinstance(v, str) else False
        try:
            return bool(v == d)
        except (TypeError, ValueError):
            return False

    def value(self, c, name, attr):
        """Attribute value or None when it is the PyPSA default."""
        v = self.get(c, name, attr)
        return None if self.is_default(c, attr, v) else v

    def input_attrs(self, c):
        attrs = self.n.components[c].attrs
        out = [a for a in attrs.index[attrs.status.str.startswith("Input")] if a != "name"]
        extra = set(self.static(c).columns) | set(self.dynamic(c).keys())
        out += sorted(a for a in extra if re.fullmatch(r"(bus|efficiency|rate|delay|cyclic_delay)\d+", a) and a not in out)
        return out

    def unused(self, components):
        out = defaultdict(list)
        for c in components:
            if c not in self.n.all_components or self.static(c).empty:
                continue
            for attr in self.input_attrs(c):
                for name in self.names(c):
                    if (c, name, attr) in self.used:
                        continue
                    if not self.is_default(c, attr, self.raw(c, name, attr)):
                        out[(c, attr)].append(name)
        return out


# ------------------------------------------------------------ value helpers

def isnull(v):
    if v is None:
        return True
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def is_number(v):
    return isinstance(v, (int, float, np.integer, np.floating)) and not isinstance(v, bool)


def finite(v):
    return is_number(v) and not isnull(v) and math.isfinite(v)


def duration_hours(value):
    """Hours of a spinedb Duration / relativedelta / string like '1h', '15m', '1D'."""
    if value is None:
        return None
    if is_number(value):
        return float(value)
    if isinstance(value, Duration):
        value = value.value
    if isinstance(value, str):
        m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhDMY])\s*", value)
        if not m:
            raise ValueError(f"Cannot parse duration {value}")
        num, unit = float(m.group(1)), m.group(2)
        return num * {"s": 1 / 3600, "m": 1 / 60, "h": 1, "D": 24, "M": 730, "Y": 8760}[unit]
    rd = value
    return (
        (getattr(rd, "years", 0) or 0) * 8760
        + (getattr(rd, "months", 0) or 0) * 730
        + (getattr(rd, "days", 0) or 0) * 24
        + (getattr(rd, "hours", 0) or 0)
        + (getattr(rd, "minutes", 0) or 0) / 60
        + (getattr(rd, "seconds", 0) or 0) / 3600
    )


def hours_to_duration(hours):
    if abs(hours - round(hours)) < 1e-9:
        return Duration(f"{int(round(hours))}h")
    return Duration(f"{int(round(hours * 60))}m")


def to_timestamp(value):
    if value is None:
        return None
    if isinstance(value, DateTime):
        value = value.value
    return pd.Timestamp(value)


def ts_to_series(ts):
    idx = pd.DatetimeIndex(pd.to_datetime(np.asarray(ts.indexes)))
    return pd.Series(np.asarray(ts.values, dtype=float), index=idx).sort_index()


def map_to_dict(value):
    return {str(i): v for i, v in zip(value.indexes, value.values)}


def value_list(value):
    """INES Array / Map / str as a list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Array):
        return [str(v) for v in value.values]
    if isinstance(value, Map):
        out = []
        for v in value.values:
            for x in value_list(v):
                if x not in out:
                    out.append(x)
        return out
    return [str(value)]


def annuity_factor(rate, lifetime):
    if not finite(lifetime) or lifetime <= 0:
        return rate if rate > 0 else 0.0
    if rate == 0:
        return 1.0 / lifetime
    return rate / (1.0 - (1.0 + rate) ** -lifetime)


# ---------------------------------------------------------------- reporting

class Report:
    def __init__(self, title, reasons):
        self.title = title
        self.reasons = reasons or {}
        self.not_transformed = {}
        self.notes = []

    def note(self, text):
        if text not in self.notes:
            self.notes.append(text)

    def skip(self, name, entities, reason=None):
        entry = self.not_transformed.setdefault(name, {"entities": [], "reason": reason})
        for e in entities:
            if e not in entry["entities"]:
                entry["entities"].append(e)
        if reason and not entry["reason"]:
            entry["reason"] = reason

    def reason_for(self, name):
        if name in self.reasons:
            return self.reasons[name]
        cls, _, param = name.partition(".")
        if cls in ("unit__to_node", "node__to_unit") and f"unit_flow.{param}" in self.reasons:
            return self.reasons[f"unit_flow.{param}"]
        if f"*.{param}" in self.reasons:
            return self.reasons[f"*.{param}"]
        return self.reasons.get(cls, "")

    def to_markdown(self):
        lines = [f"# {self.title}", "", "## Not transformed", ""]
        if not self.not_transformed:
            lines.append("Everything in the input was transformed.")
        else:
            lines.append("| Parameter | Count | Example entities | Reason |")
            lines.append("|---|---|---|---|")
            for name in sorted(self.not_transformed):
                entry = self.not_transformed[name]
                ents = entry["entities"]
                example = ", ".join(_fmt_entity(e) for e in ents[:3]) + (" ..." if len(ents) > 3 else "")
                reason = entry["reason"] or self.reason_for(name)
                lines.append(f"| {name} | {len(ents)} | {example} | {reason} |")
        lines += ["", "## Notes on approximations", ""]
        lines.extend([f"- {n}" for n in self.notes] or ["None."])
        lines.append("")
        return "\n".join(lines)

    def write(self, path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.to_markdown())

    def print_summary(self):
        print(f"{len(self.not_transformed)} parameter(s) not transformed:")
        for name in sorted(self.not_transformed):
            print(f"  {name} ({len(self.not_transformed[name]['entities'])})")
        if self.notes:
            print(f"{len(self.notes)} note(s), see the report file.")


def _fmt_entity(e):
    if isinstance(e, tuple):
        return "/".join(str(x) for x in e)
    return str(e)
