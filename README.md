# ines-pypsa
Translation between the ines specification and the PyPSA structure (written for PyPSA 1.4).

| Script (in `ines-pypsa/`) | From | To |
|---|---|---|
| `pypsa_to_ines.py` | PyPSA `.nc` / `.h5` file, csv folder, or a Spine DB with PyPSA classes (from `spine-pypsa/pypsa_to_spine.py`) | INES Spine DB (sqlite file or url) or INES JSON |
| `ines_to_pypsa.py` | INES Spine DB (sqlite file or url) or INES JSON | PyPSA `.nc` / `.h5` file or csv folder |

Both scripts use the same yaml files in opposite directions:

- `pypsa_to_ines_entities.yaml` - which INES entities each PyPSA component becomes and the name suffixes of generated entities
- `pypsa_to_ines_parameters_to_relationships.yaml` - PyPSA attributes (bus, carrier) that become INES relationships
- `pypsa_to_ines_parameters.yaml` - direct parameter mappings (with unit factors) and the reasons used in the reports
- `pypsa_to_ines_parameter_methods.yaml` - PyPSA switches <-> INES methods (committable, extendable, cycling, senses)
- `pypsa_to_ines_settings.yaml` - assumptions (interest rate, default capacities, annualisation, ...)

`ines_pypsa_common.py` has the shared I/O and `ines-spec.json` is the copy of the INES specification that is written
into new INES databases. The two models are not identical, so a round trip does not always give back the original
data. Every run writes a markdown report next to the output (`--report` to choose the file) listing the input
parameters that were not transformed (with the reason) and the approximations made.

## use

Python environment with `pypsa` (>= 1.0), `spinedb_api` and `pyyaml` (e.g. the Spine Toolbox environment).
`xarray` must be new enough for the installed `pandas` for PyPSA to read/write netCDF.

```
python ines-pypsa/pypsa_to_ines.py network.nc ines.sqlite
python ines-pypsa/pypsa_to_ines.py network.nc ines.json
python ines-pypsa/pypsa_to_ines.py sqlite:///pypsa_db.sqlite sqlite:///ines_db.sqlite
python ines-pypsa/ines_to_pypsa.py ines.sqlite network.nc [--scenario NAME | --alternative ALT ...]
python ines-pypsa/ines_to_pypsa.py ines.json network_csv_folder
```

In Spine Toolbox open folder spine_workflow as a project:
+ 1. pypsa_nc: data connection to inputfile in nc format (change the path to your file)
+ 2. pypsa_to_spine_transform: tool for conversion from pypsa to spine (`spine-pypsa/pypsa_to_spine.py`, arguments: nc file, db_url@pypsa_db)
+ 3. pypsa_db: database for pypsa data in spine format
+ 4. pypsa_to_ines_transform: `pypsa_to_ines.py`, arguments: db_url@pypsa_db (or the nc file directly), db_url@ines_db
+ 5. ines_db: database for the data in ines format

The tool specification `ines_to_pypsa_transform` runs the other direction (arguments: db_url@ines_db and the output file).

## Tests

`tests/make_pypsa_test_networks.py` builds small PyPSA networks (power system, sector coupling, multi-period
investments) and `tests/run_roundtrip.py --solve` converts them to INES (json and sqlite) and back, converts the INES
certification cases of the ines-spec repository to PyPSA and back, optimises both PyPSA networks with HiGHS and
compares the objectives (`test_output/results.md`). `--pypsa FILE` adds other PyPSA networks.
`tests/compare_networks.py` and `tests/compare_ines_json.py` show attribute differences between two networks / INES files.

## Main mapping

| PyPSA | INES |
|---|---|
| Bus | node (balance) |
| Carrier | set (members set__node / set__unit / set__link); a carrier with co2_emissions also becomes a commodity node `<carrier>_fuel` with co2_content feeding its generators |
| Load | flow_profile of the bus node (loads of a bus are summed) |
| Generator | unit + unit__to_node (capacity, profiles, costs, ramps, unit commitment) |
| Line, Transformer | link + node__link__node (transport model) |
| Link, symmetric two-port | link + node__link__node |
| Link, other / Process | unit, bus0 (negative rate) inputs and bus1.. outputs; extra ports get conversion_coefficient 0 and unit_flow__unit_flow equality_ratio; p_min_pu < 0 adds a reverse unit with bound unit counts |
| Store | storage node (merged into its bus node when it is the only store there) |
| StorageUnit | storage node + two-way link to the bus (charge efficiency on node__link__node, discharge efficiency on the link; one-way when it cannot charge); the counts are tied by a constraint (constraint_link_count_coefficient 1, constraint_storage_count_coefficient -1, so storage_capacity / link capacity = max_hours); charge and discharge units are used instead when the link cannot hold asymmetric limits, marginal_cost or set points; inflow -> flow_profile and spill_upper_limit |
| GlobalConstraint primary_energy (co2, <=) | set with co2_max_cumulative / co2_max_period over the fuel nodes |
| GlobalConstraint tech_capacity_expansion_limit / operational_limit | set with max/min_cumulative / flow_max/min_cumulative over the carrier's flows |
| p_nom, p_nom_mod, p_nom_min/max/set, extendable | capacity, units_existing, units_min/max/fix_cumulative, investment_method, investment_uses_integer |
| overnight_cost, discount_rate, lifetime, fom_cost, build_year | investment_cost, interest_rate, lifetime, fixed_cost, commission_date |
| snapshots, investment periods, weightings | system timeline, periods (start_time, duration, years_represented), solve_pattern, system discount_rate |

