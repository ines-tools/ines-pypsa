import sys
import json
#import spinedb_api as api
#from pathlib import Path

#path = Path(__file__).resolve().parent
#ines = ARGS[1] # use template to verify valid fields?
#map = ARGS[2] # use map to make a general structure like the exporters?
ARGS = sys.argv[1:]
input = ARGS[0]
output = ARGS[1]

with open(input) as f:
    pypsadict = json.load(f)

iodb = {
    "entities" : [],
    "parameter_values" : [],
    "alternatives" : [["PyPSA", ""]]
}

#Bus => node
for (name,parameters) in pypsadict["Bus"].items():
    iodb["entities"].append(["node", "bus "+name, [], None])

#Generator => node
for (name,parameters) in pypsadict["Generator"].items():
    iodb["entities"].append(["node", "gen "+name, [], None])
    iodb["parameter_values"].append(["node", "gen "+name, "commodity_price", parameters["marginal_cost"], "PyPSA"])
    iodb["parameter_values"].append(["node", "gen "+name, "capacity_per_unit", parameters["p_nom"], "PyPSA"])

#Load => node
for (name,parameters) in pypsadict["Load"].items():
    iodb["entities"].append(["node", "load "+name, [], None])
    iodb["parameter_values"].append(["node", "load "+name, "demand_profile", parameters["p_set"], "PyPSA"])

#Link => link or node
for (name,parameters) in pypsadict["Link"].items():
    if parameters["efficiency"]==1 and parameters["marginal_cost"]==0.0 and parameters["p_min_pu"]==-1:
        iodb["entities"].append(["link", "link "+name, [], None])
        iodb["entities"].append(["node__link__node", "bus "+parameters["bus0"]+" "+"link "+name+" "+"bus "+parameters["bus1"], ["bus "+parameters["bus0"], "link "+name, "bus "+parameters["bus1"]], None])
        iodb["parameter_values"].append(["link", "link "+name, "capacity", parameters["p_nom"], "PyPSA"])
        iodb["parameter_values"].append(["link", "link "+name, "efficiency", 1.0, "PyPSA"])
    else:
        iodb["entities"].append(["node__to_unit", "bus "+parameters["bus0"]+" "+"link "+name, ["bus "+parameters["bus0"],"link "+name], None])
        iodb["entities"].append(["unit", "link "+name, [], None])
        iodb["entities"].append(["unit__to_node", "link "+name+" "+"bus "+parameters["bus1"], ["link "+name,"bus "+parameters["bus1"]], None])
        iodb["parameter_values"].append(["unit", "link "+name, "efficiency", parameters["efficiency"], "PyPSA"])
        iodb["parameter_values"].append(["unit__to_node", ["link "+name, parameters["bus1"]], "other_operational_cost", parameters["marginal_cost"], "PyPSA"])
        iodb["parameter_values"].append(["unit__to_node", ["link "+name, parameters["bus1"]], "capacity_per_unit", parameters["p_nom"], "PyPSA"])

#SpineInterface.import_data(output, iodb, "import data from PyPSA")
with open(output, 'w') as f:
    json.dump(iodb, f, indent=4)