import os
import sys
import yaml
import numpy as np
from datetime import datetime
from pathlib import Path
import math
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
            #Add entities that are always there
            target_db = add_base_entities(target_db)

            # copy entities from yaml files
            target_db = ines_transform.copy_entities(source_db, target_db, entities_to_copy)
            #copy parameters to relationships
            target_db = ines_transform.transform_parameters_to_relationship_entities(source_db, target_db, parameters_to_relationships)
            #copy parameters to entities, but the entity name is from a parameter
            target_db = ines_transform.transform_parameters_entity_from_parameter(source_db, target_db, parameters_to_parameters)   
            # copy numeric parameters
            target_db = ines_transform.transform_parameters(source_db, target_db, parameter_transforms)
            # copy method parameters
            target_db = ines_transform.process_methods(source_db, target_db, parameter_methods)
            
            # copy entities to parameters
            #target_db = ines_transform.copy_entities_to_parameters(source_db, target_db, entities_to_parameters)

            # manual scripts
            # copy capacity specific parameters (manual scripting)
            #target_db = process_capacities(source_db, target_db)
            target_db = add_time_structure(source_db,target_db)
            target_db, market_carrier_list = create_market_nodes(source_db,target_db)
            target_db = create_market_relationships(source_db, target_db, market_carrier_list)

            target_db = map_loads_to_nodes(source_db,target_db)
            target_db = map_links_to_units(source_db,target_db)
            target_db = map_storageUnits_to_nodes_and_units(source_db,target_db)

            target_db = add_generator_modified_parameters(source_db,target_db)
            target_db = add_line_capacities_and_lifetimes(source_db,target_db)
            target_db = add_store_capacities_and_lifetimes(source_db,target_db)
            
            #made based on imported data, so these have to be done last
            target_db = add_profile_methods(target_db)
            target_db = add_inflow_methods_and_state_fix(target_db)
            target_db = add_node_types(target_db)
            target_db = change_same_name_entities(target_db)
            target_db = add_entity_alternative_items(target_db)
            target_db.commit_session("loads to nodes")

# only the part below is specific to a tool