INES -> PyPSA uses the same mapping. Units without inputs (or with one commodity input) become Generators (commodity
price, input costs and CO2 price folded into marginal_cost, co2_content into the carrier); other units become Links;
commodity nodes that feed Links become a bus with a supply generator; a storage node connected only to one link whose
link and storage counts are tied by an equality constraint (constant 0, no other members) becomes a StorageUnit with
max_hours from the coefficients and capacities; other storage nodes become a Bus + Store; INES links
become Links (two one-way Links when lossy or asymmetric); node penalties and spill limits become generators; sets with
CO2 limits become primary_energy GlobalConstraints.

## Costs and time

- PyPSA `capital_cost` and `fom_cost` are costs for the modelled horizon (`n.nyears`), INES costs are annual. PyPSA ->
  INES divides by nyears; if only `capital_cost` is given, `investment_cost` is the annuity divided by an assumed interest
  rate and lifetime (`Interest_rate`, `Infinite_lifetime` in the settings). Use `overnight_cost` and `discount_rate` to keep
  the original values.
- INES periods are treated as representing a year (`Annualize_periods`): INES -> PyPSA sets the objective and generator
  snapshot weightings of each period to sum to 8760 h and keeps the store weightings at the time step lengths; PyPSA -> INES
  divides CO2 limits by nyears.
- Snapshots of investment periods are moved to their period year when needed, because INES has one timeline.

## Information lost in the format transfromation

### Power flow
Currently only the energy flow is implemented in the INES-spec. 
This means that the network is only represented as active power flows with balance in every bus (node).
Slack is always distributed to all buses. 

All parameters related to voltage, angle, reactive power ect. are excluded. Lines and Transformers become INES
links (transport model), so their impedances and Kirchhoff's voltage law are lost; ShuntImpedance, LineType and
TransformerType are not transformed. INES links come back as PyPSA Links.

### Investments
- If only `capital_cost` (annuity for the modelled horizon) is given, the interest rate is assumed (settings) and
  `investment_cost` calculated from it. Both values are then individually wrong but give the same annuity.
- INES needs a unit size when investments are allowed. `p_nom_mod`, `p_nom` or a finite `p_nom_max` is used, otherwise
  `Default_module_capacity` from the settings.
- PyPSA ignores `p_nom` of extendable assets, so they have no existing units in INES. In the other direction an INES
  asset with existing units and investments becomes a fixed and an extendable PyPSA component (`Split_existing_and_new`).
- Infinite lifetimes are written as `Infinite_lifetime` from the settings when an annuity has to be calculated.

### Storage
- StorageUnits with asymmetric charge / discharge limits, a marginal_cost or set points become charge and discharge
  units; these come back as a Store and two Links whose capacities are no longer bound (PyPSA has no user constraints
  in the network data).
- PyPSA store weightings that differ from the time step lengths (representative snapshots) are lost.

### Not transformed
Each run lists the parameters that were not transformed with the reason in its report. The reasons are in
`pypsa_to_ines_parameters.yaml` (`not_transformed_reasons`). In short:
- PyPSA -> INES: coordinates, voltages, impedances, reactive power, `marginal_cost_quadratic`, start-up / shut-down
  ramps, initial up / down times, maintenance, link delays, `max_relative_growth`, store `marginal_cost`, StorageUnit
  `spill_cost`, GlobalConstraints other than primary_energy (CO2, <=), tech_capacity_expansion_limit and
  operational_limit, stochastic scenarios.
- INES -> PyPSA: reserves, user constraints and their coefficients, forecasts, per-entity time resolutions, salvage
  values, start-up tiers and emissions, NOx / SO2, inertia and non-synchronous limits, capacity margins, ramp costs,
  storage state values and price ladders, scheduled retirements and per-period fixed investments, rolling horizon.

## development
+ convert_ines_pypsa.py, convert_pypsa_ines.py, certify_pypsa.py and run_pypsa_in_spinetools.py are older scripts and are
  not used by the transformations above.
