# Convert Power Plant Matching (ppm) and Technology Data Repository (tdr) to the Juha drive Alvaro's Intermediate data Format (jaif) for use in the data pipelines of the energy modelling workbench
# ppm: Capacity, Efficiency and lifetime (DateOut-DateIn or DateOut-2020)
# tdr: 2020-2050, investment, FOM, efficiency

import sys
import csv
from math import sqrt
import pprint
import pycountry
from fuzzywuzzy.process import extractOne
import spinedb_api as api

def main(ppm,tdr,spd,
    exclude=['Other','Waste','Geothermal','hydro','Hydro','CHP','Reservoir', 'Run-Of-River', 'Pumped Storage', 'PV','Pv','CSP','Wind', 'Onshore', 'Offshore', 'Marine']
):
    # load data
    yearzero=sorted(tdr.keys())[0]
    unit_types={}
    for year,path in tdr.items():
        with open(path, 'r') as file:
            unit_types[year]={}
            for line in csv.reader(file):
                if line[0] not in unit_types[year]:
                    unit_types[year][line[0]]={}
                unit_types[year][line[0]][line[1]]=line[2]
                #unit_types[year][line[0]][line[1]+'_description']=line[3]+' '+line[4]+' '+line[5]
    #print(unit_types)
    #print("#"*50)
    with open(ppm, mode='r') as file:
        unit_instances = list(csv.DictReader(file))
    #print(unit_instances)
    # format data
    jaif = { # dictionary for intermediate data format
        "entities":[],
        "parameter_values":[]
    }
    countrycodelist = []
    commoditylist = []
    #unit_type_key_list = [] # for debugging
    for unit in unit_instances:
        if unit["DateOut"]:
            unit["lifetime"] = max(0,float(unit["DateOut"])-float(yearzero))
        if unit["Fueltype"] not in exclude and unit["Country"] not in exclude and unit["Set"] not in exclude and unit["Technology"] not in exclude:
            unit_types_key=map_powerplants_costs(unit, unit_types)
            #keystring = unit["Fueltype"] + ' ' + unit["Technology"] + ' ' + str(unit_types_key)
            #if keystring not in unit_type_key_list:
                #unit_type_key_list.append(keystring)
            # region
            #print(unit["Country"])
            country = pycountry.countries.search_fuzzy(unit["Country"])[0]
            #print(country)
            countrycode = country.alpha_2
            if countrycode not in countrycodelist:
                countrycodelist.append(countrycode)
                jaif["entities"].extend([
                    [
                        "region",
                        countrycode,
                        None
                    ],
                    [
                        "node",
                        [
                            "elec",
                            countrycode,
                        ],
                        None
                    ],
                ])
                jaif["parameter_values"].extend([
                    [
                        "region",
                        countrycode,
                        "type",
                        "onshore",
                        "Base"
                    ],
                    [
                        "region",
                        countrycode,
                        "GIS_level",
                        "PECD1",
                        "Base"
                    ],
                ])
            # commodity
            if unit["Fueltype"] not in commoditylist:
                commoditylist.append(unit["Fueltype"])
                jaif["entities"].append([
                    "commodity",
                    unit["Fueltype"],
                    None
                ])
            # storage or technology
            if unit["Set"]=="PP":
                jaif["entities"].extend([
                    [
                        "technology",
                        unit["Technology"]+"|"+countrycode+"|"+unit["Name"],# or unit["id"]
                        None
                    ],
                    [
                        "commodity__to_technology",
                        [
                            unit["Fueltype"],
                            unit["Technology"]+"|"+countrycode+"|"+unit["Name"]
                        ],
                        None
                    ],
                    [
                        "technology__region",
                        [
                            unit["Technology"]+"|"+countrycode+"|"+unit["Name"],
                            countrycode
                        ],
                        None
                    ],
                    [
                        "technology__to_commodity",
                        [
                            unit["Technology"]+"|"+countrycode+"|"+unit["Name"],
                            "elec"
                        ],
                        None
                    ],
                ])
                jaif["parameter_values"].extend([
                    [
                        "technology",
                        unit["Technology"]+"|"+countrycode+"|"+unit["Name"],
                        "efficiency",
                        year_data(unit, unit_types,unit_types_key, "efficiency"),
                        "Base"
                    ],
                    [
                        "technology",
                        unit["Technology"]+"|"+countrycode+"|"+unit["Name"],
                        "lifetime",
                        onetime_data(unit, unit_types[yearzero][unit_types_key[yearzero]], "lifetime"),
                        "Base"
                    ],
                    [
                        "technology__region",
                        [
                            unit["Technology"]+"|"+countrycode+"|"+unit["Name"],
                            countrycode
                        ],
                        "units_existing",
                        onetime_data(unit, unit_types[yearzero][unit_types_key[yearzero]], "capacity"),
                        "Base"
                    ],
                    [
                        "technology__to_commodity",
                        [
                            unit["Technology"]+"|"+countrycode+"|"+unit["Name"],
                            "elec"
                        ],
                        "investment",
                        year_data(unit, unit_types,unit_types_key, "investment"),
                        "Base"
                    ],
                    [
                        "technology__to_commodity",
                        [
                            unit["Technology"]+"|"+countrycode+"|"+unit["Name"],
                            "elec"
                        ],
                        "fixed_cost",
                        year_data(unit, unit_types,unit_types_key, "FOM"),
                        "Base"
                    ],
                    [
                        "technology__to_commodity",
                        [
                            unit["Technology"]+"|"+countrycode+"|"+unit["Name"],
                            "elec"
                        ],
                        "operational_cost",
                        year_data(unit, unit_types,unit_types_key, "VOM"),# may also be 'fuel' for some data but that conflicts with Fueltype
                        "Base"
                    ],
                ])
                #pprint.pprint(year_data(unit, unit_types,unit_types_key, "efficiency"))
            #if unit["Set"]=="CHP": # skip
            if unit["Set"]=="Store":
                jaif["entities"].extend([
                    [
                        "storage",
                        unit["Technology"]+"|"+countrycode+"|"+unit["Name"],
                        None
                    ],
                    [
                        "storage_connection",
                        [
                            unit["Technology"]+"|"+countrycode+"|"+unit["Name"],
                            "elec"
                        ],
                        None
                    ]
                ])
                jaif["parameter_values"].extend([
                    [
                        "storage_connection",
                        [
                            unit["Technology"]+"|"+countrycode+"|"+unit["Name"],
                            "elec"
                        ],
                        "efficiency_in",
                        year_data(unit, unit_types,unit_types_key, "efficiency", modifier=1/sqrt(2)),
                        "Base"
                    ],
                    [
                        "storage_connection",
                        [
                            unit["Technology"]+"|"+countrycode+"|"+unit["Name"],
                            "elec"
                        ],
                        "efficiency_out",
                        year_data(unit, unit_types,unit_types_key, "efficiency", modifier=1/sqrt(2)),
                        "Base"
                    ],
                    [
                        "storage_connection",
                        [
                            unit["Technology"]+"|"+countrycode+"|"+unit["Name"],
                            "elec"
                        ],
                        "investment",
                        year_data(unit, unit_types,unit_types_key, "investment"),
                        "Base"
                    ],
                    [
                        "storage_connection",
                        [
                            unit["Technology"]+"|"+countrycode+"|"+unit["Name"],
                            "elec"
                        ],
                        "fixed_cost",
                        year_data(unit, unit_types,unit_types_key, "FOM"),
                        "Base"
                    ],
                ])

    #pprint.pprint(unit_type_key_list)
    # save to spine database
    with api.DatabaseMapping(spd) as target_db:
        # empty database except for intermediary format and alternatives
        target_db.purge_items('parameter_value')
        target_db.purge_items('entity')
        target_db.refresh_session()
        target_db.commit_session("Purged entities and parameter values")
        api.import_data(target_db, **jaif)
        target_db.refresh_session()
        target_db.commit_session("Added pypsa data")
    return

