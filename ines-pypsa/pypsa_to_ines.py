"""Transform a PyPSA network into INES (Spine DB sqlite or JSON).

Usage:
    python pypsa_to_ines.py INPUT OUTPUT [--report REPORT.md]

INPUT  PyPSA netcdf (.nc) or hdf5 (.h5) file, a folder of PyPSA csv files, or a Spine DB
       (sqlite file / url) with PyPSA classes made by spine-pypsa/pypsa_to_spine.py.
OUTPUT INES sqlite file, sqlite url (e.g. db_url@ines_db in Spine Toolbox) or .json file.

The mapping is in the yaml files next to this script; settings in pypsa_to_ines_settings.yaml.
"""

import argparse
import os
import re
import sys
import warnings
from collections import Counter, defaultdict

import numpy as np
import pandas as pd
from spinedb_api import Array, DateTime, Map, TimeSeriesVariableResolution

from ines_pypsa_common import (
    PypsaData,
    Report,
    annuity_factor,
    finite,
    hours_to_duration,
    is_number,
    isnull,
    load_mapping,
    load_network,
    write_ines,
)

FLOW_CLASSES = ("unit__to_node", "node__to_unit")
HANDLED_COMPONENTS = ["Bus", "Carrier", "Load", "Generator", "Line", "Transformer", "Link", "Process",
                      "Store", "StorageUnit", "GlobalConstraint"]


