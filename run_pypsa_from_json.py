# import data from json
# create network from json
# run optimisation
# print results to json

import sys
import time
#from pathlib import Path
import json
import pypsa

#path = Path(__file__).resolve().parent
ARGS = sys.argv[1:]
inputpath = ARGS[0]#path.joinpath("input_pypsa.json")#
outputpath = ARGS[1]#path.joinpath("output_pypsa.json")#

with open(inputpath) as f:
    networkdict = json.load(f)

t0 = time.time()

network = pypsa.Network()

#Bus needs to go first to avoid warnings of missing busses
buses = networkdict.pop("Bus")
for objectname, object in buses.items():
    network.add("Bus", objectname, **object)

for objectclass, objects in networkdict.items():
    for objectname, object in objects.items():
        network.add(objectclass, objectname, **object)

network.optimize()

t1 = time.time()

m = network.optimize.create_model()

outputdata = {
    "tool" : "PyPSA",
    "time" : t1-t0,
    "objective" : network.objective,
    "#variables" : len(m.variables),
    "#constraints" : len(m.constraints)
}
with open(outputpath, "w") as f:
    json.dump(outputdata, f, indent=4)
    #print(network.links_t.p0, file=f)
    #print(network.generators_t.p, file=f)