def map_powerplants_costs(unit, unit_types):
    unit_types_keys={}
    for year,unit_types_year in unit_types.items():
        unit_type_key = extractOne(unit["Fueltype"], unit_types_year.keys())[0]
        if unit_type_key == 'gas' and unit["Technology"] == 'CCGT':
            unit_type_key = 'CCGT'
        elif unit_type_key == 'gas' and unit["Technology"] == 'Steam Turbine':
            unit_type_key = 'gas boiler steam'
        elif unit_type_key == 'gas' and unit["Technology"] == 'Combustion Engine':
            unit_type_key = 'direct firing gas'
        elif unit_type_key == 'gas' and unit["Technology"] == '':
            unit_type_key = 'CCGT'
        elif unit_type_key == 'solid biomass':
            unit_type_key = 'solid biomass boiler steam'
        unit_types_keys[year] = unit_type_key
    return unit_types_keys

def year_data(unit, unit_types, unit_types_keys, parameter, modifier=1):
    parameter_value = {
        "index_type": "str",
        "rank": 1,
        "index_name": "year",
        "type": "map"
    }
    data = []
    for year,unit_type_key in unit_types_keys.items():
        unit_type_parameters = unit_types[year][unit_type_key]
        datavalue = onetime_data(unit, unit_type_parameters, parameter, modifier=modifier)
        data.append([year, datavalue])
    parameter_value["data"] = data
    return parameter_value

def onetime_data(unit, unit_type_parameters, parameter, modifier=1):
    datavalue = None
    search_parameter = extractOne(parameter, unit.keys(), score_cutoff=80)
    if search_parameter:
        datavalue = unit[search_parameter[0]]
    if not datavalue:
        search_parameter = extractOne(parameter, unit_type_parameters.keys(), score_cutoff=80)
        if search_parameter:
            datavalue = unit_type_parameters[search_parameter[0]]
    if datavalue:
        datavalue = datavalue*modifier
    return datavalue


if __name__ == "__main__":
    ppm = sys.argv[1] # pypsa power plant matching
    tdr = {str(2020+(i-2)*10):sys.argv[i] for i in range(2,len(sys.argv)-1)} # pypsa technology data repository
    spd = sys.argv[-1] # spine database preformatted with an intermediate format for the mopo project (including the "Base" alternative)

    main(ppm,tdr,spd)