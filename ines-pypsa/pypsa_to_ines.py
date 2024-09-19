import os
import sys
import yaml
from pathlib import Path
import ines_transform
import spinedb_api as api
#from spinedb_api import purge

def main():
    # transform spine db with pypsa data (source db) into a spine db that already has the ines structure (target_db)
    with api.DatabaseMapping(url_db_in) as source_db:
        with api.DatabaseMapping(url_db_out) as target_db:
            # completely empty database
            #purge.purge(target_db, purge_settings=None)
            # add ines structure
            # empty database except for ines structure
            target_db.purge_items('parameter_value')
            target_db.purge_items('entity')
            target_db.purge_items('alternative')
            target_db.refresh_session()
            target_db.commit_session("Purged everything except for the existing ines structure")
            # copy alternatives and scenarios
            for alternative in source_db.get_alternative_items():
                target_db.add_alternative_item(name=alternative["name"])
            for scenario in source_db.get_scenario_items():
                target_db.add_scenario_item(name=scenario["name"])
            for scenario_alternative in source_db.get_scenario_alternative_items():
                target_db.add_scenario_alternative_item(
                    alternative_name=scenario_alternative["alternative_name"],
                    scenario_name=scenario_alternative["scenario_name"],
                    rank=scenario_alternative["rank"]
                )
            # commit changes
            target_db.refresh_session()
            target_db.commit_session("Added scenarios and alternatives")
            # copy entities from yaml files
            target_db = ines_transform.copy_entities(source_db, target_db, entities_to_copy)
            # copy numeric parameters
            #target_db = ines_transform.transform_parameters(source_db, target_db, parameter_transforms, ts_to_map=True)
            # copy method parameters
            #target_db = ines_transform.process_methods(source_db, target_db, parameter_methods)
            # copy entities to parameters
            #target_db = ines_transform.copy_entities_to_parameters(source_db, target_db, entities_to_parameters)

            # manual scripts
            # copy capacity specific parameters (manual scripting)
            #target_db = process_capacities(source_db, target_db)

# only the part below is specific to a tool

# quick conversions using dictionaries
# these definitions can be saved here or in a yaml configuration file
'''
    conversion_configuration

A function that saves/loads from yaml files (currently only supported file type). The data is also available within this function but is only loaded when requested.

If a filepath is given and it exists, it will be loaded. If it does not exist, data from within this function will be saved to the file (if available).

If a filename is given, the data from this function will be returned.

conversions : list of file paths or file names
overwrite : boolean that determines whether an existing file is overwritten with the data inside this function

return a list of conversion dictionaries
'''
def conversion_configuration(conversions = ['pypsa_to_ines_entities', 'pypsa_to_ines_parameters'], overwrite=False):
    returnlist = []
    for conversion in conversions:
        # default is data from within this function
        convertname = conversion
        load = False
        save = False

        # check whether a file or name is passed and reconfigure this function accordingly
        convertpath = Path(conversion)
        if convertpath.suffix == '.yaml':
            convertname = convertpath.stem
            if convertpath.is_file() and not overwrite:
                load = True
            else:
                save = True

        if load:
            # load data from file
            with open(convertpath,'r') as file:
                returnlist.append(yaml.safe_load(file))
        else:
            # get data from within this function
            convertdict = None
            if convertname == 'pypsa_to_ines_entities':
                convertdict = {
                    'Bus': ['node'],
                    #'Carrier': [''],
                }
            if convertname == 'pypsa_to_ines_parameters':
                convertdict = {
                    'Bus': {
                        'node':{
                            'x': ["entity_name", "float", [[1]]]
                        }
                    }
                }
            returnlist.append(convertdict)
            if convertdict:
                if save:
                    # save data to a file
                    with open(convertpath,'w') as file:
                        yaml.safe_dump(convertdict, file)
            else:
                print('The file does not exist and neither does the data for ' + convertname)
    return returnlist

# functions for specific mapping

if __name__ == "__main__":
    developer_mode = False
    if developer_mode:
        # save entities to yaml file
        save_folder = os.path.dirname(__file__)
        conversion_configuration(conversions = [save_folder+'/pypsa_to_ines_entities.yaml', save_folder+'/pypsa_to_ines_parameters.yaml'], overwrite=True)
    else:
        # assume the file to be used inside of Spine Toolbox
        url_db_in = sys.argv[1]
        url_db_out = sys.argv[2]

        # open yaml files
        entities_to_copy,parameter_transforms = conversion_configuration()

        main()