class PypsaToInes:
    def __init__(self, n, mapping, report):
        if getattr(n, "has_scenarios", False) and n.has_scenarios:
            raise ValueError("Stochastic PyPSA networks (n.scenarios) are not supported.")
        self.n = n
        self.d = PypsaData(n)
        self.m = mapping
        self.s = mapping["settings"]
        self.cfg = mapping["entities"]
        self.methods = mapping["methods"]
        self.rel = mapping["relationships"]
        self.report = report
        self.entities = {}
        self.values = {}
        self.inactive = []
        self.used_names = defaultdict(set)
        self.direct = defaultdict(list)
        for row in mapping["parameters"]["direct_parameters"]:
            self.direct[row["component"]].append(row)
        self.bus_carrier = {}
        self.fuel_node = {}
        self.carrier_flows = defaultdict(list)
        self.carrier_links = defaultdict(list)
        self.storage_nodes = set()

    # ------------------------------------------------------------ output helpers
    def ent(self, cls, *byname):
        key = (cls, tuple(byname))
        self.entities.setdefault(key, None)
        if len(byname) == 1:
            self.used_names[cls].add(byname[0])
        return key

    def put(self, entity, param, value):
        if value is None:
            return
        self.values[(entity[0], entity[1], param)] = value

    def got(self, entity, param):
        return self.values.get((entity[0], entity[1], param))

    def unique(self, cls, name, comp):
        if name not in self.used_names[cls]:
            return name
        base = f"{name}_{comp.lower()}"
        cand, i = base, 2
        while cand in self.used_names[cls]:
            cand, i = f"{base}_{i}", i + 1
        self.report.note(f"{comp} {name}: renamed to {cand} because the INES {cls} name was already taken.")
        return cand

    def active(self, comp, name, *ents):
        v = self.d.get(comp, name, "active")
        if v is not None and not isnull(v) and not bool(v):
            for e in ents:
                self.inactive.append((e[0], e[1], False))

    def iv(self, v, factor=1.0):
        """PyPSA value -> INES value (float, bool, str or time series)."""
        if v is None:
            return None
        if isinstance(v, pd.Series):
            vals = v.astype(float).values * factor
            if len(vals) and np.allclose(vals, vals[0]):
                return float(vals[0])
            return TimeSeriesVariableResolution(self.stamps, vals, False, False, index_name="time step")
        if isinstance(v, (bool, np.bool_)):
            return bool(v)
        if is_number(v):
            return float(v) * factor
        return v

    def factor(self, f):
        if f is None:
            return 1.0
        if f == "minutes":
            return self.step_minutes
        if f == "per_minute":
            return 1.0 / self.step_minutes
        return float(f)

    def apply_direct(self, comp, name, roles):
        for row in self.direct[comp]:
            ent = roles.get(row["role"])
            if ent is None:
                continue
            v = self.d.value(comp, name, row["pypsa"])
            if v is not None:
                self.put(ent, row["ines"], self.iv(v, self.factor(row.get("factor"))))

    def period_map(self, value):
        return Map(list(self.period_names), [value] * len(self.period_names), index_name="period")

    # ------------------------------------------------------------ main
    def run(self):
        self.build_time()
        self.build_carriers()
        self.build_buses()
        self.build_loads()
        self.build_generators()
        self.build_lines("Line")
        self.build_lines("Transformer")
        self.build_links()
        self.build_processes()
        self.build_stores()
        self.build_storage_units()
        self.build_global_constraints()
        self.finalize()
        self.report_unused()

    # ------------------------------------------------------------ time
    def build_time(self):
        n = self.n
        snaps = n.snapshots
        if n.has_investment_periods:
            periods = [int(p) for p in n.investment_periods]
            stamps = []
            for p in periods:
                ts = pd.DatetimeIndex(pd.to_datetime(snaps[snaps.get_level_values(0) == p].get_level_values(1)))
                shift = p - ts[0].year
                if shift:
                    ts = pd.DatetimeIndex([t + pd.DateOffset(years=shift) for t in ts])
                    self.report.note(f"Investment period {p}: snapshots moved by {shift} years to lie in the period year (INES has one timeline).")
                stamps.append(ts)
            self.period_names = [str(p) for p in periods]
        else:
            try:
                ts = pd.DatetimeIndex(pd.to_datetime(snaps))
            except (TypeError, ValueError):
                ts = None
            if ts is None or ts.year.min() < 1900:
                w = n.snapshot_weightings.objective.values.astype(float)
                start = pd.Timestamp(self.s.get("Default_start_time", "2030-01-01"))
                ts = pd.DatetimeIndex([start + pd.Timedelta(hours=h) for h in np.concatenate([[0.0], np.cumsum(w)[:-1]])])
                self.report.note(f"Snapshots are not dates; they were placed on a timeline starting {start} using the objective weightings as step lengths.")
            stamps = [ts]
            self.period_names = [str(ts[0].year)]
        self.periods = periods if n.has_investment_periods else [None]
        steps_all = []
        self.period_steps = []
        for ts in stamps:
            diffs = np.asarray((ts[1:] - ts[:-1]).total_seconds(), dtype=float) / 3600.0 if len(ts) > 1 else np.array([])
            if len(diffs):
                steps = np.append(diffs, diffs[-1])
            else:
                steps = np.array([float(n.snapshot_weightings.objective.iloc[0])])
            self.period_steps.append(steps)
            steps_all.extend(steps)
        self.stamps = [t.to_pydatetime() for ts in stamps for t in ts]
        if len(set(self.stamps)) != len(self.stamps):
            raise ValueError("The PyPSA snapshots give duplicate time stamps; INES needs a unique timeline.")
        self.step_hours = Counter(np.round(steps_all, 9)).most_common(1)[0][0]
        self.step_minutes = self.step_hours * 60.0
        w = n.snapshot_weightings
        steps_all = np.asarray(steps_all, dtype=float)
        if not np.allclose(w.stores.values.astype(float), steps_all):
            self.report.note("PyPSA store weightings differ from the time step lengths; INES storages use the time steps.")
        offset = 0
        for k, steps in enumerate(self.period_steps):
            seg = slice(offset, offset + len(steps))
            offset += len(steps)
            ratio = w.objective.values[seg].astype(float) / steps
            if not np.allclose(ratio, ratio[0]) or not np.allclose(w.generators.values[seg].astype(float), w.objective.values[seg].astype(float)):
                self.report.note(f"Period {self.period_names[k]}: snapshot weightings are not proportional to the time steps; the representative weights are lost in INES.")
            elif abs(w.objective.values[seg].sum() - 8760.0) > 1e-3 * 8760:
                self.report.note(f"Period {self.period_names[k]}: snapshot weightings sum to {w.objective.values[seg].sum():.1f} h; INES models scale a period to a year, so costs keep their ratio but the objective scales.")
        if len(set(np.round(steps_all, 9))) > 1:
            self.report.note(f"Snapshots have several step lengths; INES time_resolution set to the most common one ({self.step_hours} h), the timeline keeps the actual steps.")
        self.total_hours = float(w.generators.sum())
        nyears = n.nyears
        if isinstance(nyears, pd.Series):
            if not np.allclose(nyears.values, nyears.values[0]):
                self.report.note("Investment periods model different numbers of hours; costs were scaled with their mean.")
            nyears = float(nyears.mean())
        self.nyears = float(nyears) or 1.0

        system = self.ent("system", self.s.get("System_name", "system"))
        self.put(system, "timeline", TimeSeriesVariableResolution(self.stamps, [1.0] * len(self.stamps), False, False, index_name="time step"))
        for i, ts in enumerate(stamps):
            p = self.ent("period", self.period_names[i])
            self.put(p, "start_time", DateTime(ts[0].to_pydatetime()))
            self.put(p, "duration", hours_to_duration(float(self.period_steps[i].sum())))
            if n.has_investment_periods:
                self.put(p, "years_represented", float(n.investment_period_weightings.years.iloc[i]))
            else:
                self.put(p, "years_represented", 1.0)
        sp = self.ent("solve_pattern", self.s.get("Solve_pattern_name", "solve"))
        self.put(sp, "period", Array(list(self.period_names)))
        self.put(sp, "time_resolution", hours_to_duration(self.step_hours))
        self.put(sp, "solve_mode", "single_solve")
        if n.has_investment_periods:
            rate = self._discount_rate()
            if rate is not None:
                self.put(system, "discount_rate", rate)

    def _discount_rate(self):
        w = self.n.investment_period_weightings
        years = w.years.values.astype(float)
        obj = w.objective.values.astype(float)
        p = np.array(self.periods, dtype=float)
        if np.allclose(obj, years):
            return 0.0

        def weights(r):
            return np.array([sum((1 + r) ** -(p[i] - p[0] + k) for k in range(int(round(years[i])))) for i in range(len(p))])

        grid = np.linspace(0.0, 0.3, 3001)
        errs = [np.abs(weights(r) - obj).max() for r in grid]
        best = int(np.argmin(errs))
        if errs[best] <= 1e-3 * max(obj.max(), 1.0):
            return float(round(grid[best], 6))
        self.report.note("Investment period objective weightings do not match a constant discount rate; system.discount_rate not written.")
        return None

    # ------------------------------------------------------------ carriers and buses
    def carrier_member(self, comp, name, ent_cls, *element):
        carrier = self.d.get(comp, name, "carrier")
        if not carrier or isnull(carrier):
            return None
        self.ent("set", carrier)
        self.ent(ent_cls, carrier, *element)
        return carrier

    def membership_class(self, comp, kind=None):
        cls = self.rel["carrier_membership"][comp]
        if isinstance(cls, list):
            return next(c for c in cls if c.endswith(kind)) if kind else cls[0]
        return cls

    def build_carriers(self):
        suffix = self.cfg["Carrier"].get("fuel_node_suffix", "_fuel")
        for c in self.d.names("Carrier"):
            if not c:
                continue
            self.ent("set", c)
            co2 = self.d.value("Carrier", c, "co2_emissions")
            if co2 is not None and co2 != 0:
                node = self.ent("node", self.unique("node", c + suffix, "Carrier"))
                self.put(node, "node_type", "commodity")
                self.put(node, "co2_content", float(co2))
                self.fuel_node[c] = node[1][0]
            growth = self.d.value("Carrier", c, "max_growth")
            if growth is not None and finite(growth):
                self.put(("set", (c,)), "invest_max_period", self.period_map(float(growth)))

    def build_buses(self):
        for b in self.d.names("Bus"):
            node = self.ent("node", b)
            self.put(node, "node_type", "balance")
            carrier = self.carrier_member("Bus", b, self.membership_class("Bus"), b)
            self.bus_carrier[b] = carrier
            self.apply_direct("Bus", b, {"node": node})

    def flow_carrier(self, flow, bus):
        if self.rel.get("flow_carrier_from_bus") and self.bus_carrier.get(bus):
            self.put(flow, "carrier", self.bus_carrier[bus])

    def build_loads(self):
        profiles = defaultdict(list)
        for ld in self.d.names("Load"):
            bus = self.d.get("Load", ld, "bus")
            active = self.d.get("Load", ld, "active")
            if active is not None and not bool(active):
                self.report.skip("Load (inactive)", [ld], "Inactive loads are left out (INES flow_profile is per node).")
                continue
            p = self.d.get("Load", ld, "p_set")
            sign = self.d.get("Load", ld, "sign")
            profiles[bus].append((ld, p * sign))
        for bus, items in profiles.items():
            total = sum(v for _, v in items)
            node = ("node", (bus,))
            self.put(node, "flow_profile", self.iv(total))
            self.put(node, "flow_scaling_method", "use_profile_directly")
            if len(items) > 1 or items[0][0] != bus:
                self.report.note(f"Loads {', '.join(i[0] for i in items)} were summed into the flow_profile of node {bus}.")

    # ------------------------------------------------------------ capacities and investments
    def capacity(self, comp, name, nom):
        g = lambda a: self.d.get(comp, name, a)
        p_nom, mod, ext = g(nom), g(f"{nom}_mod"), bool(g(f"{nom}_extendable"))
        pmin, pmax, pset = g(f"{nom}_min"), g(f"{nom}_max"), g(f"{nom}_set")
        p_nom = float(p_nom or 0.0)
        mod = float(mod or 0.0)
        if mod > 0:
            cap = mod
        elif p_nom > 0:
            cap = p_nom
        elif ext and finite(pmax) and pmax > 0:
            cap = float(pmax)
        else:
            cap = float(self.s.get("Default_module_capacity", 100))
        res = {"cap": cap, "ext": ext, "integer": mod > 0, "p_nom": p_nom, "existing": 0.0 if ext else p_nom / cap}
        if ext:
            res["max"] = float(pmax) / cap if finite(pmax) else None
            res["min"] = float(pmin) / cap if finite(pmin) and pmin > 0 else None
            res["fix"] = float(pset) / cap if finite(pset) else None
            limited = any(res[k] is not None for k in ("max", "min", "fix"))
            res["method"] = self.methods["extendable"]["to_ines"]["true_with_limits" if limited else "true_without_limits"]
            if p_nom > 0 and not (finite(pmin) and pmin >= p_nom):
                self.report.note(f"{comp} {name}: PyPSA ignores {nom} of an extendable asset, so INES has no existing units for it.")
        else:
            res["method"] = self.methods["extendable"]["to_ines"][False]
        return res

    def put_counts(self, ent, res, kind):
        pre = {"unit": "units", "link": "links", "storage": "storages"}[kind]
        method = "storage_investment_method" if kind == "storage" else "investment_method"
        integer = "storage_investment_uses_integer" if kind == "storage" else "investment_uses_integer"
        self.put(ent, f"{pre}_existing", float(res["existing"]))
        self.put(ent, method, res["method"])
        if res["ext"]:
            for key in ("max", "min", "fix"):
                if res.get(key) is not None:
                    self.put(ent, f"{pre}_{key}_cumulative", res[key])
            if res["integer"]:
                self.put(ent, integer, True)

    def investment(self, comp, name, life_ent, cost_ent, storage=False, scale=1.0):
        pre = "storage_" if storage else ""
        g = lambda a: self.d.get(comp, name, a)
        lifetime, overnight, disc, capital, fom = g("lifetime"), g("overnight_cost"), g("discount_rate"), g("capital_cost"), g("fom_cost")
        life = float(lifetime) if finite(lifetime) else float(self.s.get("Infinite_lifetime", 100))
        inv = rate = None
        nyears = self.nyears
        if finite(overnight):
            inv = float(overnight)
            rate = float(disc) if finite(disc) else float(self.s.get("Interest_rate", 0.05))
        elif finite(capital) and capital != 0:
            rate = float(self.s.get("Interest_rate", 0.05))
            inv = float(capital) / annuity_factor(rate, life) / nyears
            self.report.note("capital_cost (cost for the modelled horizon) was turned into investment_cost with the assumed interest rate and lifetime and the modelled years (PyPSA nyears); use overnight_cost and discount_rate in PyPSA to keep the original values.")
        if inv is not None:
            self.put(cost_ent, f"{pre}investment_cost", inv / scale)
            self.put(life_ent, f"{pre}interest_rate", rate)
        if finite(fom) and fom != 0:
            self.put(cost_ent, f"{pre}fixed_cost", float(fom) / nyears / scale)
            self.report.note("fom_cost (cost for the modelled horizon) was divided by the modelled years (PyPSA nyears) to get the annual INES fixed_cost.")
        to_ines = self.methods["lifetime"]["to_ines"]
        if finite(lifetime):
            self.put(life_ent, f"{pre}lifetime", float(lifetime))
            self.put(life_ent, f"{pre}retirement_method", to_ines["finite"])
        else:
            if inv is not None and not finite(overnight):
                self.put(life_ent, f"{pre}lifetime", life)
            self.put(life_ent, f"{pre}retirement_method", to_ines["infinite"])
        build_year = self.d.value(comp, name, "build_year")
        if build_year:
            self.put(life_ent, f"{pre}commission_date", DateTime(f"{int(build_year)}-01-01T00:00:00"))

    def committable(self, comp, name, unit):
        c = self.d.get(comp, name, "committable")
        self.put(unit, "startup_method", self.methods["committable"]["to_ines"][bool(c)])

    def bind_counts(self, name, coefficients):
        """Equality constraint between unit / storage counts: sum(coefficient * count) = 0."""
        cname = self.unique("constraint", name, "constraint")
        con = self.ent("constraint", cname)
        self.put(con, "constant", 0.0)
        self.put(con, "sense", "equal")
        for ent, param, coef in coefficients:
            prev = self.got(ent, param)
            keys = list(prev.indexes) if isinstance(prev, Map) else []
            vals = list(prev.values) if isinstance(prev, Map) else []
            self.put(ent, param, Map(keys + [cname], vals + [float(coef)], index_name="constraint"))

    # ------------------------------------------------------------ generators
    def build_generators(self):
        for g in self.d.names("Generator"):
            bus = self.d.get("Generator", g, "bus")
            u = self.ent("unit", self.unique("unit", g, "Generator"))
            uname = u[1][0]
            flow = self.ent("unit__to_node", uname, bus)
            self.flow_carrier(flow, bus)
            carrier = self.carrier_member("Generator", g, self.membership_class("Generator"), uname)
            if carrier:
                self.carrier_flows[carrier].append((flow, bus, self.d.get("Generator", g, "p_nom_extendable")))
            res = self.capacity("Generator", g, "p_nom")
            self.put(flow, "capacity", res["cap"])
            self.put_counts(u, res, "unit")
            self.investment("Generator", g, u, flow)
            self.committable("Generator", g, u)
            self.apply_direct("Generator", g, {"unit": u, "flow": flow})
            self.fixed_profile("Generator", g, "p_set", flow, res)
            eff = self.d.value("Generator", g, "efficiency")
            if carrier in self.fuel_node:
                self.ent("node__to_unit", self.fuel_node[carrier], uname)
                self.put(u, "conversion_method", "constant_efficiency")
                self.put(u, "efficiency", self.iv(eff) if eff is not None else 1.0)
            elif eff is not None:
                self.put(u, "efficiency", self.iv(eff))
            for attr, param in (("e_sum_max", "flow_max_cumulative"), ("e_sum_min", "flow_min_cumulative")):
                v = self.d.get("Generator", g, attr)
                if finite(v):
                    self.put(flow, param, float(v) / self.total_hours)
                    if len(self.period_names) > 1:
                        self.report.note(f"Generator {g}: {attr} applies to the whole horizon in PyPSA but {param} applies to each period in INES.")
            self.active("Generator", g, u, flow)

    def fixed_profile(self, comp, name, attr, flow, res):
        v = self.d.value(comp, name, attr)
        if v is None:
            return
        if not res["ext"] and res["p_nom"] > 0:
            self.put(flow, "profile_fix", self.iv(v / res["p_nom"]))
        else:
            self.d.used.discard((comp, name, attr))

    # ------------------------------------------------------------ lines and transformers
    def build_lines(self, comp):
        for ln in self.d.names(comp):
            bus0, bus1 = self.d.get(comp, ln, "bus0"), self.d.get(comp, ln, "bus1")
            link = self.ent("link", self.unique("link", ln, comp))
            lname = link[1][0]
            nln = self.ent("node__link__node", bus0, lname, bus1)
            self.flow_carrier(nln, bus0)
            carrier = self.carrier_member(comp, ln, self.membership_class(comp), lname)
            if carrier:
                self.carrier_links[carrier].append((link, bus0, bus1, self.d.get(comp, ln, "s_nom_extendable")))
            res = self.capacity(comp, ln, "s_nom")
            self.put(link, "capacity", res["cap"])
            self.put_counts(link, res, "link")
            self.investment(comp, ln, link, link)
            self.put(link, "transfer_method", self.methods["link_transfer"]["to_ines"]["lossless_no_cost"])
            self.apply_direct(comp, ln, {"link": link, "link_flow": nln})
            self.active(comp, ln, link, nln)
        if self.d.names(comp):
            self.report.note(f"{comp}s became INES links (transport model): impedances and Kirchhoff's voltage law are not represented.")

    # ------------------------------------------------------------ links
    def port_indices(self, comp):
        return sorted(int(c[3:]) for c in self.d.static(comp).columns if re.fullmatch(r"bus\d+", c))

    def ports(self, comp, name):
        """(bus, coefficient per unit of activity) of a Link or Process; negative coefficient = input."""
        out = []
        for k in self.port_indices(comp):
            bus = self.d.get(comp, name, f"bus{k}")
            if not bus or isnull(bus):
                continue
            if comp == "Process":
                out.append((bus, self.d.get(comp, name, f"rate{k}")))
            elif k == 0:
                out.append((bus, -1.0))
            else:
                out.append((bus, self.d.get(comp, name, "efficiency" if k == 1 else f"efficiency{k}")))
        return out

    def build_links(self):
        for ln in self.d.names("Link"):
            ports = self.ports("Link", ln)
            pmin, pmax = self.d.get("Link", ln, "p_min_pu"), self.d.get("Link", ln, "p_max_pu")
            symmetric = (
                len(ports) == 2 and is_number(pmin) and is_number(pmax) and pmax > 0
                and abs(pmin + pmax) < 1e-12 and is_number(ports[1][1])
                and not bool(self.d.get("Link", ln, "committable"))
            )
            if symmetric:
                self.link_as_link(ln, ports, pmax)
            else:
                u, ref_flow, res = self.multiport_unit("Link", ln, ports)
                if self._has_negative(pmin):
                    self.reverse_unit(ln, ports, pmin, res, u)

    def link_as_link(self, ln, ports, pmax):
        (bus0, _), (bus1, eff) = ports
        link = self.ent("link", self.unique("link", ln, "Link"))
        lname = link[1][0]
        nln = self.ent("node__link__node", bus0, lname, bus1)
        self.flow_carrier(nln, bus0)
        carrier = self.carrier_member("Link", ln, self.membership_class("Link", "link"), lname)
        if carrier:
            self.carrier_links[carrier].append((link, bus0, bus1, self.d.get("Link", ln, "p_nom_extendable")))
        res = self.capacity("Link", ln, "p_nom")
        self.put(link, "capacity", res["cap"])
        self.put_counts(link, res, "link")
        self.investment("Link", ln, link, link)
        mc = self.d.value("Link", ln, "marginal_cost")
        lossy = abs(float(eff) - 1.0) > 1e-12
        key = ("lossy" if lossy else "lossless") + ("_with_cost" if mc is not None else "_no_cost")
        self.put(link, "transfer_method", self.methods["link_transfer"]["to_ines"][key])
        if lossy:
            self.put(link, "efficiency", float(eff))
            self.report.note(f"Link {ln}: two-way PyPSA link with efficiency {eff}; PyPSA applies it as p1 = -efficiency * p0 in both directions, INES as a loss in both directions.")
        if mc is not None:
            self.report.note(f"Link {ln}: PyPSA marginal_cost is negative for the reverse flow, INES operational_cost applies to both directions.")
        if pmax != 1.0:
            self.put(link, "availability", float(pmax))
        self.d.mark("Link", ln, "p_min_pu", "p_max_pu", "efficiency")
        self.apply_direct("Link", ln, {"link": link, "link_flow": nln})
        self.active("Link", ln, link, nln)

    @staticmethod
    def _mean(v):
        return float(v.mean()) if isinstance(v, pd.Series) else float(v)

    @staticmethod
    def _has_negative(v):
        if isinstance(v, pd.Series):
            return bool((v < 0).any())
        return is_number(v) and v < 0

    def multiport_unit(self, comp, name, ports):
        """INES unit for a Link / Process. ports: (bus, coefficient per unit of activity), negative = input."""
        u = self.ent("unit", self.unique("unit", name, comp))
        uname = u[1][0]
        ref_i = next((i for i, (_, c) in enumerate(ports) if self._mean(c) < 0), 0)
        ref_bus, ref_coef = ports[ref_i]
        scale = abs(self._mean(ref_coef)) or 1.0
        if isinstance(ref_coef, pd.Series) and not np.allclose(ref_coef.values, ref_coef.values[0]):
            self.report.note(f"{comp} {name}: time-varying coefficient of the reference port {ref_bus} averaged.")
        ref_in = self._mean(ref_coef) < 0
        ref_flow = self.ent("node__to_unit", ref_bus, uname) if ref_in else self.ent("unit__to_node", uname, ref_bus)
        self.flow_carrier(ref_flow, ref_bus)
        carrier = self.carrier_member(comp, name, self.membership_class(comp, "unit"), uname)
        if carrier:
            self.carrier_flows[carrier].append((ref_flow, ref_bus, self.d.get(comp, name, "p_nom_extendable")))
        res = self.capacity(comp, name, "p_nom")
        self.put(ref_flow, "capacity", res["cap"] * scale)
        self.put_counts(u, res, "unit")
        self.investment(comp, name, u, ref_flow, scale=scale)
        self.committable(comp, name, u)
        self.apply_direct(comp, name, {"unit": u, "flow": ref_flow})
        mc = self.d.value(comp, name, "marginal_cost")
        if mc is not None:
            self.put(ref_flow, "other_operational_cost", self.iv(mc / scale))
        pmax = self.d.value(comp, name, "p_max_pu")
        if pmax is not None:
            self.put(ref_flow, "profile_limit_upper", self.iv(pmax.clip(lower=0) if isinstance(pmax, pd.Series) else max(pmax, 0.0)))
        pmin = self.d.get(comp, name, "p_min_pu")
        if (isinstance(pmin, pd.Series) and (pmin > 0).any()) or (is_number(pmin) and pmin > 0):
            self.put(ref_flow, "profile_limit_lower", self.iv(pmin.clip(lower=0) if isinstance(pmin, pd.Series) else pmin))
        self.fixed_profile(comp, name, "p_set", ref_flow, res)
        main_done = not ref_in
        seen = {(ref_flow[0], ref_bus)}
        for i, (bus, coef) in enumerate(ports):
            if i == ref_i:
                continue
            out = self._mean(coef) >= 0
            flow_cls = "unit__to_node" if out else "node__to_unit"
            if (flow_cls, bus) in seen:
                self.report.skip(f"{comp}.bus{i}", [name], "INES has one flow per unit and node in each direction; this port repeats a node.")
                continue
            seen.add((flow_cls, bus))
            flow = self.ent(flow_cls, uname, bus) if out else self.ent(flow_cls, bus, uname)
            self.flow_carrier(flow, bus)
            ratio = coef.abs() / scale if isinstance(coef, pd.Series) else abs(float(coef)) / scale
            if out and not main_done:
                self.put(u, "conversion_method", "constant_efficiency")
                self.put(u, "efficiency", self.iv(ratio))
                main_done = True
                continue
            self.put(flow, "conversion_coefficient", 0.0)
            uf = self.ent("unit_flow__unit_flow", *flow[1], *ref_flow[1])
            self.put(uf, "equality_ratio", self.iv(ratio))
        self.active(comp, name, u, ref_flow)
        return u, ref_flow, res

    def reverse_unit(self, ln, ports, pmin, res, u):
        """Unit for the bus1 -> bus0 direction of a Link with negative p_min_pu."""
        (bus0, _), (bus1, eff) = ports[0], ports[1]
        rname = self.unique("unit", ln + self.cfg["Link"].get("reverse_unit_suffix", "_reverse"), "Link")
        ur = self.ent("unit", rname)
        fin = self.ent("node__to_unit", bus1, rname)
        fout = self.ent("unit__to_node", rname, bus0)
        self.flow_carrier(fin, bus1)
        self.flow_carrier(fout, bus0)
        self.put(fout, "capacity", res["cap"])
        self.put_counts(ur, res, "unit")
        upper = (-pmin).clip(lower=0) if isinstance(pmin, pd.Series) else max(-pmin, 0.0)
        self.put(fout, "profile_limit_upper", self.iv(upper))
        self.put(ur, "conversion_method", "constant_efficiency")
        self.put(ur, "efficiency", self.iv(1.0 / eff))
        mc = self.d.value("Link", ln, "marginal_cost")
        if mc is not None:
            self.put(fout, "other_operational_cost", self.iv(-mc))
        for p in ("lifetime", "retirement_method"):
            self.put(ur, p, self.got(u, p))
        if res["ext"]:
            self.put(ur, "investment_method", res["method"])
            self.bind_counts(ln + self.cfg["Link"].get("bind_constraint_suffix", "_bind"),
                             [(u, "constraint_unit_count_coefficient", 1.0), (ur, "constraint_unit_count_coefficient", -1.0)])
        if len(ports) > 2:
            self.report.note(f"Link {ln}: the reverse direction unit only has bus1 -> bus0; further ports apply to the forward direction only.")
        self.active("Link", ln, ur, fin, fout)

    def build_processes(self):
        if "Process" not in self.n.all_components:
            return
        for pr in self.d.names("Process"):
            self.multiport_unit("Process", pr, self.ports("Process", pr))
            if self._has_negative(self.d.get("Process", pr, "p_min_pu")):
                self.report.skip("Process.p_min_pu", [pr], "A negative activity of a Process is not transformed.")

    # ------------------------------------------------------------ storage
    def storage_state(self, comp, name, node, total_energy):
        g = lambda a: self.d.get(comp, name, a)
        if comp == "Store":
            cyclic, per_period, initial, fix = g("e_cyclic"), g("e_cyclic_per_period"), g("e_initial"), "e_set"
        else:
            cyclic, per_period, initial, fix = g("cyclic_state_of_charge"), g("cyclic_state_of_charge_per_period"), g("state_of_charge_initial"), "state_of_charge_set"
        cyc = self.methods["storage_cycling"]["to_ines"]
        multi = len(self.period_names) > 1
        if per_period and multi:
            self.put(node, "storage_state_binding_method", cyc["cyclic_per_period"])
        elif cyclic:
            self.put(node, "storage_state_binding_method", cyc["cyclic"])
        else:
            self.put(node, "storage_state_binding_method", cyc["not_cyclic"])
        fixed = self.d.value(comp, name, fix)
        if fixed is not None and total_energy > 0:
            self.put(node, "storage_state_fix", self.iv(fixed / total_energy))
            self.put(node, "storage_state_fix_method", "fix_all_timesteps")
            return
        if fixed is not None:
            self.d.used.discard((comp, name, fix))
        if cyclic or (per_period and multi):
            return
        initial = float(initial or 0.0)
        if initial == 0.0:
            self.put(node, "storage_state_fix", 0.0)
        elif total_energy > 0:
            self.put(node, "storage_state_fix", initial / total_energy)
        else:
            self.report.skip(f"{comp}.{'e_initial' if comp == 'Store' else 'state_of_charge_initial'}", [name],
                             "An initial state in MWh cannot be given relative to an extendable storage.")
            return
        self.put(node, "storage_state_fix_method", "fix_start")

    def build_stores(self):
        by_bus = defaultdict(list)
        for s in self.d.names("Store"):
            by_bus[self.d.get("Store", s, "bus")].append(s)
        merge = self.s.get("Merge_single_store_into_bus", True)
        for s in self.d.names("Store"):
            bus = self.d.get("Store", s, "bus")
            if merge and len(by_bus[bus]) == 1 and bus not in self.storage_nodes:
                node = ("node", (bus,))
                if s != bus:
                    self.report.note(f"Store {s} was merged into its bus node {bus}.")
            else:
                node = self.ent("node", self.unique("node", s, "Store"))
                link = self.ent("link", self.unique("link", s + self.cfg["Store"].get("connection_link_suffix", "_connection"), "Store"))
                self.ent("node__link__node", bus, link[1][0], node[1][0])
                self.put(link, "transfer_method", "no_losses_no_cost")
                self.put(link, "capacity", float(self.s.get("Unlimited_capacity", 1e6)))
                self.put(link, "links_existing", 1.0)
                self.put(link, "investment_method", "not_allowed")
                self.active("Store", s, link)
            self.storage_nodes.add(node[1][0])
            self.put(node, "node_type", "storage")
            res = self.capacity("Store", s, "e_nom")
            self.put(node, "storage_capacity", res["cap"])
            self.put_counts(node, res, "storage")
            self.investment("Store", s, node, node, storage=True)
            self.apply_direct("Store", s, {"storage": node})
            self.storage_state("Store", s, node, res["existing"] * res["cap"])
            self.carrier_member("Store", s, self.membership_class("Store"), node[1][0])
            self.active("Store", s, node)

    def link_can_hold(self, su):
        """True when a two-way INES link can carry the StorageUnit's charge and discharge limits and costs."""
        pmin, pmax = self.d.raw("StorageUnit", su, "p_min_pu"), self.d.raw("StorageUnit", su, "p_max_pu")
        if any(self.d.value("StorageUnit", su, a) is not None for a in ("marginal_cost", "p_dispatch_set", "p_store_set")):
            return False
        no_charge = (isinstance(pmin, pd.Series) and np.allclose(pmin.values, 0)) or (is_number(pmin) and pmin == 0)
        if no_charge:
            return True
        if isinstance(pmin, pd.Series) or isinstance(pmax, pd.Series):
            a = pmin.values if isinstance(pmin, pd.Series) else np.full(len(self.stamps), pmin)
            b = pmax.values if isinstance(pmax, pd.Series) else np.full(len(self.stamps), pmax)
            return bool(np.allclose(a, -b))
        return abs(pmin + pmax) < 1e-12

    def build_storage_units(self):
        cfg = self.cfg["StorageUnit"]
        for su in self.d.names("StorageUnit"):
            g = lambda a: self.d.get("StorageUnit", su, a)
            bus = g("bus")
            node = self.ent("node", self.unique("node", su, "StorageUnit"))
            nname = node[1][0]
            self.storage_nodes.add(nname)
            self.put(node, "node_type", "storage")
            res = self.capacity("StorageUnit", su, "p_nom")
            max_hours = float(g("max_hours"))
            self.put(node, "storage_capacity", res["cap"] * max_hours)
            self.put_counts(node, res, "storage")
            if self.s.get("Storage_unit_as_link", True) and self.link_can_hold(su):
                power = self.storage_unit_link(su, bus, node, res)
                members = [(self.membership_class("StorageUnit", "link"), power)]
            else:
                power, members = self.storage_unit_units(su, bus, node, res)
            for p in ("lifetime", "retirement_method", "commission_date"):
                self.put(node, f"storage_{p}", self.got(power, p))
            suffix = cfg.get("energy_to_power_constraint_suffix", "_energy_to_power")
            for i, (_, e) in enumerate(members):
                coef = "constraint_link_count_coefficient" if e[0] == "link" else "constraint_unit_count_coefficient"
                self.bind_counts(su + suffix + ("" if i == 0 else f"_{i + 1}"),
                                 [(e, coef, 1.0), (node, "constraint_storage_count_coefficient", -1.0)])
            self.apply_direct("StorageUnit", su, {"storage": node})
            inflow = self.d.value("StorageUnit", su, "inflow")
            if inflow is not None:
                self.put(node, "flow_profile", self.iv(inflow))
                self.put(node, "flow_scaling_method", "use_profile_directly")
                self.put(node, "spill_upper_limit", self.iv(inflow))
            self.storage_state("StorageUnit", su, node, res["existing"] * res["cap"] * max_hours)
            if self.carrier_member("StorageUnit", su, self.membership_class("StorageUnit", "node"), nname):
                for cls, ent in members:
                    self.carrier_member("StorageUnit", su, cls, ent[1][0])
            self.active("StorageUnit", su, node, *[m for _, m in members])

    def storage_unit_link(self, su, bus, node, res):
        """Two-way link bus <-> storage node; its count is tied to the storage count by a constraint."""
        g = lambda a: self.d.get("StorageUnit", su, a)
        link = self.ent("link", self.unique("link", su, "StorageUnit"))
        lname, nname = link[1][0], node[1][0]
        pmin, pmax = g("p_min_pu"), g("p_max_pu")
        e_store, e_dispatch = g("efficiency_store"), g("efficiency_dispatch")
        two_way = self._has_negative(pmin)
        is_one = lambda v: is_number(v) and abs(v - 1.0) < 1e-12
        transfer = self.methods["link_transfer"]["to_ines"]
        if two_way:
            nln = self.ent("node__link__node", bus, lname, nname)
            if not is_one(e_store):
                self.put(nln, "efficiency", self.iv(e_store))
            if not is_one(e_dispatch):
                self.put(link, "efficiency", self.iv(e_dispatch))
            lossless = is_one(e_store) and is_one(e_dispatch)
            self.put(link, "transfer_method", transfer["lossless_no_cost" if lossless else "lossy_with_cost"])
        else:
            nln = self.ent("node__link__node", nname, lname, bus)
            if not is_one(e_dispatch):
                self.put(nln, "efficiency", self.iv(e_dispatch))
            self.put(link, "transfer_method", self.methods["link_transfer"]["one_way"][0])
        self.flow_carrier(nln, bus)
        if not (is_number(pmax) and pmax == 1.0):
            self.put(link, "availability", self.iv(pmax))
        self.put(link, "capacity", res["cap"])
        self.put_counts(link, res, "link")
        self.investment("StorageUnit", su, link, link)
        self.d.mark("StorageUnit", su, "p_min_pu", "p_max_pu", "efficiency_store", "efficiency_dispatch", "marginal_cost",
                    "p_dispatch_set", "p_store_set")
        return link

    def storage_unit_units(self, su, bus, node, res):
        """Charge and discharge units, for limits a two-way link cannot hold (asymmetric profiles, dispatch cost, set points)."""
        cfg = self.cfg["StorageUnit"]
        g = lambda a: self.d.get("StorageUnit", su, a)
        nname = node[1][0]
        cls_unit = self.membership_class("StorageUnit", "unit")
        ud = self.ent("unit", self.unique("unit", su + cfg.get("discharge_suffix", "_discharge"), "StorageUnit"))
        self.ent("node__to_unit", nname, ud[1][0])
        d_out = self.ent("unit__to_node", ud[1][0], bus)
        self.flow_carrier(d_out, bus)
        self.put(d_out, "capacity", res["cap"])
        self.put_counts(ud, res, "unit")
        self.put(ud, "conversion_method", "constant_efficiency")
        self.investment("StorageUnit", su, ud, d_out)
        roles = {"discharge": ud, "discharge_flow": d_out}
        members = [(cls_unit, ud)]
        pmin = g("p_min_pu")
        if self._has_negative(pmin):
            uc = self.ent("unit", self.unique("unit", su + cfg.get("charge_suffix", "_charge"), "StorageUnit"))
            c_in = self.ent("node__to_unit", bus, uc[1][0])
            self.ent("unit__to_node", uc[1][0], nname)
            self.flow_carrier(c_in, bus)
            self.put(c_in, "capacity", res["cap"])
            self.put_counts(uc, res, "unit")
            self.put(uc, "conversion_method", "constant_efficiency")
            for p in ("lifetime", "retirement_method", "commission_date"):
                self.put(uc, p, self.got(ud, p))
            upper = (-pmin).clip(lower=0) if isinstance(pmin, pd.Series) else -pmin
            if not (is_number(upper) and upper == 1.0):
                self.put(c_in, "profile_limit_upper", self.iv(upper))
            roles.update({"charge": uc, "charge_flow": c_in})
            members.append((cls_unit, uc))
            self.fixed_profile("StorageUnit", su, "p_store_set", c_in, res)
        else:
            self.d.mark("StorageUnit", su, "efficiency_store")
        self.fixed_profile("StorageUnit", su, "p_dispatch_set", d_out, res)
        self.apply_direct("StorageUnit", su, roles)
        self.report.note(f"StorageUnit {su}: charge and discharge units used (asymmetric limits, dispatch cost or set points); ines_to_pypsa.py returns a Store and Links.")
        return ud, members

    # ------------------------------------------------------------ global constraints
    def build_global_constraints(self):
        senses = self.methods["global_constraint_sense"]["to_ines"]
        for gc in self.d.names("GlobalConstraint"):
            g = lambda a: self.d.get("GlobalConstraint", gc, a)
            typ, attr, sense, const, period, bus = g("type"), g("carrier_attribute"), g("sense"), g("constant"), g("investment_period"), g("bus")
            sense = senses.get(sense)
            bus = None if (bus is None or isnull(bus) or bus == "") else bus
            per = str(int(period)) if finite(period) else None

            def value_for(v):
                return Map([per], [float(v)], index_name="period") if per else float(v)

            if typ == "primary_energy" and attr == "co2_emissions":
                if sense != "less_than":
                    self.report.skip("GlobalConstraint.sense", [gc], "INES has only upper limits for CO2.")
                    continue
                s = self.ent("set", self.unique("set", gc, "GlobalConstraint"))
                for fuel in self.fuel_node.values():
                    self.ent("set__node", s[1][0], fuel)
                if self.s.get("Annualize_periods", True):
                    const = float(const) / self.nyears
                    if abs(self.nyears - 1.0) > 1e-6:
                        self.report.note(f"GlobalConstraint {gc}: CO2 limit divided by the modelled years ({self.nyears:.4g}) because INES periods are scaled to a year (Annualize_periods).")
                self.put(s, "co2_max_period" if per else "co2_max_cumulative", value_for(const))
                if any(self.d.get(c, x, "carrier") in self.fuel_node for c in ("Store", "StorageUnit") for x in self.d.names(c)):
                    self.report.note(f"GlobalConstraint {gc}: emissions from stores and storage units of carriers with co2_emissions are not counted in INES.")
            elif typ in ("tech_capacity_expansion_limit", "operational_limit"):
                members = [f for f in self.carrier_flows.get(attr, []) if bus is None or f[1] == bus]
                links = [ln for ln in self.carrier_links.get(attr, []) if bus is None or bus in (ln[1], ln[2])]
                if typ == "operational_limit":
                    links = []
                    members = [f for f in members if f[0][0] == "unit__to_node"]
                if not members and not links:
                    self.report.skip(f"GlobalConstraint.{typ}", [gc], f"No INES flows with carrier {attr}.")
                    continue
                s = self.ent("set", self.unique("set", gc, "GlobalConstraint"))
                for flow, _, _ in members:
                    self.ent("set__unit_flow", s[1][0], *flow[1])
                for link, _, _, _ in links:
                    self.ent("set__link", s[1][0], link[1][0])
                if typ == "tech_capacity_expansion_limit":
                    names = {"less_than": ["max_cumulative"], "greater_than": ["min_cumulative"], "equal": ["max_cumulative", "min_cumulative"]}
                    for p in names[sense]:
                        self.put(s, p, value_for(const))
                    if any(not bool(m[-1]) for m in members + links):
                        self.report.note(f"GlobalConstraint {gc}: PyPSA limits only extendable capacity, INES max/min_cumulative include existing capacity of non-extendable members.")
                else:
                    names = {"less_than": ["flow_max_cumulative"], "greater_than": ["flow_min_cumulative"], "equal": ["flow_max_cumulative", "flow_min_cumulative"]}
                    hours = self.total_hours / max(len(self.period_names), 1)
                    for p in names[sense]:
                        self.put(s, p, value_for(float(const) / hours))
            else:
                self.report.skip(f"GlobalConstraint.{typ}", [gc])

    # ------------------------------------------------------------ finishing
    def finalize(self):
        for cls in FLOW_CLASSES:
            for (c, byname) in list(self.entities):
                if c != cls:
                    continue
                ent = (c, byname)
                up, lo, fix = (self.got(ent, p) is not None for p in ("profile_limit_upper", "profile_limit_lower", "profile_fix"))
                if fix:
                    self.put(ent, "profile_method", "fixed")
                elif up and lo:
                    self.put(ent, "profile_method", "upper_and_lower_limit")
                elif up:
                    self.put(ent, "profile_method", "upper_limit")
                elif lo:
                    self.put(ent, "profile_method", "lower_limit")
        for (c, byname) in list(self.entities):
            ent = (c, byname)
            if c in FLOW_CLASSES + ("node__link__node",):
                if self.got(ent, "ramp_limit_up") is not None or self.got(ent, "ramp_limit_down") is not None:
                    self.put(ent, "ramp_method", "ramp_limit")
            if c == "node" and self.got(ent, "node_type") == "storage":
                up = self.got(ent, "storage_state_upper_limit") is not None
                lo = self.got(ent, "storage_state_lower_limit") is not None
                method = "upper_and_lower_limit" if up and lo else "upper_limit" if up else "lower_limit" if lo else "no_profile"
                self.put(ent, "storage_limit_method", method)

    def report_unused(self):
        for (c, attr), names in sorted(self.d.unused(HANDLED_COMPONENTS).items()):
            self.report.skip(f"{c}.{attr}", names)
        for comp in self.cfg.get("not_transformed_components", []):
            if comp in self.n.all_components and not self.d.static(comp).empty:
                self.report.skip(comp, self.d.names(comp))
        for comp in ("LineType", "TransformerType"):
            owner = "Line" if comp == "LineType" else "Transformer"
            used = {t for t in self.d.static(owner)["type"] if t} if not self.d.static(owner).empty else set()
            if used:
                self.report.skip(comp, sorted(used), "Standard types (impedances) are not in INES.")


