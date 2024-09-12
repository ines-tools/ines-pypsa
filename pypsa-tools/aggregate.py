import sys
from copy import deepcopy
import json

import numpy as np
import matplotlib.pyplot as plt

#ARGS = sys.argv[1:]
inputpath = "/home/u0102409/OneDrive_KUL/Mopo/spinefiles/workflow/data/elec.json" #ARGS[0] # json file
outputpath = "/home/u0102409/OneDrive_KUL/Mopo/WP2/Data structure/external data/PyPSA/processed/elec_agg.json" #ARGS[1] # json file

with open(inputpath, 'r') as f:
    inputdata=json.load(f)

outputdata = {}
currency = "currency"
for generator in inputdata["Generator"].values():
    name = generator["carrier"]
    lifetime = generator["lifetime"]
    if type(lifetime) is str:
        lifetime = 0
    if name not in outputdata:
        outputdata[name]={
            "CAPEX+FOM EUR/MW":[generator["capital_cost"]],
            "VOM EUR/MWh":[generator["marginal_cost"]],
            "lifetime":[lifetime],
            "conversion rate":[generator["efficiency"]],
            #"CO2":[],
        }#currency?
    else:
        outputdata[name]["CAPEX+FOM EUR/MW"].append(generator["capital_cost"])
        outputdata[name]["VOM EUR/MWh"].append(generator["marginal_cost"])
        outputdata[name]["lifetime"].append(lifetime)
        outputdata[name]["conversion rate"].append(generator["efficiency"])
        #outputdata[name]["CO2"].append(generator[""])

fig=plt.figure()
ylabels=["CAPEX+FOM EUR/MW", "VOM EUR/MWh", "conversion rate"]#"lifetime",
for index,ylabel in enumerate(ylabels):
    xlabels=[]
    data=[]
    for xlabel,generator in outputdata.items():
        xlabels.append(xlabel)
        data.append(generator[ylabel])
    ax=fig.add_subplot(len(ylabels),1,index+1)
    violins=ax.violinplot(data,showmedians=False,showmeans=False,showextrema=False)
    ax.boxplot(data,showmeans=True,meanprops=dict(marker='D',markerfacecolor='black',markeredgecolor='black'),medianprops=dict(color='black'))
    ax.set_ylabel(ylabel)
    rot=0
    align='center'
    if len(data)>1:
        rot=30#45#90#
        align='right'#'center'#
    ax.set_xticklabels(xlabels,rotation=rot,ha=align)
fig.tight_layout()
for extension in [".png",".svg"]:
    fig.savefig(outputpath[:-5]+extension)
plt.close(fig)

for generator in outputdata.values():
    for parameter, parametervalues in generator.items():
        generator[parameter] = np.median(parametervalues)

with open(outputpath, 'w') as f:
    json.dump(outputdata, f, indent=4)