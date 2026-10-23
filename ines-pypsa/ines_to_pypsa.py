"""Transform an INES model (Spine DB sqlite or JSON) into a PyPSA network.

Usage:
    python ines_to_pypsa.py INPUT OUTPUT [--scenario NAME | --alternative ALT ...] [--report REPORT.md]

INPUT  INES sqlite file, sqlite url (e.g. db_url@ines_db in Spine Toolbox) or INES JSON export.
OUTPUT PyPSA netcdf (.nc), hdf5 (.h5) file or (any other path) a folder of PyPSA csv files.

The mapping is in the yaml files next to this script; settings in pypsa_to_ines_settings.yaml.
"""

import argparse
import math
import os
import sys
import warnings
from collections import Counter, defaultdict

import numpy as np
import pandas as pd
from spinedb_api import Array, Map, TimeSeries

from ines_pypsa_common import (
    InesData,
    Report,
    annuity_factor,
    duration_hours,
    finite,
    is_number,
    isnull,
    load_mapping,
    map_to_dict,
    open_ines_source,
    save_network,
    to_timestamp,
    ts_to_series,
    value_list,
)

ORDER = ["Carrier", "Bus", "Load", "Generator", "Link", "Store", "StorageUnit", "GlobalConstraint"]
STORE_STATE = {"cyclic": "e_cyclic", "cyclic_per_period": "e_cyclic_per_period", "initial": "e_initial", "set": "e_set"}
SU_STATE = {"cyclic": "cyclic_state_of_charge", "cyclic_per_period": "cyclic_state_of_charge_per_period",
            "initial": "state_of_charge_initial", "set": "state_of_charge_set"}
CARRIER_SET_PARAMS = {"invest_max_period"}