def get_settings():
    convertpath = 'pypsa_to_ines_settings.yaml'
    with open(convertpath,'r') as file:
        settings = yaml.safe_load(file)

    return settings

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
def conversion_configuration(conversions = ['pypsa_to_ines_entities', 'pypsa_to_ines_parameters','pypsa_to_ines_parameter_methods',
                                            'pypsa_to_ines_parameters_to_relationships','pypsa_to_ines_parameters_to_parameters'], overwrite=False):
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
                    'Carrier': ['set'], # if emissions then also market node?
                    'Generator': ['unit'],
                    'Line': ['link'],
                    #'LineType': [], # ready line types, could be included in the Line parameters
                    #ShuntImpedance: power flow stuff
                    #'SubNetwork': [], might be handled by the transform of buses and carriers to nodes
                    #'Transformer: [], between busses of different voltage
                    #'TransformerType: [], ready types, could be included in the transformer
                }
            if convertname == 'pypsa_to_ines_parameters':
                convertdict = {
                    'Bus': {
                        'node':{
                            #'x': 'x', coordinates
                            #'y':'y'
                            #'control': type of node
                            #'generator': slack producing generator?
                            #'marginal_price': maybe a result param ie. nodal shadow price
                            #'p' active power at bus
                            #'q' reactive power at bus
                            #'sub_network' again a set?
                            #v_ang voltage_angle
                            #v_mag_pu voltage magnitude
                            #v_mag_pu_set: voltage calculations
                            #v_nom: nominal voltage
                        }
                    },
                    'Carrier':{
                        'set':{
                            #'max_growth': 'invest_max_period'
                            #max_relative_growth: not in ines format
                        },
                    },
                    'Generator':{
                        'unit':{
                            #'active': #entity alternative
                            #'build_year': #maybe to lifetime calucation
                            #'control' slack ?? or empty?
                            #'down_time_before: hot start not in ines
                            #'e_sum_max: max production of energy in period, in a set in ines, create a set for individual units?
                            #'e_sum_min:
                            'efficiency': 'efficiency',
                            #'min_down_time' # not ines?
                            #'min_up_time' # not ines?
                            #'p_set': power_flow stuff
                            #'q_set': power_flow stuff
                            #'ramp_limit_shut_down': # not in ines
                            #'ramp_limit_start_up: # not in ines
                            'shut_down_cost': 'shutdown_cost',
                            #'sign' power_flow stuff
                            # stand_by_cost # not in ines
                            'start_up_cost': 'startup_cost',
                            # up_time_before # no hot start in ines
                            #'weight': #for network clustering
                        }
                    },
                    'Line':{
                        'link':{
                            #'active': # entity_alternative
                            #'b' power flow stuff
                            #'b_pu' power flow stuff
                            #'build_year': #maybe to lifetime calucation
                            #'capital_cost': 'investment_cost',
                            #g: power_flow stuff
                            #g_pu: power_flow stuff
                            #length, not in ines
                            #'num_parallel' number of parallel lines, if type is set
                            # 'r' resistance
                            # 'r_pu' per unit resistance
                            # 'r_pu_eff' effective resistance, calculated from other
                            #sub_network: # not sure if need to be implemented
                            #Terrain_factor: for increasing capital cost 
                            #type: ready types of lines
                            #v_ang_max: power_flow stuff
                            #v_ang_min: power_flow stuff
                            #x: power_flow stuff
                            #x_pu: power_flow stuff
                            #x_pu_eff: power_flow stuff
                        }    
                    },
                    #"Transformer": {} #maybe needed to create links between different voltage nodes
                    #"TransformerType": {} #ready values that should replace values from the Transformer
                }
            if convertname == 'pypsa_to_ines_parameter_methods':
                convertdict = {
                    'Generator': {
                        'unit':{
                            'committable': {
                                False: {
                                    'startup_method': 'no_startup'
                                },
                                True: {
                                    'startup_method': 'integer' # the MIP or linearized choice is model wide and not in the network file
                                }
                            },                          
                        }
                    },
                }
            if convertname == 'pypsa_to_ines_parameters_to_relationships':
                convertdict = {
                    'Generator': {
                        'unit':{
                            'to_node':{
                                'bus': {
                                    'position': 2,
                                    'parameters':{
                                        #'capital_cost': 'investment_cost',
                                        'marginal_cost': 'other_operational_cost',
                                    }
                                }
                            },
                            'set':{
                                'carrier': {
                                    'position': 1
                                }
                            }
                        }
                    },
                    'Bus': {
                        'node':{
                            'set':{
                                'carrier': {
                                    'position': 1  #AC and DC not as carrier elements... create them manually?
                                }
                            }
                        }
                    },
                    'Line': {
                        'link':{
                            'set':{
                                'carrier': {
                                    'position': 1  #AC and DC not as carrier elements... create them manually?
                                }
                            },
                            ('node','node'):{
                                ('bus0','bus1'):{
                                    'position':(1,3)
                                },
                            }
                        }
                    }
                }
            if convertname == 'pypsa_to_ines_parameters_to_parameters':
                convertdict = {
                    "Store":{ 
                        "node":{
                            "bus":{
                                # make mapping to add to the node from bus parameter
                                #'capital_cost': 'storage_investment_cost',
                                'e_max_pu': 'storage_state_upper_limit',
                                'e_min_pu':'storage_state_lower_limit',
                                #'e' result param?
                                #'marginal_cost' option to buy/sell from the market (create units for this)
                                #'marginal_cost_storage': not in ines
                                #'standing_loss': not in ines
                            }
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

#entities that PyPSA just assume exist
def add_base_entities(target_db):
    ines_transform.assert_success(target_db.add_entity_item(entity_class_name='set',entity_byname=('AC',)), warn=True)
    ines_transform.assert_success(target_db.add_entity_item(entity_class_name='set',entity_byname=('DC',)), warn=True)
    return target_db


def add_time_structure(source_db,target_db):
    alt_ent_class_source = [settings["Alternative"], ('Time',) ,'Network']
    #timeline
    ines_transform.assert_success(target_db.add_entity_item(entity_class_name='system',entity_byname=('Time',)), warn=True)
    snapshots = ines_transform.get_parameter_from_DB(source_db,"snapshots", alt_ent_class_source)
    step_length = []
    for i in range(1, len(snapshots.values)):
        diff = (snapshots.values[i].value-snapshots.values[i-1].value).total_seconds()/60.0/60.0
        step_length.append(diff)
    #this is kind of a guess, the last (or the first) time step length is not defined by a date-time array
    step_length.append(diff)
    time_series = api.TimeSeriesVariableResolution(snapshots.values, np.array(step_length), ignore_year = False, repeat=False, index_name="time step")
    target_db = ines_transform.add_item_to_DB(target_db, 'timeline', [settings["Alternative"], ('Time',), 'system'], time_series, value_type=True)
    #resolution
    #This is just the difference between the first two timesteps ie. assumes constant resolution
    minutes_diff = (snapshots.values[1].value-snapshots.values[0].value).total_seconds() / 60.0
    ines_transform.assert_success(target_db.add_entity_item(entity_class_name='temporality',entity_byname=('resolution',)), warn=True)
    target_db = ines_transform.add_item_to_DB(target_db, 'resolution', [settings["Alternative"], ('resolution',), 'temporality'], minutes_diff, value_type=True)

    #periods
    ines_transform.assert_success(target_db.add_entity_item(entity_class_name='solve_pattern',entity_byname=('solve',)), warn=True)
    periods = ines_transform.get_parameter_from_DB(source_db,"investment_periods", alt_ent_class_source)
    if periods: #pypsa periods are always years
        target_db = ines_transform.add_item_to_DB(target_db, 'period', [settings["Alternative"], ('solve',), 'solve_pattern'], periods, value_type=True)
        for period in periods:
            ines_transform.assert_success(target_db.add_entity_item(entity_class_name='period',entity_byname=(period,)), warn=True)
            first = None
            last = None
            for i in snapshots.values:
                if datetime.fromisoformat(i).year == period:
                    if not first:
                        first = i
                    last = i
            duration_hours = (datetime.fromisoformat(last)-datetime.fromisoformat(first)).total_seconds() / 60.0/60.0 + minutes_diff/60.0
            duration = api.Duration(str(duration_hours)+"h")
            target_db = ines_transform.add_item_to_DB(target_db, 'duration', [settings["Alternative"], ('base_period',), 'period'], duration, value_type=True)
            target_db = ines_transform.add_item_to_DB(target_db, 'start_time', [settings["Alternative"], ('base_period',), 'period'], i , value_type=True)
        target_db = ines_transform.add_item_to_DB(target_db, 'period', [settings["Alternative"], ('solve',), 'period'], periods, value_type=True)
    else:  #add base period
        ines_transform.assert_success(target_db.add_entity_item(entity_class_name='period',entity_byname=("base_period",)), warn=True)
        duration_hours = (snapshots.values[-1].value-snapshots.values[0].value).total_seconds() / 60.0/60.0
        duration = api.Duration(str(int(duration_hours))+"h")
        target_db = ines_transform.add_item_to_DB(target_db, 'duration', [settings["Alternative"], ('base_period',), 'period'], duration, value_type=True)
        target_db = ines_transform.add_item_to_DB(target_db, 'start_time', [settings["Alternative"], ('base_period',), 'period'], api.DateTime(snapshots.values[0]), value_type=True)
        target_db = ines_transform.add_item_to_DB(target_db, 'period', [settings["Alternative"], ('solve',), 'solve_pattern'], "base_period", value_type=True)
        
    #representative years
    investment_period_weightings= ines_transform.get_parameter_from_DB(source_db,"investment_period_weightings", alt_ent_class_source)
    
    for i in investment_period_weightings.to_dict()["data"]:
        if i[0] == 'years' and i[1] != None:
            for j in i[1].to_dict():
                target_db = ines_transform.add_item_to_DB(target_db, 'years_represented', [settings["Alternative"], (j[0],), 'period'], j[1], value_type=True)
    
    #weightings?

    return target_db

#commodity carrier      
#PyPSA has no commodity price, they are embedded to the marginal cost
#only for co2_emissions  
def create_market_nodes(source_db, target_db):
    market_carrier_list = list()
    for source_entity in source_db.get_entity_items(entity_class_name='Carrier'): 
        alt_ent_class_source = alt_ent_class_source = [settings["Alternative"], source_entity["entity_byname"],'Carrier']
        co2_emissions = ines_transform.get_parameter_from_DB(source_db, 'co2_emissions', alt_ent_class_source)
        if isinstance(co2_emissions, float):
            if co2_emissions > 0.0:
                market_byname = (source_entity["name"]+"_market",)
                ines_transform.assert_success(target_db.add_entity_item(entity_class_name='node',entity_byname=market_byname,
                ), warn=True)
                alt_ent_class_market = [settings["Alternative"], market_byname, 'node']
                target_db = ines_transform.add_item_to_DB(target_db, 'co2_content', alt_ent_class_market, co2_emissions, value_type=True)
                target_db = ines_transform.add_item_to_DB(target_db, 'node_type', alt_ent_class_market, 'commodity', value_type=True)
                market_carrier_list.append(source_entity["name"])
    #add c02 limit?
    #should be added to the ines format
    
    return target_db, market_carrier_list

def create_market_relationships(source_db, target_db, market_carrier_list):
    for source_entity in source_db.get_entity_items(entity_class_name='Generator'): 
        alt_ent_class_source = alt_ent_class_source = [settings["Alternative"], source_entity["entity_byname"],'Generator']
        carrier = ines_transform.get_parameter_from_DB(source_db,"carrier", alt_ent_class_source)
        if carrier in market_carrier_list:
            relationship_byname = (carrier+"_market", source_entity["name"])
            ines_transform.assert_success(target_db.add_entity_item(entity_class_name="node__to_unit",entity_byname=relationship_byname,
                                            ), warn=True)
    
    return target_db

def add_carrier_investment_limits(source_db,target_db):
    periods = ines_transform.get_parameter_from_DB(source_db,"investment_periods", [settings["Alternative"], ('Time',) ,'Network'])
    for source_entity in source_db.get_entity_items(entity_class_name='Carrier'):
        alt_ent_class_source = alt_ent_class_source = [settings["Alternative"], source_entity["entity_byname"],'Carrier']
        max_growth = ines_transform.get_parameter_from_DB(source_db,"max_growth", alt_ent_class_source)
        if max_growth != math.inf:
            periodic_limit = api.Map([str(x) for x in periods], [max_growth for x in periods], index_name="period")
        target_db = ines_transform.add_item_to_DB(target_db, 'invest_max_period', [settings["Alternative"],(source_entity["name"],),'set']
                                                  ,value=periodic_limit, value_type=True)    

    return target_db

def map_links_to_units(source_db,target_db):
    #check if the other direction is needed
    for source_entity in source_db.get_entity_items(entity_class_name='Link'): 
        alt_ent_class_source = [settings["Alternative"], source_entity["entity_byname"],'Link']
        p_min_pu = ines_transform.get_parameter_from_DB(source_db,"p_min_pu", alt_ent_class_source)
        bus0 = ines_transform.get_parameter_from_DB(source_db,"bus0", alt_ent_class_source)
        bus1 = ines_transform.get_parameter_from_DB(source_db,"bus1", alt_ent_class_source)
        carrier = ines_transform.get_parameter_from_DB(source_db,"carrier", alt_ent_class_source)
        p_nom_extendable = ines_transform.get_parameter_from_DB(source_db,"p_nom_extendable", alt_ent_class_source)

        unit1_name = source_entity["name"] +"_link_1"
        unit1_byname = (unit1_name,)
        #add entity
        ines_transform.assert_success(target_db.add_entity_item(entity_class_name='unit',entity_byname=unit1_byname,
                    ), warn=True)
        ines_transform.assert_success(target_db.add_entity_item(entity_class_name='set__unit',entity_byname=(carrier,unit1_name),
                    ), warn=True)
        #add relationships
        out_class = 'unit' + "__" + 'to_node'
        out_entity_byname = (unit1_name, bus1)
        in_class = 'node' + "__" + 'to_unit'
        in_entity_byname = (bus0, unit1_name)
        alt_ent_class_unit = [settings["Alternative"], (unit1_name,), 'unit'] 
        alt_ent_class_unit_out = [settings["Alternative"], (unit1_name, bus1), 'unit__to_node']

        names = [unit1_byname,out_class,out_entity_byname,in_class,in_entity_byname]
        
        target_db = create_link_unit_params(source_db, target_db, names, alt_ent_class_source)
        
        investment_binding_map = {
            "1": (alt_ent_class_unit_out, -1),
        }
        target_db = ines_transform.add_item_to_DB(target_db, "interest_rate", alt_ent_class_unit, settings["Interest_rate"], value_type=True)
        target_db = calculate_investment_cost(source_db, target_db, alt_ent_class_source, alt_ent_class_unit_out)
        
        #check if the other direction unit is needed
        neg_flag = False
        if isinstance(p_min_pu, api.TimeSeriesVariableResolution):
            new_values = []
            for value in p_min_pu.values:
                if value < 0.0:
                    neg_flag = True
                new_values.append(value * -1)
            p_min_pu.values = new_values
        else:
            if p_min_pu <0.0:
                neg_flag =True
                p_min_pu = -1 * p_min_pu
        
        if neg_flag:  #allows flow for both directions
            unit2_name = source_entity["name"] +"_link_2"
            unit2_byname = (unit2_name,)
            ines_transform.assert_success(target_db.add_entity_item(entity_class_name='unit',entity_byname=unit2_byname,
                    ), warn=True)
            ines_transform.assert_success(target_db.add_entity_item(entity_class_name='set__unit',entity_byname=(carrier,unit2_name),
                    ), warn=True)
            
            out_entity_byname = (unit2_name, bus0)
            in_entity_byname = (bus1, unit2_name)
            alt_ent_class_unit = [settings["Alternative"], unit2_byname, 'unit']
            alt_ent_class_unit_out = [settings["Alternative"], out_entity_byname, 'unit__to_node']


            names = [unit2_byname,out_class,out_entity_byname,in_class,in_entity_byname]
            target_db = create_link_unit_params(source_db, target_db, names, alt_ent_class_source, p_min_pu = p_min_pu)

            investment_binding_map["2"] = (alt_ent_class_unit,1)
            if p_nom_extendable:
                target_db = bind_investments(source_db, target_db, alt_ent_class_source, investment_binding_map)
            target_db = ines_transform.add_item_to_DB(target_db, "interest_rate", alt_ent_class_unit, settings["Interest_rate"], value_type=True)
            target_db = ines_transform.add_item_to_DB(target_db, "investment_cost", alt_ent_class_unit_out, 0.0, value_type=True)
    return target_db

def create_link_unit_params(source_db, target_db, names, alt_ent_class_source, p_min_pu = None):
    committable = ines_transform.get_parameter_from_DB(source_db,"committable", alt_ent_class_source)
    unit_byname = names[0]
    out_class = names[1]
    out_entity_byname = names[2]
    in_class = names[3]
    in_entity_byname = names[4]
    alt_ent_class_unit = [settings["Alternative"], unit_byname, 'unit']
    alt_ent_class_unit_out =[settings["Alternative"], out_entity_byname, out_class]
    alt_ent_class_unit_in =[settings["Alternative"], in_entity_byname, in_class]

    ines_transform.assert_success(target_db.add_entity_item(entity_class_name=out_class,entity_byname=out_entity_byname,
                ), warn=True)
    ines_transform.assert_success(target_db.add_entity_item(entity_class_name=in_class,entity_byname=in_entity_byname,
                ), warn=True)
    
    #add parameters to entity
    parameters_dict = {    
        'efficiency': 'efficiency',
        'shut_down_cost': 'shutdown_cost',
        'start_up_cost': 'startup_cost',
        #up_time_before # no hot start in ines
    }
    for name, target_name in parameters_dict.items(): 
        value = ines_transform.get_parameter_from_DB(source_db, name, alt_ent_class_source)
        target_db = ines_transform.add_item_to_DB(target_db, target_name, alt_ent_class_unit, value, value_type=True)

    #add methods
    if committable:
        value ='integer'
    else:
        value= 'no_startup'
    target_db = ines_transform.add_item_to_DB(target_db, 'startup_method', alt_ent_class_unit,value=value, value_type=True)

    #add parameters to relationships
    parameters_dict = {
        'marginal_cost': 'other_operational_cost',
    }
    for name, target_name in parameters_dict.items():
        value = ines_transform.get_parameter_from_DB(source_db,name, alt_ent_class_source)
        target_db = ines_transform.add_item_to_DB(target_db, target_name, alt_ent_class_unit_out, value, value_type=True)
        target_db = ines_transform.add_item_to_DB(target_db, target_name, alt_ent_class_unit_in, value, value_type=True)
    
    if p_min_pu != None:
        if isinstance(p_min_pu, api.TimeSeriesVariableResolution) or (isinstance(p_min_pu, float) and p_min_pu != 1.0):
            target_db = ines_transform.add_item_to_DB(target_db, 'profile_limit_upper', alt_ent_class_unit_out, p_min_pu, value_type=True)
    else:
        value = ines_transform.get_parameter_from_DB(source_db, "p_max_pu", alt_ent_class_source)
        if isinstance(value, api.TimeSeriesVariableResolution) or (isinstance(value, float) and value != 1.0):
            target_db = ines_transform.add_item_to_DB(target_db, 'profile_limit_upper', alt_ent_class_unit_out, value, value_type=True)

    relationship_list = [alt_ent_class_unit_out,alt_ent_class_unit_in]
    target_db = add_unit_capacities(source_db,target_db, alt_ent_class_source, alt_ent_class_unit, relationship_list)
    target_db = add_lifetime(source_db, target_db, alt_ent_class_source, alt_ent_class_unit)
    target_db = add_ramps(source_db,target_db, alt_ent_class_source, relationship_list)
    
    return target_db

def map_loads_to_nodes(source_db,target_db):
    for source_entity in source_db.get_entity_items(entity_class_name='Load'): 
        alt_ent_class_source = [settings["Alternative"], source_entity["entity_byname"],'Load']
        sign_param = ines_transform.get_parameter_from_DB(source_db,"sign",alt_ent_class_source)
        p_set_param = ines_transform.get_parameter_from_DB(source_db,"p_set",alt_ent_class_source)
        bus = ines_transform.get_parameter_from_DB(source_db,"bus",alt_ent_class_source)
        alt_ent_class_target = [settings["Alternative"], (bus,) ,'node']

        if isinstance(p_set_param, api.TimeSeriesVariableResolution):
            new_values = []
            for value in p_set_param.values:
                new_values.append(value * sign_param)
            p_set_param.values = new_values
        else:
            p_set_param = sign_param* p_set_param

        target_db = ines_transform.add_item_to_DB(target_db, "flow_profile", alt_ent_class_target, p_set_param, value_type=True)
    
    return target_db

def map_storageUnits_to_nodes_and_units(source_db,target_db):
    for source_entity in source_db.get_entity_items(entity_class_name='StorageUnit'): 
        alt_ent_class_source = [settings["Alternative"], source_entity["entity_byname"],'StorageUnit']
        node_name = source_entity["name"]+"_node"
        unit1_name = source_entity["name"]+"_unit_1"
        unit2_name = source_entity["name"]+"_unit_2"
        node_byname = (node_name,)
        unit1_byname = (unit1_name,)
        unit2_byname = (unit2_name,)
        alt_ent_class_storage = [settings["Alternative"], node_byname, 'node']

        bus = ines_transform.get_parameter_from_DB(source_db,"bus", alt_ent_class_source)
        carrier = ines_transform.get_parameter_from_DB(source_db,"carrier", alt_ent_class_source)
        p_min_pu = ines_transform.get_parameter_from_DB(source_db,"p_min_pu", alt_ent_class_source)
        max_hours = ines_transform.get_parameter_from_DB(source_db,"max_hours", alt_ent_class_source)
        p_nom_extendable = ines_transform.get_parameter_from_DB(source_db,"p_nom_extendable", alt_ent_class_source)

        #ines does not have storage start-end state methods? 
        cyclic_state_of_charge =ines_transform.get_parameter_from_DB(source_db,"cyclic_state_of_charge", alt_ent_class_source)
        cyclic_state_of_charge_per_period =ines_transform.get_parameter_from_DB(source_db,"cyclic_state_of_charge_per_period", alt_ent_class_source)


        #add entity
        ines_transform.assert_success(target_db.add_entity_item(entity_class_name='unit',entity_byname=unit1_byname), warn=True)
        ines_transform.assert_success(target_db.add_entity_item(entity_class_name='node',entity_byname=node_byname), warn=True)

        ines_transform.assert_success(target_db.add_entity_item(entity_class_name='set__unit',entity_byname=(carrier,unit1_name),
                    ), warn=True)
        ines_transform.assert_success(target_db.add_entity_item(entity_class_name='set__node',entity_byname=(carrier,node_name),
                    ), warn=True)
        
        target_db = ines_transform.add_item_to_DB(target_db, 'node_type', alt_ent_class_storage, 'storage', value_type=True)
        node_param_dict = {
            'inflow': 'flow_profile',
            'spill_cost': 'penalty_downward',
            'state_of_charge': 'storage_state_fix',
            #'state_of_charge_initial' # not in ines
            #'state_of_charge_initial_per_period'
            #'marginal_cost_storage': #not in ines
        }
        for name, target_name in node_param_dict.items():
            value = ines_transform.get_parameter_from_DB(source_db, name, alt_ent_class_source)
            target_db = ines_transform.add_item_to_DB(target_db, target_name, alt_ent_class_storage, value, value_type=True)
        
        target_db = add_storage_capacities(source_db, target_db, alt_ent_class_source, alt_ent_class_storage)
        target_db = add_lifetime(source_db, target_db, alt_ent_class_source, alt_ent_class_storage, storage= True) 
        
        #add relationships
        out_class = 'unit' + "__" + 'to_node'
        out_entity_byname = (unit1_name, bus)
        in_class = 'node' + "__" + 'to_unit'
        in_entity_byname = (node_name, unit1_name)
        names = [unit1_byname,out_class,out_entity_byname,in_class,in_entity_byname]
        target_db = create_storageUnit_params(source_db, target_db, names, alt_ent_class_source)
        
        investment_binding_map = {
            "storage" : ([settings["Alternative"], node_byname, 'node'], -1/max_hours),
            "out": ([settings["Alternative"], unit1_byname, 'unit'], 1),
        }
        target_db = ines_transform.add_item_to_DB(target_db, "storage_interest_rate", alt_ent_class_storage, settings["Interest_rate"], value_type=True)
        target_db = calculate_investment_cost(source_db, target_db, alt_ent_class_source, alt_ent_class_storage, storage= True, max_hours = max_hours)
        
        #check if the other direction unit is needed
        neg_flag = False
        if isinstance(p_min_pu, api.TimeSeriesVariableResolution):
            new_values = []
            for value in p_min_pu.values:
                if value < 0.0:
                    neg_flag = True
                new_values.append(value * -1)
            p_min_pu.values = new_values
        else:
            if p_min_pu <0.0:
                neg_flag =True
                p_min_pu = -1 * p_min_pu
        if neg_flag:
            ines_transform.assert_success(target_db.add_entity_item(entity_class_name='unit',entity_byname=unit2_byname,
                    ), warn=True)
            
            ines_transform.assert_success(target_db.add_entity_item(entity_class_name='set__unit',entity_byname=(carrier,unit2_name),
                    ), warn=True)
            
            out_class = 'unit' + "__" + 'to_node'
            out_entity_byname = (unit2_name, node_name)
            in_class = 'node' + "__" + 'to_unit'
            in_entity_byname = (bus, unit2_name)
            names = [unit2_byname,out_class,out_entity_byname,in_class,in_entity_byname]
            target_db = create_storageUnit_params(source_db, target_db, names, alt_ent_class_source, p_min_pu = p_min_pu)

            investment_binding_map["in"] = ([settings["Alternative"],unit2_byname, 'unit'],1)

        if p_nom_extendable:
            target_db = bind_investments(source_db, target_db, alt_ent_class_source, investment_binding_map, storage= True)
    
    return target_db

def create_storageUnit_params(source_db, target_db, names, alt_ent_class_source, p_min_pu = None):
    unit_byname = names[0]
    out_class = names[1]
    out_entity_byname = names[2]
    in_class = names[3]
    in_entity_byname = names[4]
    alt_ent_class_unit = [settings["Alternative"], unit_byname, 'unit']
    alt_ent_class_out = [settings["Alternative"], out_entity_byname, out_class]
    alt_ent_class_in = [settings["Alternative"], in_entity_byname, in_class]
    relationship_list = [alt_ent_class_out, alt_ent_class_in]
    
    ines_transform.assert_success(target_db.add_entity_item(entity_class_name=out_class,entity_byname=out_entity_byname,
                ), warn=True)
    ines_transform.assert_success(target_db.add_entity_item(entity_class_name=in_class,entity_byname=in_entity_byname,
                ), warn=True)

    if p_min_pu != None:
        value = ines_transform.get_parameter_from_DB(source_db, 'efficiency_store', alt_ent_class_source)
        target_db = ines_transform.add_item_to_DB(target_db, 'efficiency', alt_ent_class_unit, value, value_type=True)
    else:
        value = ines_transform.get_parameter_from_DB(source_db, 'efficiency_dispatch', alt_ent_class_source)
        target_db = ines_transform.add_item_to_DB(target_db, 'efficiency', alt_ent_class_unit, value, value_type=True)

    #add parameters to relationships
    parameters_dict = {
        'marginal_cost': 'other_operational_cost',
    }
    for name, target_name in parameters_dict.items():
        value = ines_transform.get_parameter_from_DB(source_db,name, alt_ent_class_source)
        target_db = ines_transform.add_item_to_DB(target_db, target_name, alt_ent_class_out, value, value_type=True)
        target_db = ines_transform.add_item_to_DB(target_db, target_name, alt_ent_class_in, value, value_type=True)

    if p_min_pu != None:
        if isinstance(p_min_pu, api.TimeSeriesVariableResolution) or (isinstance(p_min_pu, float) and p_min_pu != 1.0):
            target_db = ines_transform.add_item_to_DB(target_db, 'profile_limit_upper', alt_ent_class_out, p_min_pu, value_type=True)
    else:
        value = ines_transform.get_parameter_from_DB(source_db, "p_max_pu", alt_ent_class_source)
        if isinstance(value, api.TimeSeriesVariableResolution) or (isinstance(value, float) and value != 1.0):
            target_db = ines_transform.add_item_to_DB(target_db, 'profile_limit_upper', alt_ent_class_out, value, value_type=True)

    target_db = add_ramps(source_db,target_db, alt_ent_class_source, relationship_list)
    target_db = add_unit_capacities(source_db,target_db, alt_ent_class_source, alt_ent_class_unit, relationship_list)
    target_db = add_lifetime(source_db, target_db, alt_ent_class_source, alt_ent_class_unit)
    #all of the invesment cost is set to the storage
    target_db = ines_transform.add_item_to_DB(target_db, "investment_cost", alt_ent_class_out, 0.0, value_type=True)

    return target_db

#change capacities to the form of capacities per unit and number of units
def add_unit_capacities(source_db,target_db,alt_ent_class_source, alt_ent_class_unit, alt_ent_class_relationship_list):
     
    p_nom = ines_transform.get_parameter_from_DB(source_db, 'p_nom', alt_ent_class_source)
    p_nom_max = ines_transform.get_parameter_from_DB(source_db, 'p_nom_max', alt_ent_class_source)
    p_nom_min = ines_transform.get_parameter_from_DB(source_db, 'p_nom_min', alt_ent_class_source)
    p_nom_mod = ines_transform.get_parameter_from_DB(source_db, 'p_nom_mod', alt_ent_class_source)
    p_nom_extendable = ines_transform.get_parameter_from_DB(source_db,"p_nom_extendable", alt_ent_class_source)

    if p_nom_mod > 0.0:
        unit_number = p_nom/p_nom_mod
        max_units = p_nom_max/p_nom_mod
        min_units = p_nom_min/p_nom_mod
        capacity = p_nom_mod
    elif p_nom > 0.0:
        unit_number = 1
        max_units = round(p_nom_max/p_nom,1)
        min_units = round(p_nom_min/p_nom,1)
        capacity = p_nom
    else:
        unit_number = 0
        min_units = 0
        max_units = p_nom_max
        capacity = settings["Default_module_capacity"]
    
    for alt_ent_class_relationship in alt_ent_class_relationship_list:
        target_db = ines_transform.add_item_to_DB(target_db, 'capacity', alt_ent_class_relationship, capacity, value_type=True)
    target_db = ines_transform.add_item_to_DB(target_db, 'units_existing', alt_ent_class_unit, unit_number, value_type=True)
    if min_units > 0.0:
        target_db = ines_transform.add_item_to_DB(target_db, 'units_min_cumulative', alt_ent_class_unit, min_units, value_type=True)

    if p_nom_extendable:
        if isinstance(p_nom_max,float) and p_nom_max != math.inf:
            value = 'cumulative_limits'
            target_db = ines_transform.add_item_to_DB(target_db, 'units_max_cumulative', alt_ent_class_unit, max_units, value_type=True)
        else:
            value = 'no_limits'
    else:
        value = 'not_allowed'
    target_db = ines_transform.add_item_to_DB(target_db, 'investment_method', alt_ent_class_unit, value=value, value_type=True)

    return target_db

def add_storage_capacities(source_db, target_db, alt_ent_class_source, alt_ent_class_target):
    
    max_hours = ines_transform.get_parameter_from_DB(source_db, 'max_hours', alt_ent_class_source)
    p_nom = ines_transform.get_parameter_from_DB(source_db, 'p_nom', alt_ent_class_source)
    p_nom_max = ines_transform.get_parameter_from_DB(source_db, 'p_nom_max', alt_ent_class_source)
    p_nom_min = ines_transform.get_parameter_from_DB(source_db, 'p_nom_min', alt_ent_class_source)
    p_nom_mod = ines_transform.get_parameter_from_DB(source_db, 'p_nom_mod', alt_ent_class_source)
    p_nom_extendable = ines_transform.get_parameter_from_DB(source_db,"p_nom_extendable", alt_ent_class_source)

    if p_nom_mod > 0.0:
        unit_number = p_nom/p_nom_mod
        max_units = p_nom_max/p_nom_mod
        min_units = p_nom_min/p_nom_mod
        capacity = max_hours * p_nom_mod
    elif p_nom > 0.0:
        unit_number = 1
        max_units = round(p_nom_max/p_nom,1)
        min_units = round(p_nom_min/p_nom,1)
        capacity = max_hours * p_nom
    else:
        unit_number = 0
        min_units = 0
        max_units = p_nom_max
        capacity = settings["Default_module_capacity"]
    target_db = ines_transform.add_item_to_DB(target_db, 'storage_capacity', alt_ent_class_target, capacity, value_type=True)
    target_db = ines_transform.add_item_to_DB(target_db, 'storages_existing', alt_ent_class_target, unit_number, value_type=True)
    if min_units > 0.0:
        target_db = ines_transform.add_item_to_DB(target_db, 'storages_min_cumulative', alt_ent_class_target, min_units, value_type=True)

    if p_nom_extendable:
        if isinstance(p_nom_max,float) and p_nom_max != math.inf:
            value = 'cumulative_limits'
            target_db = ines_transform.add_item_to_DB(target_db, 'storages_max_cumulative', alt_ent_class_target, max_units, value_type=True)
        else:
            value = 'no_limits'
    else:
        value = 'not_allowed'
    target_db = ines_transform.add_item_to_DB(target_db, 'storage_investment_method', alt_ent_class_target, value=value, value_type=True)

    return target_db


#these do them for all generators, links and stores
def add_generator_modified_parameters(source_db, target_db):
    for source_entity in source_db.get_entity_items(entity_class_name='Generator'):
        alt_ent_class_source = [settings["Alternative"], source_entity["entity_byname"],'Generator']
        alt_ent_class_unit = [settings["Alternative"], (source_entity["name"],),'unit']

        bus = ines_transform.get_parameter_from_DB(source_db, 'bus', alt_ent_class_source)
        alt_ent_class_relationship = [settings["Alternative"], (source_entity["name"],bus),'unit__to_node']

        upper_limit = ines_transform.get_parameter_from_DB(source_db, 'p_max_pu', alt_ent_class_source)
        lower_limit = ines_transform.get_parameter_from_DB(source_db, 'p_min_pu', alt_ent_class_source)
        if isinstance(upper_limit, api.TimeSeriesVariableResolution) or (isinstance(upper_limit, float) and upper_limit != 1.0):
            target_db = ines_transform.add_item_to_DB(target_db, 'profile_limit_upper', alt_ent_class_relationship, upper_limit, value_type=True)
        if isinstance(lower_limit, api.TimeSeriesVariableResolution) or (isinstance(lower_limit, float) and lower_limit > 0.0):
            target_db = ines_transform.add_item_to_DB(target_db, 'profile_limit_lower', alt_ent_class_relationship, lower_limit, value_type=True)

        target_db = add_unit_capacities(source_db, target_db, alt_ent_class_source, alt_ent_class_unit,[alt_ent_class_relationship])
        target_db = add_lifetime(source_db, target_db, alt_ent_class_source, alt_ent_class_unit)
        target_db = ines_transform.add_item_to_DB(target_db, "interest_rate", alt_ent_class_unit, settings["Interest_rate"], value_type=True)
        target_db = calculate_investment_cost(source_db, target_db, alt_ent_class_source, alt_ent_class_relationship)
        target_db = add_ramps(source_db,target_db, alt_ent_class_source, [alt_ent_class_relationship])
        
    return target_db

def add_line_capacities_and_lifetimes(source_db, target_db):
    for source_entity in source_db.get_entity_items(entity_class_name='Line'):
        alt_ent_class_source = [settings["Alternative"], source_entity["entity_byname"],'Line']
        alt_ent_class_target = [settings["Alternative"], (source_entity["name"],),'link']

        s_nom = ines_transform.get_parameter_from_DB(source_db, 's_nom', alt_ent_class_source)
        s_nom_max = ines_transform.get_parameter_from_DB(source_db, 's_nom_max', alt_ent_class_source)
        s_nom_min = ines_transform.get_parameter_from_DB(source_db, 's_nom_min', alt_ent_class_source)
        s_nom_mod = ines_transform.get_parameter_from_DB(source_db, 's_nom_mod', alt_ent_class_source)
        s_nom_extendable = ines_transform.get_parameter_from_DB(source_db,"s_nom_extendable", alt_ent_class_source)

        if s_nom_mod > 0:
            unit_number = s_nom/s_nom_mod
            max_units = s_nom_max/s_nom_mod
            min_units = s_nom_min/s_nom_mod
            capacity = s_nom_mod
        elif s_nom > 0.0:
            unit_number = 1
            max_units = round(s_nom_max/s_nom,1)
            min_units = round(s_nom_min/s_nom,1)
            capacity = s_nom
        else:
            unit_number = 0
            min_units = 0
            max_units = s_nom_max
            capacity = settings["Default_module_capacity"]

        target_db = ines_transform.add_item_to_DB(target_db, 'capacity', alt_ent_class_target, capacity, value_type=True)
        target_db = ines_transform.add_item_to_DB(target_db, 'links_existing', alt_ent_class_target, unit_number, value_type=True)
        if min_units > 0.0:
            target_db = ines_transform.add_item_to_DB(target_db, 'links_min_cumulative', alt_ent_class_target, min_units, value_type=True)
        
        if s_nom_extendable:
            if isinstance(s_nom_max,float) and s_nom_max != math.inf:
                value = 'cumulative_limits'
                target_db = ines_transform.add_item_to_DB(target_db, 'links_max_cumulative', alt_ent_class_target, max_units, value_type=True)
            else:
                value = 'no_limits'
        else:    
            value = 'not_allowed'
        target_db = ines_transform.add_item_to_DB(target_db, 'investment_method', alt_ent_class_target, value=value, value_type=True)

        target_db = add_lifetime(source_db, target_db, alt_ent_class_source, alt_ent_class_target)
        target_db = ines_transform.add_item_to_DB(target_db, "interest_rate", alt_ent_class_target, settings["Interest_rate"], value_type=True)
        target_db = calculate_investment_cost(source_db, target_db, alt_ent_class_source, alt_ent_class_target)
    
    return target_db

def add_store_capacities_and_lifetimes(source_db, target_db):
    for source_entity in source_db.get_entity_items(entity_class_name='Store'):
        alt_ent_class_source = [settings["Alternative"], source_entity["entity_byname"],'Store']
        alt_ent_class_target = [settings["Alternative"], (source_entity["name"],),'node']

        e_nom = ines_transform.get_parameter_from_DB(source_db, 'e_nom', alt_ent_class_source)
        e_nom_max = ines_transform.get_parameter_from_DB(source_db, 'e_nom_max', alt_ent_class_source)
        e_nom_min = ines_transform.get_parameter_from_DB(source_db, 'e_nom_min', alt_ent_class_source)
        e_nom_mod = ines_transform.get_parameter_from_DB(source_db, 'e_nom_mod', alt_ent_class_source)
        e_nom_extendable = ines_transform.get_parameter_from_DB(source_db,"e_nom_extendable", alt_ent_class_source)

        if e_nom_mod > 0:
            unit_number = e_nom/e_nom_mod
            max_units = e_nom_max/e_nom_mod
            min_units = e_nom_min/e_nom_mod
            capacity = e_nom_mod
        elif e_nom > 0.0:
            unit_number = 1
            max_units = round(e_nom_max/e_nom,1)
            min_units = round(e_nom_min/e_nom,1)
            capacity = e_nom
        else:
            unit_number = 0
            min_units = 0
            max_units = e_nom_max
            capacity = settings["Default_module_capacity"]


        target_db = ines_transform.add_item_to_DB(target_db, 'storage_capacity', alt_ent_class_target, capacity, value_type=True)
        target_db = ines_transform.add_item_to_DB(target_db, 'storages_existing', alt_ent_class_target, unit_number, value_type=True)
        if min_units > 0.0:
            target_db = ines_transform.add_item_to_DB(target_db, 'storages_min_cumulative', alt_ent_class_target, min_units, value_type=True)
        
        if e_nom_extendable and e_nom_max != math.inf:
            if isinstance(e_nom_max,float):
                value = 'cumulative_limits'
                target_db = ines_transform.add_item_to_DB(target_db, 'storages_max_cumulative', alt_ent_class_target, max_units, value_type=True)
            else:
                value = 'no_limits'
        else:    
            value = 'not_allowed'
        target_db = ines_transform.add_item_to_DB(target_db, 'storage_investment_method', alt_ent_class_target, value=value, value_type=True)

        target_db = add_lifetime(source_db, target_db, alt_ent_class_source, alt_ent_class_target, storage = True)
        target_db = ines_transform.add_item_to_DB(target_db, "storage_interest_rate", alt_ent_class_target, settings["Interest_rate"], value_type=True)
        target_db = calculate_investment_cost(source_db, target_db, alt_ent_class_source, alt_ent_class_target, storage = True)
    
    return target_db

# Binds investments for entities like storage and its input and output
# All of the cost is added to only one of the entities, for others the investment cost is zero
def bind_investments(source_db, target_db, alt_ent_class_source, investment_binding_map, storage = False):
    if storage:
        name_list =[]
        directions = []
        for i in investment_binding_map.keys():
            if i != "storage":
                directions.append(i)
        for direction in directions:
            constraint_name = alt_ent_class_source[1][0] + "_investment_bind_"+ direction
            name_list.append(constraint_name)
            alt_ent_class_constraint = [settings["Alternative"],(constraint_name,),'constraint']
            asd
            ines_transform.assert_success(target_db.add_entity_item(
                                entity_class_name="constraint",
                                entity_byname=(constraint_name,),
                            ), warn=True)
            target_db = ines_transform.add_item_to_DB(target_db, "constant", alt_ent_class_constraint, value=0, value_type=True)
            target_db = ines_transform.add_item_to_DB(target_db, "sense", alt_ent_class_constraint, value="equal", value_type=True)
            for key, data in investment_binding_map.items():
                if key == direction:
                    output_map = api.Map([constraint_name], [data[1]], index_name="constraint")
                    target_db = ines_transform.add_item_to_DB(target_db, "constraint_unit_count_coefficient", data[0], value = output_map, value_type=True)
        for key, data in investment_binding_map.items():
            if key == "storage":
                output_map = api.Map(name_list, [data[1] for i in name_list], index_name="constraint")
                target_db = ines_transform.add_item_to_DB(target_db, "constraint_storage_count_coefficient", data[0], value = output_map, value_type=True)
    else:
        constraint_name = alt_ent_class_source[1][0] + "_investment_bind"
        alt_ent_class_constraint = [settings["Alternative"],(constraint_name,),'constraint']
        ines_transform.assert_success(target_db.add_entity_item(
                            entity_class_name="constraint",
                            entity_byname=(constraint_name,),
                        ), warn=True)
        target_db = ines_transform.add_item_to_DB(target_db, "constant", alt_ent_class_constraint, value=0, value_type=True)
        target_db = ines_transform.add_item_to_DB(target_db, "sense", alt_ent_class_constraint, value="equal", value_type=True)

        for key, data in investment_binding_map.items():
            output_map = api.Map([constraint_name], [data[1]], index_name="constraint")
            target_db = ines_transform.add_item_to_DB(target_db, "constraint_unit_count_coefficient", data[0], value = output_map, value_type=True)
    
    return target_db

def add_lifetime(source_db, target_db, alt_ent_class_source, alt_ent_class_target, storage = False):
    target_param = 'lifetime'
    method_param = 'retirement_method'
    if storage:
        target_param = 'storage_lifetime'
        method_param = 'storage_retirement_method'
    source_lifetime = ines_transform.get_parameter_from_DB(source_db, 'lifetime', alt_ent_class_source)
    if source_lifetime and isinstance(source_lifetime, float) and source_lifetime != math.inf:
        target_db = ines_transform.add_item_to_DB(target_db, target_param, alt_ent_class_target, value=source_lifetime, value_type=True)
        target_db = ines_transform.add_item_to_DB(target_db, method_param, alt_ent_class_target, value='retire_as_scheduled', value_type=True)
    if source_lifetime and isinstance(source_lifetime, float) and source_lifetime == math.inf:
        target_db = ines_transform.add_item_to_DB(target_db, target_param, alt_ent_class_target, value=settings["Infinite_lifetime"], value_type=True)
        target_db = ines_transform.add_item_to_DB(target_db, method_param, alt_ent_class_target, value='retire_as_scheduled', value_type=True)
    else:
        target_db = ines_transform.add_item_to_DB(target_db, method_param, alt_ent_class_target, value='not_retired', value_type=True)
    
    return target_db

def add_ramps(source_db,target_db, alt_ent_class_source, alt_ent_class_relationship_list):
    ramp_limit_down = ines_transform.get_parameter_from_DB(source_db, "ramp_limit_down", alt_ent_class_source)
    ramp_limit_up = ines_transform.get_parameter_from_DB(source_db, "ramp_limit_up", alt_ent_class_source)
    for alt_ent_class_relationship in alt_ent_class_relationship_list:
        if (not ramp_limit_down and not ramp_limit_up) or (math.isnan(ramp_limit_down) and math.isnan(ramp_limit_up)):
            target_db = ines_transform.add_item_to_DB(target_db, "ramp_method", alt_ent_class_relationship, "no_constraint", value_type=True)
        else:
            target_db = ines_transform.add_item_to_DB(target_db, "ramp_method", alt_ent_class_relationship, "ramp_limit", value_type=True)
            if ramp_limit_up and not math.isnan(ramp_limit_up):
                target_db = ines_transform.add_item_to_DB(target_db, "ramp_limit_up", alt_ent_class_relationship, ramp_limit_up, value_type=True)
            if ramp_limit_down and not math.isnan(ramp_limit_down):
                target_db = ines_transform.add_item_to_DB(target_db, "ramp_limit_down", alt_ent_class_relationship, ramp_limit_down, value_type=True)
    
    return target_db        

def add_profile_methods(target_db):
    for classes in ["unit__to_node","node__to_unit"]:
        for source_entity in target_db.get_entity_items(entity_class_name=classes):
            alt_ent_class_target = [settings["Alternative"], source_entity["entity_byname"], classes]
            upper_limit = ines_transform.get_parameter_from_DB(target_db, 'profile_limit_upper', alt_ent_class_target)
            lower_limit = ines_transform.get_parameter_from_DB(target_db, 'profile_limit_lower', alt_ent_class_target)
            upper = False
            lower = False
            if isinstance(upper_limit, api.TimeSeriesVariableResolution) or (isinstance(upper_limit, float) and upper_limit != 1.0):
                upper = True
            if isinstance(lower_limit, api.TimeSeriesVariableResolution) or (isinstance(lower_limit, float) and lower_limit > 0.0):
                lower = True
            if upper and lower:
                profile_method = 'upper_and_lower_limit'
            elif upper:
                profile_method = 'upper_limit'
            elif lower:
                profile_method = 'lower_limit'
            else:
                profile_method = 'no_profile'

            target_db = ines_transform.add_item_to_DB(target_db, "profile_method", alt_ent_class_target, profile_method, value_type=True) 
    return target_db

def add_inflow_methods_and_state_fix(target_db):
    for target_entity in target_db.get_entity_items(entity_class_name='node'): 
        alt_ent_class_source = [settings["Alternative"], target_entity["entity_byname"], 'node']
        flow_profile = ines_transform.get_parameter_from_DB(target_db, "flow_profile",alt_ent_class_source)
        flow_scaling_method = "no_inflow" 
        if isinstance(flow_profile,api.TimeSeriesVariableResolution):
            flow_scaling_method = "use_profile_directly"
        elif isinstance(flow_profile,float):
            if flow_profile > 0.0:
                flow_scaling_method = "use_profile_directly"
        target_db = ines_transform.add_item_to_DB(target_db, "flow_scaling_method", alt_ent_class_source, flow_scaling_method, value_type=True)

        fix_limit = ines_transform.get_parameter_from_DB(target_db, "storage_state_fix", alt_ent_class_source)
        upper_limit = ines_transform.get_parameter_from_DB(target_db, "storage_state_upper_limit", alt_ent_class_source)
        lower_limit = ines_transform.get_parameter_from_DB(target_db, "storage_state_lower_limit", alt_ent_class_source)
        fix = False
        upper = False
        lower = False
        if isinstance(fix_limit, api.TimeSeriesVariableResolution) or (isinstance(fix_limit, float) and fix_limit != 1.0):
            fix = True
        if isinstance(upper_limit, api.TimeSeriesVariableResolution) or (isinstance(upper_limit, float) and upper_limit != 1.0):
            upper = True
        if isinstance(lower_limit, api.TimeSeriesVariableResolution) or (isinstance(lower_limit, float) and lower_limit != 1.0):
            lower = True
        if fix:
            profile_method = 'fixed'
        elif upper and lower:
            profile_method = 'upper_and_lower_limit'
        elif upper:
            profile_method = 'upper_limit'
        elif lower:
            profile_method = 'lower_limit'
        else:
            profile_method = 'no_profile'
        target_db = ines_transform.add_item_to_DB(target_db, "storage_limit_method", alt_ent_class_source, profile_method, value_type=True)
    return target_db

#This is slightly problematic. Both investment cost and interest rate produced are wrong, but combinend they will produce the correct annuity
def calculate_investment_cost(source_db, target_db, alt_ent_class_source, alt_ent_class_target, storage = False, max_hours = None):
    capital_cost = ines_transform.get_parameter_from_DB(source_db, "capital_cost", alt_ent_class_source)
    if max_hours: 
        capital_cost = capital_cost * 1/max_hours
    lifetime = ines_transform.get_parameter_from_DB(source_db, "lifetime", alt_ent_class_source)
    if lifetime and isinstance(lifetime, float) and lifetime == math.inf:
        lifetime = settings["Infinite_lifetime"]
    r = settings["Interest_rate"]
    investment_cost = capital_cost * r /(1- 1 /((1+r)**lifetime))
    if storage:
        target_db = ines_transform.add_item_to_DB(target_db, "storage_investment_cost", alt_ent_class_target, investment_cost, value_type=True)
    else: 
        target_db = ines_transform.add_item_to_DB(target_db, "investment_cost", alt_ent_class_target, investment_cost, value_type=True)

    return target_db
    
def add_node_types(target_db):
    for target_entity in target_db.get_entity_items(entity_class_name='node'): 
        alt_ent_class_source = [settings["Alternative"], target_entity["entity_byname"], 'node']
        node_type = ines_transform.get_parameter_from_DB(target_db, "node_type",alt_ent_class_source)
        if not node_type:
            target_db = ines_transform.add_item_to_DB(target_db, "node_type", alt_ent_class_source, "balance", value_type=True)

    return target_db

def add_entity_alternative_items(target_db):
    for entity_class in target_db.get_entity_class_items():
        for entity in target_db.get_entity_items(entity_class_name=entity_class["name"]): 
            ines_transform.assert_success(target_db.add_update_entity_alternative_item(
                entity_class_name=entity_class["name"],
                entity_byname=entity["entity_byname"],
                alternative_name=settings["Alternative"],
                active=True,
            ))
    return target_db

#PyPSA earth produces same name entities for bus, store, generator and link for example in csp...
def change_same_name_entities(target_db):
    entity_name_list = []
    for entity_class in target_db.get_entity_class_items():
        for entity in target_db.get_entity_items(entity_class_name=entity_class["name"]): 
            if entity["name"] in entity_name_list:
                target_db.update_item(
                    "entity", id=entity["id"], name=entity["name"]+"_"+entity_class["name"]
                )
            else:
                entity_name_list.append(entity["name"])
    return target_db
    
if __name__ == "__main__":
    developer_mode = False
    if developer_mode:
        # save entities to yaml file
        save_folder = os.path.dirname(__file__)
        conversion_configuration(conversions = [save_folder+'/pypsa_to_ines_entities.yaml', save_folder+'/pypsa_to_ines_parameters.yaml', save_folder+'/pypsa_to_ines_parameter_methods.yaml',
                                             save_folder+'/pypsa_to_ines_parameters_to_relationships.yaml'], overwrite=True)
    else:
        # assume the file to be used inside of Spine Toolbox
        url_db_in = sys.argv[1]
        url_db_out = sys.argv[2]
        # open yaml files
        entities_to_copy,parameter_transforms,parameter_methods, parameters_to_relationships, parameters_to_parameters = conversion_configuration()
        settings_path = 'pypsa_to_ines_settings.yaml'
        with open(settings_path,'r') as file:
            settings = yaml.safe_load(file) 

        main()