def default_report_path(output):
    if "://" in output:
        return os.path.join(os.getcwd(), "pypsa_to_ines_report.md")
    return os.path.splitext(output)[0] + "_report.md"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", help="PyPSA .nc / .h5 file, csv folder, or Spine DB with PyPSA classes")
    parser.add_argument("output", help="INES sqlite file, sqlite url or .json file")
    parser.add_argument("--report", help="Markdown report of what was not transformed")
    args = parser.parse_args(argv)

    mapping = load_mapping()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        n = load_network(args.input)
    source_name = os.path.basename(args.input.rstrip("/\\"))
    report = Report(f"PyPSA to INES: {source_name}", mapping["parameters"]["not_transformed_reasons"]["pypsa"])
    conv = PypsaToInes(n, mapping, report)
    conv.run()
    settings = mapping["settings"]
    write_ines(
        args.output,
        list(conv.entities.keys()),
        [(c, b, p, v) for (c, b, p), v in conv.values.items()],
        alternative=settings.get("Alternative", "PyPSA"),
        scenario=settings.get("Scenario", "PyPSA"),
        entity_alternatives=conv.inactive,
    )
    report_path = args.report or default_report_path(args.output)
    report.write(report_path)
    print(f"Wrote INES data to {args.output}")
    report.print_summary()
    print(f"Report: {report_path}")


if __name__ == "__main__":
    main(sys.argv[1:])