class InesToPypsa:
    def __init__(self, ines, mapping, report):
        self.i = ines
        self.m = mapping
        self.s = mapping["settings"]
        self.cfg = mapping["entities"].get("ines_to_pypsa", {})
        self.methods = mapping["methods"]
        self.report = report
        self.comps = {c: {} for c in ORDER}
        self.direct = defaultdict(list)
        for row in mapping["parameters"]["direct_parameters"]:
            if row.get("reverse", True):
                self.direct[row["component"]].append(row)
        self.big = float(self.s.get("Unlimited_capacity", 1e6))
        self.carrier_co2 = {}
        self.commodity_buses = {}
        self.co2_price = {}

    # ------------------------------------------------------------ helpers
    def g(self, cls, byname, param, default=None):
        return self.i.get(cls, byname, param, default)

    def add(self, comp, name, **attrs):
        base, i = name, 2
        while name in self.comps[comp]:
            name, i = f"{base}_{i}", i + 1
        self.comps[comp][name] = {k: v for k, v in attrs.items() if v is not None}
        return name

    def series(self, v, what=None):
        """INES value -> scalar or pandas Series over the snapshots."""
        if v is None:
            return None
        if isinstance(v, TimeSeries):
            return self._bin(ts_to_series(v), what)
        if isinstance(v, Map):
            d = map_to_dict(v)
            if any(p in d for p in self.period_names):
                vals = [self.scalar(d.get(p, next(iter(d.values()))), what) for p in self.period_names]
                if len(self.period_names) == 1 or len(set(vals)) == 1:
                    return vals[0]
                return pd.Series(np.repeat(vals, self.period_lengths), index=self.snapshots)
            try:
                s = pd.Series([float(x) for x in v.values], index=pd.to_datetime([str(x) for x in v.indexes]))
                return self._bin(s.sort_index(), what)
            except (TypeError, ValueError):
                return self.scalar(v, what)
        return self.scalar(v, what)

    def scalar(self, v, what=None):
        """INES value -> one number (first period of a period map, mean of a time series)."""
        if v is None or isinstance(v, (str, bool)):
            return v
        if is_number(v):
            return float(v)
        if isinstance(v, pd.Series):
            if what and not np.allclose(v.values, v.values[0]):
                self.report.note(f"{what}: time series averaged because PyPSA needs a constant there.")
            return float(v.mean())
        if isinstance(v, TimeSeries):
            if what:
                self.report.note(f"{what}: time series averaged because PyPSA needs a constant there.")
            return float(np.nanmean(np.asarray(v.values, dtype=float)))
        if isinstance(v, Map):
            d = map_to_dict(v)
            for p in self.period_names:
                if p in d:
                    vals = {json_key(d[q]) for q in self.period_names if q in d}
                    if len(vals) > 1 and what:
                        self.report.note(f"{what}: values differ between periods; the first period value is used.")
                    return self.scalar(d[p], what)
            points = operating_points(v)
            if points:
                if what:
                    self.report.note(f"{what}: map over operating points; the full-load value is used.")
                return points[-1][1]
            return self.scalar(v.values[0], what) if len(v.values) else None
        if isinstance(v, Array):
            return self.scalar(v.values[0], what) if len(v.values) else None
        return v

    def _bin(self, s, what=None):
        times = self.snap_times
        pos = np.searchsorted(times.values, s.index.values, side="right") - 1
        ends = self.snap_ends.values
        valid = (pos >= 0) & (s.index.values < ends[np.clip(pos, 0, None)])
        if not valid.any():
            if what:
                self.report.note(f"{what}: time series does not overlap the modelled time; its values were used in order.")
            vals = np.resize(s.values.astype(float), len(times))
            return pd.Series(vals, index=self.snapshots)
        binned = pd.Series(s.values[valid].astype(float)).groupby(pos[valid]).mean()
        out = binned.reindex(range(len(times))).ffill().bfill().values
        return pd.Series(out, index=self.snapshots)

    @staticmethod
    def is_const(v):
        return v is None or is_number(v)

    def mul(self, a, b):
        if a is None or b is None:
            return None
        return a * b

    def factor(self, f):
        if f is None:
            return 1.0
        if f == "minutes":
            return self.step_minutes
        if f == "per_minute":
            return 1.0 / self.step_minutes
        return float(f)

    def apply_direct(self, comp, attrs, roles):
        cattrs = self.attr_table(comp)
        for ent in roles.values():
            if ent[0] in ("unit__to_node", "node__to_unit", "node__link__node"):
                self.i.mark(ent[0], ent[1], "ramp_method")
        for row in self.direct[comp]:
            ent = roles.get(row["role"])
            if ent is None:
                continue
            v = self.g(ent[0], ent[1], row["ines"])
            if v is None:
                continue
            f = 1.0 / self.factor(row.get("factor"))
            varying = row["pypsa"] in cattrs.index and bool(cattrs.at[row["pypsa"], "varying"])
            val = self.series(v, f"{ent[0]} {ent[1]} {row['ines']}") if varying else self.scalar(v, f"{ent[0]} {ent[1]} {row['ines']}")
            if val is None or isinstance(val, str):
                continue
            val = val * f
            if row["pypsa"] in cattrs.index and str(cattrs.at[row["pypsa"], "type"]) == "int":
                val = int(math.ceil(float(val) - 1e-9))
            attrs[row["pypsa"]] = val

    def attr_table(self, comp):
        if not hasattr(self, "_attr_tables"):
            import pypsa

            n = pypsa.Network()
            self._attr_tables = {c: n.components[c].attrs for c in ORDER}
        return self._attr_tables[comp]

    # ------------------------------------------------------------ main
    def run(self):
        self.build_time()
        self.classify_nodes()
        self.read_sets()
        self.build_buses()
        self.build_storages()
        self.build_storage_units()
        self.build_units()
        self.build_links()
        self.build_global_constraints()
        self.build_carriers()
        self.report_unused()
        return self.make_network()

    # ------------------------------------------------------------ time
    def build_time(self):
        sps = self.i.names("solve_pattern")
        sp = self.s.get("Ines_solve_pattern") or (sps[0] if sps else None)
        if len(sps) > 1:
            self.report.note(f"Several solve_patterns; {sp} was used for the time structure.")
        self.sp = sp
        all_periods = self.i.names("period")
        periods = [p for p in value_list(self.g("solve_pattern", sp, "period")) if p in all_periods] if sp else []
        periods = periods or all_periods
        timeline = None
        for sysname in self.i.names("system"):
            v = self.g("system", sysname, "timeline")
            if isinstance(v, TimeSeries):
                timeline = pd.DatetimeIndex(pd.to_datetime(np.asarray(v.indexes))).sort_values()
                break
        res = duration_hours(self.g("solve_pattern", sp, "time_resolution")) if sp else None
        sp_start = to_timestamp(self.g("solve_pattern", sp, "start_time")) if sp else None
        sp_dur = duration_hours(self.g("solve_pattern", sp, "duration")) if sp else None
        if timeline is None and res is None:
            raise ValueError("INES data has neither a system timeline nor a solve_pattern time_resolution.")
        if timeline is not None and len(timeline) > 1:
            step = Counter(np.round(np.asarray((timeline[1:] - timeline[:-1]).total_seconds()) / 3600.0, 9)).most_common(1)[0][0]
        else:
            step = res or 1.0
        res = max(res or step, step)
        windows = []
        for p in periods or [None]:
            start = to_timestamp(self.g("period", p, "start_time")) if p else None
            dur = duration_hours(self.g("period", p, "duration")) if p else None
            start = start or sp_start or (timeline[0] if timeline is not None else None)
            if dur is None:
                dur = sp_dur
            if dur is None:
                dur = (timeline[-1] - start).total_seconds() / 3600.0 + step if timeline is not None else 8760.0
            if sp_start is not None and sp_dur is not None and len(periods) <= 1:
                start, dur = max(start, sp_start), min(dur, sp_dur)
            windows.append((p, start, dur))
        stamps, lengths, names = [], [], []
        for p, start, dur in windows:
            end = start + pd.Timedelta(hours=dur)
            if timeline is not None:
                ts = timeline[(timeline >= start) & (timeline < end)]
                if res > step + 1e-9:
                    offs = np.asarray((ts - start).total_seconds()) / 3600.0
                    ts = ts[np.isclose(np.mod(offs, res), 0) | np.isclose(np.mod(offs, res), res)]
            else:
                ts = pd.date_range(start, end, freq=pd.Timedelta(hours=res), inclusive="left")
            if len(ts) == 0:
                raise ValueError(f"Period {p}: no timeline steps between {start} and {end}.")
            stamps.extend(ts)
            lengths.append(len(ts))
            names.append(p if p is not None else str(start.year))
        self.period_names = names
        self.period_lengths = lengths
        self.snap_times = pd.DatetimeIndex(stamps)
        weights = []
        for k, (p, start, dur) in enumerate(windows):
            ts = self.snap_times[sum(lengths[:k]):sum(lengths[:k + 1])]
            end = start + pd.Timedelta(hours=dur)
            nxt = list(ts[1:]) + [end]
            weights.extend([(b - a).total_seconds() / 3600.0 for a, b in zip(ts, nxt)])
        self.weights = np.array(weights)
        self.store_weights = self.weights.copy()
        if self.s.get("Annualize_periods", True):
            for k in range(len(lengths)):
                seg = slice(sum(lengths[:k]), sum(lengths[:k + 1]))
                hours = self.weights[seg].sum()
                if hours > 0 and abs(hours - 8760.0) > 1e-6:
                    self.weights[seg] *= 8760.0 / hours
                    self.report.note(f"Objective and generator snapshot weightings scale each period to a year (Annualize_periods); store weightings keep the time step lengths.")
        self.snap_ends = self.snap_times + pd.to_timedelta(self.store_weights, unit="h")
        self.step_hours = res
        self.step_minutes = res * 60.0
        if timeline is not None and res > step + 1e-9:
            self.report.note(f"Time series were averaged from the {step} h timeline to the {res} h time_resolution.")
        for p in periods:
            self.i.mark("period", p, "start_time")
            self.i.mark("period", p, "duration")
        for name in ("period", "time_resolution", "start_time", "duration"):
            if sp:
                self.i.mark("solve_pattern", sp, name)
        if sp and self.g("solve_pattern", sp, "solve_mode") not in (None, "single_solve"):
            self.i.used.discard(("solve_pattern", (sp,), "solve_mode"))
        for sysname in self.i.names("system"):
            self.i.mark("system", sysname, "timeline")
        self.multi = len(names) > 1
        if self.multi:
            years = []
            for p, start, _ in windows:
                years.append(int(p) if str(p).isdigit() else start.year)
            if len(set(years)) != len(years) or years != sorted(years):
                years = [windows[0][1].year + 10 * k for k in range(len(windows))]
                self.report.note(f"Period names are not increasing years; investment periods named {years}.")
            self.years = years
            self.snapshots = pd.MultiIndex.from_arrays([np.repeat(years, lengths), self.snap_times], names=["period", "timestep"])
            represented = []
            for k, p in enumerate(periods):
                v = self.scalar(self.g("period", p, "years_represented"))
                if v is None:
                    v = float(years[k + 1] - years[k]) if k + 1 < len(years) else 1.0
                represented.append(float(v))
            rate = None
            for sysname in self.i.names("system"):
                rate = self.scalar(self.g("system", sysname, "discount_rate"), f"system {sysname} discount_rate")
                if rate is not None:
                    break
            rate = rate or 0.0
            objective = [sum((1 + rate) ** -(years[k] - years[0] + j) for j in range(int(round(represented[k]))))
                         for k in range(len(years))]
            self.period_weightings = pd.DataFrame({"objective": objective, "years": represented}, index=pd.Index(years, name="period"))
        else:
            self.years = None
            self.snapshots = self.snap_times
            for p in periods:
                self.i.mark("period", p, "years_represented")
            for sysname in self.i.names("system"):
                self.i.mark("system", sysname, "discount_rate")
        self.total_hours = float(self.weights.sum())
        per_period = [float(self.weights[sum(lengths[:k]):sum(lengths[:k + 1])].sum()) / 8760.0 for k in range(len(lengths))]
        self.equal_nyears = bool(np.allclose(per_period, per_period[0]))
        self.nyears = float(np.mean(per_period))
        if not self.equal_nyears:
            self.report.note("Periods model different numbers of hours; investment costs written as capital_cost scaled with the mean modelled years.")

    # ------------------------------------------------------------ classification
    def classify_nodes(self):
        self.node_kind = {}
        for nd in self.i.names("node"):
            nt = self.g("node", nd, "node_type")
            if nt == "commodity":
                kind = "commodity"
            elif nt == "storage" or (nt is None and self.i.has("node", nd, "storage_capacity")):
                kind = "storage"
            else:
                kind = "balance"
                if nt not in (None, "balance"):
                    self.report.note(f"Node {nd}: node_type {nt} treated as a balance node.")
            self.node_kind[nd] = kind
        self.unit_in = defaultdict(list)
        self.unit_out = defaultdict(list)
        for nd, u in self.i.bynames("node__to_unit"):
            self.unit_in[u].append(nd)
        for u, nd in self.i.bynames("unit__to_node"):
            self.unit_out[u].append(nd)
        self.ratios = {}
        for byname in self.i.bynames("unit_flow__unit_flow"):
            r = self.g("unit_flow__unit_flow", byname, "equality_ratio")
            if r is not None:
                self.ratios[(tuple(byname[:2]), tuple(byname[2:]))] = r
        self.find_storage_units()

    def flow_cls(self, a, b):
        return "node__to_unit" if a in self.node_kind else "unit__to_node"

    def ratio(self, fa, fb):
        """r with flow fa = r * flow fb from unit_flow__unit_flow equality_ratio, else None."""
        if (fa, fb) in self.ratios:
            return self.series(self.ratios[(fa, fb)])
        if (fb, fa) in self.ratios:
            r = self.series(self.ratios[(fb, fa)])
            return 1.0 / r
        return None

    def read_sets(self):
        self.carrier_of = {}
        self.co2_sets = []
        members = defaultdict(list)
        for cls in ("set__node", "set__unit", "set__link"):
            for s, x in self.i.bynames(cls):
                members[s].append((cls[5:], x))
        self.set_members = members
        for s in self.i.names("set"):
            params = {k[2] for k in self.i.values if k[0] == "set" and k[1] == (s,)}
            if params <= CARRIER_SET_PARAMS:
                for kind, x in members[s]:
                    self.carrier_of.setdefault((kind, x), s)
            if params & {"co2_max_cumulative", "co2_max_period", "co2_price"}:
                self.co2_sets.append(s)
                price = self.g("set", s, "co2_price")
                if price is not None:
                    for kind, x in members[s]:
                        if kind == "node":
                            self.co2_price[x] = self.series(price, f"set {s} co2_price")

    # ------------------------------------------------------------ buses, loads, penalties
    def bus_carrier(self, nd):
        carriers = Counter()
        for cls in ("unit__to_node", "node__to_unit", "node__link__node"):
            for byname in self.i.bynames(cls):
                if nd in (byname[0], byname[-1]):
                    c = self.g(cls, byname, "carrier")
                    if c:
                        carriers[c] += 1
        if carriers:
            return carriers.most_common(1)[0][0]
        return self.carrier_of.get(("node", nd)) or self.s.get("Default_bus_carrier", "AC")

    def build_buses(self):
        suffix_up = self.cfg.get("penalty_up_suffix", "_penalty_up")
        suffix_down = self.cfg.get("penalty_down_suffix", "_penalty_down")
        for nd, kind in self.node_kind.items():
            self.i.mark("node", nd, "node_type")
            if kind in ("commodity", "storage_unit"):
                continue
            self.add("Bus", nd, carrier=self.bus_carrier(nd))
            self.load(nd)
            spill_up = self.g("node", nd, "spill_upper_limit")
            if spill_up is not None:
                spill_lo = self.g("node", nd, "spill_lower_limit")
                self.add("Generator", nd + self.cfg.get("spill_suffix", "_spill"), bus=nd, carrier="spill", p_nom=1.0,
                         p_min_pu=-self.series(spill_up, f"node {nd} spill_upper_limit"),
                         p_max_pu=-self.series(spill_lo, f"node {nd} spill_lower_limit") if spill_lo is not None else 0.0)
            if self.s.get("Add_penalty_generators", True):
                up = self.g("node", nd, "penalty_upward")
                down = self.g("node", nd, "penalty_downward")
                if up is not None:
                    self.add("Generator", nd + suffix_up, bus=nd, carrier="penalty", p_nom=self.big,
                             marginal_cost=self.series(up, f"node {nd} penalty_upward"))
                if down is not None:
                    self.add("Generator", nd + suffix_down, bus=nd, carrier="penalty", p_nom=self.big, p_min_pu=-1.0,
                             p_max_pu=0.0, marginal_cost=-self.series(down, f"node {nd} penalty_downward"))

    def load(self, nd):
        profile = self.g("node", nd, "flow_profile")
        method = self.g("node", nd, "flow_scaling_method")
        if profile is None:
            return
        if method == "no_inflow":
            self.i.mark("node", nd, "flow_profile")
            return
        p = self.series(profile, f"node {nd} flow_profile")
        if method == "scale_to_annual":
            annual = self.scalar(self.g("node", nd, "flow_annual"), f"node {nd} flow_annual")
            if annual is not None:
                total = float((p * self.weights).sum()) if isinstance(p, pd.Series) else p * self.total_hours
                if total:
                    p = p * (annual * self.total_hours / 8760.0 / (total / max(len(self.period_names), 1)))
                    self.report.note(f"Node {nd}: flow_profile scaled to flow_annual (pro rata for the modelled hours).")
        if isinstance(p, pd.Series) and np.allclose(p.values, 0) or (is_number(p) and p == 0):
            return
        if self.node_kind[nd] == "storage" and self.i.has("node", nd, "spill_upper_limit"):
            self.report.note(f"Node {nd}: inflow to a storage node became a Load with negative p_set; spill is not modelled.")
        self.add("Load", nd, bus=nd, p_set=-p)

    # ------------------------------------------------------------ investments
    def capacity_info(self, cls, ent, cap, kind):
        """Capacity and investment options of an INES unit / link / storage node (cap in MW or MWh)."""
        pre = {"unit": "units", "link": "links", "storage": "storages"}[kind]
        method_p = "storage_investment_method" if kind == "storage" else "investment_method"
        integer_p = "storage_investment_uses_integer" if kind == "storage" else "investment_uses_integer"
        what = f"{cls} {ent[0] if len(ent) == 1 else ent}"
        method = self.g(cls, ent, method_p)
        ext = method is not None and self.methods["extendable"]["to_pypsa"].get(method, self.methods["extendable"]["to_pypsa"]["default"])
        existing = self.scalar(self.g(cls, ent, f"{pre}_existing"), f"{what} {pre}_existing")
        if existing is None:
            existing = 0.0 if ext else float(self.s.get("Default_units_existing", 1)) if cap is not None else 0.0
        if cap is None:
            cap = float(self.s.get("Default_module_capacity", 100)) if ext else self.big
            if not ext:
                existing = 1.0
                self.report.note(f"{what}: no capacity in INES; PyPSA gets {self.big} (Unlimited_capacity).")
        info = {"cap": cap, "existing": existing, "ext": bool(ext), "mod": None, "max": None, "min": None, "set": None}
        for key in ("max", "min", "fix"):
            v = self.scalar(self.g(cls, ent, f"{pre}_{key}_cumulative"), f"{what} {pre}_{key}_cumulative")
            if v is not None:
                info["set" if key == "fix" else key] = v * cap
        inv_max = self.g(cls, ent, f"{pre}_invest_max_period")
        if inv_max is not None and info["max"] is None:
            vals = [x for x in (map_to_dict(inv_max).values() if isinstance(inv_max, Map) else [self.scalar(inv_max)]) if is_number(x)]
            if vals:
                info["max"] = (existing + sum(vals)) * cap
                if len(vals) > 1:
                    self.report.note(f"{what}: {pre}_invest_max_period summed over the periods into one p_nom_max.")
        if self.g(cls, ent, integer_p):
            info["mod"] = cap
        if method in ("fix_investments",) and info["set"] is None:
            self.report.note(f"{what}: investment_method fix_investments is approximated by a free investment.")
        return info

    def investment_attrs(self, cls, ent, cost_ents, storage=False, cost_scale=1.0):
        """overnight_cost / capital_cost, discount_rate, lifetime, fom_cost, build_year."""
        pre = "storage_" if storage else ""
        what = f"{cls} {ent[0] if len(ent) == 1 else ent}"
        attrs = {}
        inv = fixed = None
        for ce, scale in cost_ents:
            v = self.scalar(self.g(ce[0], ce[1], f"{pre}investment_cost"), f"{what} investment_cost")
            if v is not None:
                inv = (inv or 0.0) + v * scale
            f = self.scalar(self.g(ce[0], ce[1], f"{pre}fixed_cost"), f"{what} fixed_cost")
            if f is not None:
                fixed = (fixed or 0.0) + f * scale
        rate = self.scalar(self.g(cls, ent, f"{pre}interest_rate"), f"{what} interest_rate")
        lifetime = self.scalar(self.g(cls, ent, f"{pre}lifetime"), f"{what} lifetime")
        self.i.mark(cls, ent, f"{pre}retirement_method")
        if lifetime is not None:
            attrs["lifetime"] = float(lifetime)
        if inv is not None:
            rate = float(self.s.get("Interest_rate", 0.05)) if rate is None else rate
            if self.s.get("Use_overnight_cost", True) and self.equal_nyears:
                attrs["overnight_cost"] = inv * cost_scale
                attrs["discount_rate"] = rate
            else:
                attrs["capital_cost"] = inv * cost_scale * annuity_factor(rate, lifetime if lifetime else float("inf")) * self.nyears
        if fixed is not None:
            attrs["fom_cost"] = fixed * cost_scale * self.nyears
        commission = to_timestamp(self.g(cls, ent, f"{pre}commission_date"))
        if commission is not None:
            attrs["build_year"] = int(commission.year)
        return attrs

    def add_asset(self, comp, name, attrs, info, nom="p_nom", inv_attrs=None):
        """Add a component; an asset with existing units and investments may become two components."""
        inv_attrs = inv_attrs or {}
        existing = info["existing"] * info["cap"]
        cost_keys = {"overnight_cost", "discount_rate", "capital_cost"}
        if not info["ext"]:
            return [self.add(comp, name, **dict(attrs, **inv_attrs, **{nom: existing}))]
        ext_attrs = {f"{nom}_extendable": True, f"{nom}_max": info["max"], f"{nom}_min": info["min"],
                     f"{nom}_set": info["set"], f"{nom}_mod": info["mod"]}
        if existing > 0 and self.s.get("Split_existing_and_new", True):
            old = dict(attrs, **{k: v for k, v in inv_attrs.items() if k not in cost_keys}, **{nom: existing})
            new = dict(attrs, **inv_attrs, **ext_attrs)
            for key in (f"{nom}_max", f"{nom}_min", f"{nom}_set"):
                if new.get(key) is not None:
                    new[key] = max(new[key] - existing, 0.0)
            suffix = self.cfg.get("new_capacity_suffix", "_new")
            self.report.note(f"{comp} {name}: existing units and investments became {name} (fixed) and {name}{suffix} (extendable).")
            return [self.add(comp, name, **old), self.add(comp, name + suffix, **new)]
        if existing > 0:
            ext_attrs[f"{nom}_min"] = max(ext_attrs[f"{nom}_min"] or 0.0, existing)
            self.report.note(f"{comp} {name}: existing capacity became the lower bound of the extendable capacity (and is charged investment cost).")
        return [self.add(comp, name, **attrs, **inv_attrs, **ext_attrs)]

    def commitment(self, u, attrs):
        method = self.g("unit", u, "startup_method")
        to_pypsa = self.methods["committable"]["to_pypsa"]
        if method is not None and to_pypsa.get(method, to_pypsa["default"]):
            attrs["committable"] = True
            if method not in ("integer",):
                self.report.note(f"Unit {u}: startup_method {method} became committable (binary); use linearized_unit_commitment in PyPSA for a linear relaxation.")

    def profile_limits(self, flow, cap_ent, unit, attrs, flow_cls):
        """p_max_pu / p_min_pu from the profiles of a flow and the unit availability."""
        what = f"{flow_cls} {flow}"
        method = self.g(flow_cls, flow, "profile_method")
        up = self.g(flow_cls, flow, "profile_limit_upper")
        lo = self.g(flow_cls, flow, "profile_limit_lower")
        fix = self.g(flow_cls, flow, "profile_fix")
        avail = self.series(self.g("unit", unit, "availability"), f"unit {unit} availability") if unit else None
        use_up = method in (None, "upper_limit", "upper_and_lower_limit")
        use_lo = method in (None, "lower_limit", "upper_and_lower_limit")
        pmax = pmin = None
        if fix is not None and method in (None, "fixed"):
            pmax = pmin = self.series(fix, what + " profile_fix")
        else:
            if up is not None and use_up:
                pmax = self.series(up, what + " profile_limit_upper")
            if lo is not None and use_lo:
                pmin = self.series(lo, what + " profile_limit_lower")
        if avail is not None:
            pmax = avail if pmax is None else pmax * avail
        if pmax is not None:
            attrs["p_max_pu"] = pmax
        if pmin is not None:
            attrs["p_min_pu"] = pmin

    def efficiency(self, u):
        method = self.g("unit", u, "conversion_method")
        eff = self.g("unit", u, "efficiency")
        if eff is None:
            return 1.0
        if method not in (None, "constant_efficiency"):
            self.report.note(f"Unit {u}: conversion_method {method} approximated with a constant efficiency.")
        if isinstance(eff, Map) and operating_points(eff):
            self.report.note(f"Unit {u}: efficiency over operating points; PyPSA uses the full-load efficiency.")
            return operating_points(eff)[-1][1]
        return self.series(eff, f"unit {u} efficiency")

    # ------------------------------------------------------------ storages
    def build_storages(self):
        for nd, kind in self.node_kind.items():
            if kind != "storage":
                continue
            ent = (nd,)
            what = f"node {nd}"
            cap = self.scalar(self.g("node", ent, "storage_capacity"), what + " storage_capacity")
            info = self.capacity_info("node", ent, cap, "storage")
            attrs = {"bus": nd, "carrier": self.comps["Bus"][nd].get("carrier")}
            self.apply_direct("Store", attrs, {"storage": ("node", ent)})
            method = self.g("node", ent, "storage_limit_method")
            up = self.g("node", ent, "storage_state_upper_limit")
            lo = self.g("node", ent, "storage_state_lower_limit")
            if up is not None and method in (None, "upper_limit", "upper_and_lower_limit"):
                attrs["e_max_pu"] = self.series(up, what + " storage_state_upper_limit")
            if lo is not None and method in (None, "lower_limit", "upper_and_lower_limit"):
                attrs["e_min_pu"] = self.series(lo, what + " storage_state_lower_limit")
            energy = info["existing"] * info["cap"]
            self.storage_state_attrs(nd, attrs, energy, STORE_STATE)
            inv = self.investment_attrs("node", ent, [(("node", ent), 1.0)], storage=True)
            self.add_asset("Store", nd, attrs, info, nom="e_nom", inv_attrs=inv)

    def storage_state_attrs(self, nd, attrs, energy, names):
        """Cycling and fixed states of an INES storage node; names maps to Store or StorageUnit attributes."""
        ent = (nd,)
        what = f"node {nd}"
        binding = self.g("node", ent, "storage_state_binding_method")
        cyc = self.methods["storage_cycling"]["to_pypsa"]
        mode = cyc.get(binding, cyc["default"]) if binding else cyc["default"]
        if mode == "cyclic":
            attrs[names["cyclic"]] = True
        elif mode == "cyclic_per_period":
            attrs[names["cyclic_per_period"]] = True
            if not self.multi:
                attrs[names["cyclic"]] = True
        if binding in ("fill_with_representatives", "fill_with_interlaced_representatives"):
            self.report.note(f"{what}: storage_state_binding_method {binding} approximated by a cyclic storage.")
        fix = self.g("node", ent, "storage_state_fix")
        fix_method = self.g("node", ent, "storage_state_fix_method")
        if fix is not None and fix_method in ("fix_start", "fix_start_and_horizon_end"):
            f = self.scalar(fix, what + " storage_state_fix")
            if f == 0 or energy > 0:
                attrs[names["initial"]] = f * energy
            else:
                self.report.skip("node.storage_state_fix", [nd], "Initial state relative to an extendable storage without existing capacity.")
            if fix_method == "fix_start_and_horizon_end":
                self.report.note(f"{what}: the end state fix is not transformed (PyPSA has a cyclic or a free end state).")
        elif fix is not None and fix_method == "fix_all_timesteps" and energy > 0:
            attrs[names["set"]] = self.series(fix, what + " storage_state_fix") * energy
        elif fix is not None:
            self.report.skip("node.storage_state_fix", [nd], f"storage_state_fix_method {fix_method} is not transformed.")
        if fix_method is not None:
            self.i.mark("node", ent, "storage_state_fix_method")

    # ------------------------------------------------------------ storage units
    def find_storage_units(self):
        """Storage nodes tied to one two-way link by an energy-to-power constraint become StorageUnits."""
        self.storage_units = {}
        if not self.s.get("Storage_unit_as_link", True):
            return
        values = self.i.values
        nlns_of = defaultdict(list)
        for b in self.i.bynames("node__link__node"):
            nlns_of[b[0]].append(b)
            if b[2] != b[0]:
                nlns_of[b[2]].append(b)
        unit_nodes = {x for x, _ in self.i.bynames("node__to_unit")} | {x for _, x in self.i.bynames("unit__to_node")}
        users = defaultdict(set)
        for (cls, byname, param), v in values.items():
            if param.startswith("constraint_") and param.endswith("_coefficient") and isinstance(v, Map):
                for c in map_to_dict(v):
                    users[c].add((cls, byname, param))
        for s, kind in self.node_kind.items():
            if kind != "storage" or s in unit_nodes:
                continue
            nlns = nlns_of[s]
            if len({b[1] for b in nlns}) != 1:
                continue
            ln = nlns[0][1]
            if any(len(b) != 3 for b in nlns) or len({b[2] if b[0] == s else b[0] for b in nlns}) != 1:
                continue
            bus = nlns[0][2] if nlns[0][0] == s else nlns[0][0]
            if self.node_kind.get(bus) not in ("balance", "storage") or bus == s:
                continue
            st = values.get(("node", (s,), "constraint_storage_count_coefficient"))
            lk = values.get(("link", (ln,), "constraint_link_count_coefficient"))
            if not (isinstance(st, Map) and isinstance(lk, Map)):
                continue
            st, lk = map_to_dict(st), map_to_dict(lk)
            for c in st:
                if c not in lk or not is_number(st[c]) or not is_number(lk[c]) or st[c] == 0 or lk[c] == 0:
                    continue
                sense = values.get(("constraint", (c,), "sense"))
                const = values.get(("constraint", (c,), "constant"))
                if sense not in (None, "equal") or (is_number(const) and const != 0):
                    continue
                if users[c] != {("node", (s,), "constraint_storage_count_coefficient"), ("link", (ln,), "constraint_link_count_coefficient")}:
                    continue
                self.storage_units[s] = {"link": ln, "bus": bus, "nlns": nlns, "constraint": c, "ratio": -float(lk[c]) / float(st[c])}
                self.node_kind[s] = "storage_unit"
                for key in (("node", s, "constraint_storage_count_coefficient"), ("link", ln, "constraint_link_count_coefficient"),
                            ("constraint", c, "sense"), ("constraint", c, "constant")):
                    self.i.mark(*key)
                if len(st) > 1 or len(lk) > 1:
                    self.report.note(f"Storage node {s}: only constraint {c} is used as the energy-to-power ratio; other constraint coefficients are ignored.")
                break

    def build_storage_units(self):
        transfer = self.methods["link_transfer"]
        for s, su in self.storage_units.items():
            ln, bus, ratio = su["link"], su["bus"], su["ratio"]
            what = f"storage node {s} with link {ln}"
            fwd = su["nlns"][0]
            rev = next((b for b in su["nlns"] if b != fwd), None)
            link_cap = self.scalar(self.g("link", ln, "capacity"), what + " capacity")
            cap = first(self.scalar(self.g("node__link__node", fwd, "capacity")), link_cap)
            if rev is not None:
                self.i.mark("node__link__node", rev, "capacity")
            cap_e = self.scalar(self.g("node", s, "storage_capacity"), what + " storage_capacity")
            info = self.capacity_info("link", (ln,), cap, "link")
            max_hours = ratio * cap_e / info["cap"] if cap_e and info["cap"] else 0.0
            self.capacity_info("node", (s,), cap_e, "storage")
            tm = self.g("link", ln, "transfer_method")
            link_eff = self.series(self.g("link", ln, "efficiency"), what + " efficiency")
            e_fwd = first(self.series(self.g("node__link__node", fwd, "efficiency")), link_eff, 1.0)
            e_rev = first(self.series(self.g("node__link__node", rev, "efficiency")) if rev else None, link_eff, 1.0)
            if tm in transfer["lossless"]:
                e_fwd = e_rev = 1.0
            into_storage = fwd[2] == s
            e_store, e_dispatch = (e_fwd, e_rev) if into_storage else (e_rev, e_fwd)
            avail = self.series(self.g("link", ln, "availability"), what + " availability")
            avail = 1.0 if avail is None else avail
            if tm in transfer["one_way"]:
                p_max, p_min = (0.0, -avail) if into_storage else (avail, 0.0)
            else:
                p_max, p_min = avail, -avail
            carrier = self.carrier_of.get(("node", s)) or self.carrier_of.get(("link", ln)) or self.comps["Bus"].get(bus, {}).get("carrier", "")
            attrs = {"bus": bus, "carrier": carrier, "max_hours": max_hours, "efficiency_store": e_store,
                     "efficiency_dispatch": e_dispatch, "p_max_pu": p_max, "p_min_pu": p_min}
            cost = self.series(self.g("link", ln, "operational_cost"), what + " operational_cost")
            if cost is not None and tm not in transfer["no_cost"]:
                attrs["marginal_cost"] = cost
                self.report.note(f"{what}: link operational_cost became the StorageUnit marginal_cost, which PyPSA applies to discharging only.")
            self.apply_direct("StorageUnit", attrs, {"storage": ("node", (s,))})
            if self.g("node", s, "storage_limit_method") not in (None, "no_profile"):
                self.i.used.discard(("node", (s,), "storage_limit_method"))
            inflow = self.g("node", s, "flow_profile")
            if inflow is not None and self.g("node", s, "flow_scaling_method") != "no_inflow":
                attrs["inflow"] = self.series(inflow, f"node {s} flow_profile")
                self.i.mark("node", s, "spill_upper_limit")
            self.i.mark("node", s, "flow_scaling_method")
            energy = self.scalar(self.g("node", s, "storages_existing")) or 0.0
            self.storage_state_attrs(s, attrs, energy * (cap_e or 0.0), SU_STATE)
            if abs(energy - ratio * info["existing"]) > 1e-6:
                self.report.note(f"{what}: storages_existing does not match links_existing times the energy-to-power ratio; PyPSA uses the link units.")
            inv = self.investment_attrs("link", (ln,), [(("link", (ln,)), 1.0)])
            s_inv = self.investment_attrs("node", (s,), [(("node", (s,)), max_hours)], storage=True)
            for key in ("overnight_cost", "capital_cost", "fom_cost"):
                if key in s_inv:
                    inv[key] = inv.get(key, 0.0) + s_inv[key]
            for key in ("discount_rate", "lifetime", "build_year"):
                if key in s_inv and key not in inv:
                    inv[key] = s_inv[key]
                elif key in s_inv and inv[key] != s_inv[key]:
                    self.report.note(f"{what}: {key} of the storage differs from the link; the link value is used.")
            self.add_asset("StorageUnit", s, attrs, info, nom="p_nom", inv_attrs=inv)

    # ------------------------------------------------------------ units
    def commodity_bus(self, nd):
        """Bus + supply generator for a commodity node that feeds a Link."""
        if nd in self.commodity_buses:
            return nd
        self.add("Bus", nd, carrier=nd)
        cost = self.commodity_cost(nd)
        self.add("Generator", nd, bus=nd, carrier=nd, p_nom=self.big, marginal_cost=cost if cost is not None else 0.0)
        self.commodity_buses[nd] = True
        return nd

    def commodity_cost(self, nd):
        price = self.series(self.g("node", nd, "commodity_price"), f"node {nd} commodity_price")
        co2 = self.scalar(self.g("node", nd, "co2_content"), f"node {nd} co2_content")
        if co2:
            self.carrier_co2[nd] = co2
            if nd in self.co2_price:
                price = (price or 0.0) + self.co2_price[nd] * co2
        if self.g("node", nd, "commodity_price_ladder") is not None:
            self.report.skip("node.commodity_price_ladder", [nd])
        return price

    def build_units(self):
        for u in self.i.names("unit"):
            ins, outs = self.unit_in[u], self.unit_out[u]
            real_in = [x for x in ins if self.node_kind.get(x) != "commodity"]
            fuels = [x for x in ins if self.node_kind.get(x) == "commodity"]
            if not outs:
                self.report.skip("unit (no output)", [u], "Units without an output node are not transformed.")
                continue
            if not real_in and len(outs) == 1 and len(fuels) <= 1:
                self.generator(u, outs[0], fuels[0] if fuels else None)
            elif not ins:
                self.report.note(f"Unit {u}: several outputs without inputs; only {outs[0]} kept as a Generator.")
                self.generator(u, outs[0], None)
            else:
                self.link_from_unit(u, real_in + fuels, outs)

    def generator(self, u, out, fuel):
        flow = (u, out)
        eff = self.efficiency(u)
        cap = self.scalar(self.g("unit__to_node", flow, "capacity"), f"unit__to_node {flow} capacity")
        fin = (fuel, u) if fuel else None
        if cap is None and fin:
            cin = self.scalar(self.g("node__to_unit", fin, "capacity"))
            cap = cin * self.scalar(eff) if cin is not None else None
        info = self.capacity_info("unit", (u,), cap, "unit")
        attrs = {"bus": out}
        cost = self.series(self.g("unit__to_node", flow, "other_operational_cost"), f"unit__to_node {flow} other_operational_cost")
        carrier = self.carrier_of.get(("unit", u))
        cost_ents = [(("unit__to_node", flow), 1.0)]
        if fuel:
            fuel_cost = self.commodity_cost(fuel)
            in_cost = self.series(self.g("node__to_unit", fin, "other_operational_cost"), f"node__to_unit {fin} other_operational_cost")
            extra = sum(x for x in (fuel_cost, in_cost) if x is not None) if (fuel_cost is not None or in_cost is not None) else None
            if extra is not None:
                cost = (cost if cost is not None else 0.0) + extra / eff
            if fuel in self.carrier_co2:
                carrier = fuel
            attrs["efficiency"] = eff
            cost_ents.append((("node__to_unit", fin), 1.0 / self.scalar(eff)))
            limit = self.g("node", fuel, "commodity_limit")
            users = [x for x in self.i.names("unit") if fuel in self.unit_in[x]]
            if limit is not None and len(users) == 1:
                attrs["e_sum_max"] = self.scalar(limit, f"node {fuel} commodity_limit") * self.scalar(eff)
        else:
            self.i.mark("unit", u, "efficiency")
            self.i.mark("unit", u, "conversion_method")
        if cost is not None:
            attrs["marginal_cost"] = cost
        attrs["carrier"] = carrier or ""
        self.profile_limits(flow, None, u, attrs, "unit__to_node")
        self.commitment(u, attrs)
        self.apply_direct("Generator", attrs, {"unit": ("unit", (u,)), "flow": ("unit__to_node", flow)})
        for param, attr in (("flow_max_cumulative", "e_sum_max"), ("flow_min_cumulative", "e_sum_min")):
            v = self.scalar(self.g("unit__to_node", flow, param), f"unit__to_node {flow} {param}")
            if v is not None:
                attrs[attr] = v * self.total_hours
        inv = self.investment_attrs("unit", (u,), cost_ents)
        self.add_asset("Generator", u, attrs, info, inv_attrs=inv)

    def link_from_unit(self, u, inputs, outputs):
        bus0 = inputs[0]
        if self.node_kind.get(bus0) == "commodity":
            self.commodity_bus(bus0)
        in0 = (bus0, u)
        f_in0 = ("node__to_unit", in0)
        out1 = outputs[0]
        f_out1 = ("unit__to_node", (u, out1))
        eff = self.efficiency(u)
        eff_s = self.scalar(eff)
        cap_in = self.scalar(self.g(*f_in0, "capacity"))
        cap_out = self.scalar(self.g(*f_out1, "capacity"))
        cap = cap_in if cap_in is not None else (cap_out / eff_s if cap_out is not None and eff_s else None)
        if cap_in is not None and cap_out is not None and eff_s and abs(cap_out - cap_in * eff_s) > 1e-6 * max(cap_out, 1):
            self.report.note(f"Unit {u}: input and output capacities differ; PyPSA Link p_nom uses the input capacity.")
        info = self.capacity_info("unit", (u,), cap, "unit")
        attrs = {"bus0": bus0, "bus1": out1, "efficiency": eff, "carrier": self.carrier_of.get(("unit", u), "")}
        costs = []
        c = self.series(self.g(*f_in0, "other_operational_cost"))
        if c is not None:
            costs.append(c)
        c = self.series(self.g(*f_out1, "other_operational_cost"))
        if c is not None:
            costs.append(c * eff)
        cost_ents = [(f_in0, 1.0), (f_out1, eff_s or 1.0)]
        k = 2
        for nd in outputs[1:] + inputs[1:]:
            is_out = nd in outputs[1:]
            flow = (u, nd) if is_out else (nd, u)
            fcls = "unit__to_node" if is_out else "node__to_unit"
            r = self.ratio(flow, in0)
            if r is None:
                r1 = self.ratio(flow, (u, out1))
                r = r1 * eff if r1 is not None else None
            if r is None:
                r = eff if is_out else 1.0
                self.report.note(f"Unit {u}: no equality_ratio for the {'output to' if is_out else 'input from'} {nd}; PyPSA efficiency{k} set to {'the unit efficiency' if is_out else '-1'}.")
            self.i.mark(fcls, flow, "conversion_coefficient")
            if not is_out and self.node_kind.get(nd) == "commodity":
                self.commodity_bus(nd)
            attrs[f"bus{k}"] = nd
            attrs[f"efficiency{k}"] = r if is_out else -r
            fc = self.series(self.g(fcls, flow, "other_operational_cost"))
            if fc is not None:
                costs.append(fc * r)
            k += 1
        if costs:
            attrs["marginal_cost"] = sum(costs)
        self.i.mark(*f_out1, "capacity")
        self.profile_limits(in0, None, u, attrs, "node__to_unit")
        if "p_max_pu" not in attrs and "p_min_pu" not in attrs:
            self.profile_limits((u, out1), None, None, attrs, "unit__to_node")
        self.commitment(u, attrs)
        self.apply_direct("Link", attrs, {"unit": ("unit", (u,)), "flow": f_in0})
        if not any(k.startswith("ramp_limit") for k in attrs):
            self.apply_direct("Link", attrs, {"flow": f_out1})
        inv = self.investment_attrs("unit", (u,), cost_ents)
        self.add_asset("Link", u, attrs, info, inv_attrs=inv)

    # ------------------------------------------------------------ links
    def build_links(self):
        transfer = self.methods["link_transfer"]
        for ln in self.i.names("link"):
            if any(su["link"] == ln for su in self.storage_units.values()):
                continue
            nlns = [b for b in self.i.bynames("node__link__node") if b[1] == ln]
            if not nlns:
                self.report.skip("link (no nodes)", [ln], "Link without node__link__node.")
                continue
            fwd = nlns[0]
            a, b = fwd[0], fwd[2]
            rev = (b, ln, a) if (b, ln, a) in nlns else None
            for x in (a, b):
                if self.node_kind.get(x) == "commodity":
                    self.commodity_bus(x)
            what = f"link {ln}"
            tm = self.g("link", ln, "transfer_method")
            link_cap = self.scalar(self.g("link", ln, "capacity"), what + " capacity")
            cap = first(self.scalar(self.g("node__link__node", fwd, "capacity")), link_cap)
            cap_rev = first(self.scalar(self.g("node__link__node", rev, "capacity")) if rev else None, link_cap)
            link_eff = self.series(self.g("link", ln, "efficiency"), what + " efficiency")
            eff = first(self.series(self.g("node__link__node", fwd, "efficiency")), link_eff, 1.0)
            eff_rev = first(self.series(self.g("node__link__node", rev, "efficiency")) if rev else None, link_eff, 1.0)
            if tm in transfer["lossless"]:
                eff = eff_rev = 1.0
            cost = self.series(self.g("link", ln, "operational_cost"), what + " operational_cost")
            cost_f = self.series(self.g("node__link__node", fwd, "operational_cost"))
            cost_r = self.series(self.g("node__link__node", rev, "operational_cost")) if rev else None
            if tm in transfer["no_cost"]:
                cost = cost_f = cost_r = None
            cost_f = cost_f if cost_f is not None else cost
            cost_r = cost_r if cost_r is not None else cost
            avail = self.series(self.g("link", ln, "availability"), what + " availability")
            info = self.capacity_info("link", (ln,), cap, "link")
            inv = self.investment_attrs("link", (ln,), [(("link", (ln,)), 1.0)])
            carrier = self.carrier_of.get(("link", ln)) or self.g("node__link__node", fwd, "carrier") or ""
            base = {"carrier": carrier}
            if avail is not None:
                base["p_max_pu"] = avail
            ramps = {}
            self.apply_direct("Link", ramps, {"link_flow": ("node__link__node", fwd)})
            one_way = tm in transfer["one_way"]
            simple = (not one_way and self._is_one(eff) and self._is_one(eff_rev) and cap_rev == cap
                      and cost_f is None and cost_r is None)
            if one_way:
                self.add_asset("Link", ln, dict(base, bus0=a, bus1=b, efficiency=eff, marginal_cost=cost_f, **ramps), info, inv_attrs=inv)
                continue
            if simple:
                attrs = dict(base, bus0=a, bus1=b, efficiency=1.0, p_min_pu=-(avail if avail is not None else 1.0), **ramps)
                self.add_asset("Link", ln, attrs, info, inv_attrs=inv)
                continue
            suffix = self.cfg.get("reverse_link_suffix", "_reverse")
            half = {k: (v * 0.5 if k in ("overnight_cost", "capital_cost", "fom_cost") else v) for k, v in inv.items()}
            if info["ext"]:
                self.report.note(f"Link {ln}: lossy or asymmetric two-way link became two one-way Links ({ln}, {ln}{suffix}) that share the investment cost half and half; their capacities are not bound together.")
            else:
                self.report.note(f"Link {ln}: lossy or asymmetric two-way link became two one-way Links ({ln}, {ln}{suffix}).")
            self.add_asset("Link", ln, dict(base, bus0=a, bus1=b, efficiency=eff, marginal_cost=cost_f, **ramps), info, inv_attrs=half)
            info_r = dict(info, cap=cap_rev)
            self.add_asset("Link", ln + suffix, dict(base, bus0=b, bus1=a, efficiency=eff_rev, marginal_cost=cost_r, **ramps), info_r, inv_attrs=half)

    @staticmethod
    def _is_one(v):
        if isinstance(v, pd.Series):
            return bool(np.allclose(v.values, 1.0))
        return abs(float(v) - 1.0) < 1e-12

    # ------------------------------------------------------------ global constraints and carriers
    def build_global_constraints(self):
        senses = self.methods["global_constraint_sense"]["to_pypsa"]
        fuels = {nd for nd, k in self.node_kind.items() if k == "commodity" and self.i.has("node", nd, "co2_content")}
        for s in self.co2_sets:
            nodes = {x for kind, x in self.set_members[s] if kind == "node"}
            if nodes and fuels - nodes:
                self.report.note(f"Set {s}: PyPSA CO2 limits count all carriers with co2_emissions, also {sorted(fuels - nodes)} outside the set.")
            cum = self.g("set", s, "co2_max_cumulative")
            per = self.g("set", s, "co2_max_period")
            self.i.mark("set", s, "co2_price")
            if cum is not None:
                v = self.scalar(cum, f"set {s} co2_max_cumulative")
                if isinstance(cum, Map) and self.multi:
                    per = per or cum
                else:
                    self.add("GlobalConstraint", s, type="primary_energy", carrier_attribute="co2_emissions",
                             sense=senses["less_than"], constant=v)
            if per is not None:
                d = map_to_dict(per) if isinstance(per, Map) else {self.period_names[0]: per}
                for p, v in d.items():
                    if p not in self.period_names:
                        continue
                    k = self.period_names.index(p)
                    attrs = {"type": "primary_energy", "carrier_attribute": "co2_emissions", "sense": senses["less_than"],
                             "constant": self.scalar(v)}
                    if self.multi:
                        attrs["investment_period"] = self.years[k]
                    self.add("GlobalConstraint", f"{s}_{p}" if self.multi else s, **attrs)
        for s in self.i.names("set"):
            self.capacity_and_energy_limits(s, senses)

    def capacity_and_energy_limits(self, s, senses):
        params = {"max_cumulative": "less_than", "min_cumulative": "greater_than",
                  "flow_max_cumulative": "less_than", "flow_min_cumulative": "greater_than"}
        present = {p: self.g("set", s, p) for p in params if self.i.has("set", s, p)}
        if not present:
            return
        flows = [tuple(x[1:]) for x in self.i.bynames("set__unit_flow") if x[0] == s]
        members = [("unit", f[0] if f[0] in self.unit_out else f[1]) for f in flows]
        members += [(k, x) for k, x in self.set_members[s] if k in ("unit", "link")]
        carriers = {self.component_carrier(k, x) for k, x in members}
        if not members or len(carriers) != 1 or "" in carriers or None in carriers:
            for p in present:
                self.i.used.discard(("set", (s,), p))
            return
        carrier = carriers.pop()
        for p, v in present.items():
            typ = "tech_capacity_expansion_limit" if p in ("max_cumulative", "min_cumulative") else "operational_limit"
            d = map_to_dict(v) if isinstance(v, Map) else {None: v}
            for per, val in d.items():
                if per is not None and per not in self.period_names:
                    continue
                const = self.scalar(val)
                if typ == "operational_limit":
                    const = const * self.total_hours / max(len(self.period_names), 1)
                attrs = {"type": typ, "carrier_attribute": carrier, "sense": senses[params[p]], "constant": const}
                if per is not None and self.multi:
                    attrs["investment_period"] = self.years[self.period_names.index(per)]
                self.add("GlobalConstraint", f"{s}_{p}" + (f"_{per}" if per is not None and self.multi else ""), **attrs)
        self.report.note(f"Set {s}: limits became PyPSA GlobalConstraints on carrier {carrier} (they apply to all assets of that carrier).")

    def component_carrier(self, kind, x):
        for comp in ("Generator", "Link"):
            for name in (x, x + self.cfg.get("new_capacity_suffix", "_new")):
                if name in self.comps[comp]:
                    return self.comps[comp][name].get("carrier")
        return None

    def build_carriers(self):
        used = {""}
        for comp in ORDER:
            if comp in ("Carrier", "GlobalConstraint"):
                continue
            for attrs in self.comps[comp].values():
                used.add(attrs.get("carrier", ""))
        for c in sorted(used - {""}):
            attrs = {}
            if c in self.carrier_co2:
                attrs["co2_emissions"] = self.carrier_co2[c]
            if c in self.i.names("set"):
                growth = self.g("set", c, "invest_max_period")
                if growth is not None:
                    attrs["max_growth"] = self.scalar(growth, f"set {c} invest_max_period")
            self.add("Carrier", c, **attrs)

    # ------------------------------------------------------------ reporting and output
    def report_unused(self):
        for (cls, param), ents in sorted(self.i.unused().items()):
            if cls in ("unit__to_node", "node__to_unit") and param == "carrier":
                continue
            self.report.skip(f"{cls}.{param}", [e[0] if len(e) == 1 else e for e in ents])
        for cls in ("reserve", "constraint", "node__reserve", "unit__node__reserve", "link__node__reserve", "set__reserve", "node__node"):
            ents = self.i.bynames(cls)
            if ents and not any(k[0] == cls for k in self.i.values):
                self.report.skip(cls, [e[0] if len(e) == 1 else e for e in ents])

    def make_network(self):
        import pypsa

        n = pypsa.Network()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            n.set_snapshots(self.snapshots)
            for col in ("objective", "generators"):
                n.snapshot_weightings[col] = self.weights
            n.snapshot_weightings["stores"] = self.store_weights
            if self.multi:
                for col in ("objective", "years"):
                    n.investment_period_weightings[col] = self.period_weightings[col].values
            for comp in ORDER:
                items = self.comps[comp]
                if not items:
                    continue
                names = list(items)
                attrs = n.components[comp].attrs
                keys = sorted({k for a in items.values() for k in a})
                static, dynamic = {}, {}
                for key in keys:
                    default = attrs.at[key, "default"] if key in attrs.index else ("" if key.startswith("bus") else 1.0 if key.startswith("efficiency") else np.nan)
                    col, series = [], {}
                    for nm in names:
                        v = items[nm].get(key, default)
                        if isinstance(v, pd.Series):
                            series[nm] = v
                            v = default
                        col.append(v)
                    static[key] = col
                    if series:
                        dynamic[key] = pd.DataFrame(series, index=n.snapshots)
                n.add(comp, names, **{k: v for k, v in static.items() if k not in dynamic},
                      **{k: _full_frame(dynamic[k], static[k], names, n.snapshots) for k in dynamic})
        return n


