# Convert Power Plant Matching (ppm) and Technology Data Repository (tdr) to the Juha drive Alvaro's Intermediate data Format (jaif) for use in the data pipelines of the energy modelling workbench

import sys
import csv
import pycountry
import spinedb_api as api

def main(ppm,tdr,spd):
    # load data
    with open(tdr, 'r') as file:
        unit_types = list(csv.DictReader(file))
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
    for unit in unit_instances:
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

def commoditymap(commodityname):
    commoditycode=commodityname
    return commoditycode

if __name__ == "__main__":
    ppm = sys.argv[1] # pypsa power plant matching
    tdr = sys.argv[2] # pypsa technology data repository
    spd = sys.argv[3] # spine database preformatted with an intermediate format for the mopo project (including the "Base" alternative)

    main(ppm,tdr,spd)