def _full_frame(df, static_values, names, snapshots):
    """Time series for all names of a component: series where given, static values elsewhere."""
    out = pd.DataFrame({nm: (df[nm] if nm in df.columns else pd.Series(float(v), index=snapshots))
                        for nm, v in zip(names, static_values)}, index=snapshots)
    return out


def first(*values):
    return next((v for v in values if v is not None), None)


def json_key(v):
    return repr(v)


def operating_points(value):
    """Sorted (point, value) pairs of a Map over operating points in [0, 1], else None."""
    if not isinstance(value, Map):
        return None
    try:
        points = sorted((float(i), v) for i, v in zip(value.indexes, value.values))
    except (TypeError, ValueError):
        return None
    if not points or not all(0 <= p <= 1 and is_number(v) for p, v in points):
        return None
    return [(p, float(v)) for p, v in points]


def default_report_path(output):
    base = output.rstrip("/\\")
    return os.path.splitext(base)[0] + "_report.md"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", help="INES sqlite file, sqlite url or JSON file")
    parser.add_argument("output", help="PyPSA .nc / .h5 file or csv folder")
    parser.add_argument("--scenario", help="INES scenario to use")
    parser.add_argument("--alternative", action="append", help="INES alternative(s) to use, later ones override earlier ones")
    parser.add_argument("--report", help="Markdown report of what was not transformed")
    args = parser.parse_args(argv)

    mapping = load_mapping()
    scenario = args.scenario or mapping["settings"].get("Ines_scenario")
    db = open_ines_source(args.input)
    try:
        ines = InesData(db, scenario=scenario, alternatives=args.alternative)
    finally:
        db.close()
    report = Report(f"INES to PyPSA: {os.path.basename(args.input)}", mapping["parameters"]["not_transformed_reasons"]["ines"])
    conv = InesToPypsa(ines, mapping, report)
    n = conv.run()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        save_network(n, args.output)
    report_path = args.report or default_report_path(args.output)
    report.write(report_path)
    print(f"Wrote PyPSA network to {args.output}")
    report.print_summary()
    print(f"Report: {report_path}")


if __name__ == "__main__":
    main(sys.argv[1:])
