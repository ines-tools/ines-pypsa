import sys
import pypsa
import spinedb_api as api
from spinedb_api import purge

'''
Conversion script to convert a nc file with PyPSA data to a spine database.

There are a lot of possible ways to structure the data in the spine database. Here, we choose for a quite literal map (following the tables from https://pypsa.readthedocs.io/en/latest/user-guide/components.html) as it makes the conversion scripts easier and PyPSA is not designed to directly make use of the more advanced features of the spine database anyway.

The data is streamed to the database in pieces, alternatively all data could be collected in a dictionary first and then imported in its entirety.
'''

def main(input, output):
    print("load nc file")
    n = pypsa.Network(input)
    # check https://pypsa.readthedocs.io/en/latest/user-guide/components.html for the structure of n

    # Set icons for PyPSA components in spine, default is None
    iconmap = {
        #"Network":,
        #"SubNetwork":,
        "Bus":280683380142637,
        #"Carrier":,
        "GlobalConstraint":281246450775340,
        "Line":280462687073079,
        #"LineType":,
        #"Transformer":,
        #"TransformerType":,
        #"Link":,
        #"Load":,
        "Generator":280740537364493,
        #"StorageUnit":,
        #"Store":,
        #"ShuntImpedance":,
        #"Shape":,
    }

    with api.DatabaseMapping(output) as spinedb:
        # first empty the database
        purge.purge(spinedb, purge_settings=None)
        # to add parameter values we need at least one alternative defined. Here we choose PyPSA but it can be anything
        datadict = {
            "alternatives": [[
                "PyPSA",
                ""
            ]]
        }
        spinedb.refresh_session()
        api.import_data(spinedb,**datadict)

        # the for loops follow the PyPSA format
        # the datadicts follow the spine format
        for component,table in n.components.items():
            # sometimes it is good to refresh the session
            spinedb.refresh_session()

            print("define " + component + " class and parameter definitions")

            # first collect some data in a dictionary
            datadict = {
                "entity_classes":[[
                    component, # class name
                    [], # connected entities
                    table["description"], # description?
                    iconmap.get(component), # icon
                    False
                ]],
                "parameter_definitions":[],
            }
            parameters = table["attrs"].to_dict(orient='index')
            for parametername, dataframe in parameters.items():
                datadict["parameter_definitions"].append([
                    component, # entity class
                    parametername, # parameter name
                    dataframe["default"], # default value
                    None, # parameter value list
                    dataframe["description"] # description
                ])
            # then stream it to the spine database
            api.import_data(spinedb,**datadict)
            #spinedb.commit_session(component + " entity class and parameter definition")

            if hasattr(n, table["list_name"]):
                #Network has list_name in the table not actually in the data, therefore we need to check whether the attribute exists

                print("add " + component + " entities and parameter values")

                n_component = getattr(n, table["list_name"]).to_dict("index")
                # correct dictionary for empty keys
                n_component = {k: v for k, v in n_component.items() if k}
                for name,parameters in n_component.items():
                    print(name,end="\r")
                    #Exception for shape as spine cannot deal with the Polygon objects
                    if component == "Shape":
                        parameters["geometry"] = str(parameters["geometry"])
                    
                    #again first create dictionary
                    datadict = {
                        "entities":[[
                            component,
                            name,
                            None
                        ]],
                        "parameter_values":[],
                    }
                    for parametername,value in parameters.items():
                        datadict["parameter_values"].append([
                            component,
                            name,
                            parametername,
                            value,
                            "PyPSA"
                        ])
                    api.import_data(spinedb,**datadict)
                    n_component_t = getattr(n, table["list_name"]+"_t")
                    # correct dictionary for empty keys
                    n_component_t = {k: v for k, v in n_component_t.items() if k}
                    for name_t,parameters_t in n_component_t.items():
                        #print(name_t,end="\r")
                        #Exception for shape as spine cannot deal with the Polygon objects
                        if component == "Shape":
                            parameters["geometry"] = str(parameters["geometry"])
                        
                        #again first create dictionary
                        datadict_t = {
                            "entities":[[
                                component,
                                name,
                                None
                            ]],
                            "parameter_values":[],
                        }
                        #if parameters
                        for entity_name,value_t in parameters_t.items():
                            value_out = api.TimeSeriesVariableResolution(value_t.index, value_t.values, ignore_year = False, repeat=False, index_name="time step")
                            datadict_t["parameter_values"].append([
                                component,
                                name,
                                name_t,
                                value_out,
                                "PyPSA"
                            ])
                        #print(datadict) # debug line
                        # then import the data to the spine database
                        api.import_data(spinedb,**datadict_t)
            
        #add the time structure
        param = "snapshots"
        values =  getattr(n, param).tolist()
        new_values= []
        for val in values:
            #new_values.append(val.isoformat())
            new_values.append(api.DateTime(val))
        datadict = {
            "entities":[['Network', 'Time', None]],
            "parameter_values":[],
        }
        datadict["parameter_values"].append([
            'Network',
            'Time',
            param,
            api.Array(new_values),
            "PyPSA"
        ])
        #api.import_data(spinedb,**datadict)

        param = "investment_periods"
        values =  getattr(n, param).to_list()
        datadict["parameter_values"].append([
            'Network',
            'Time',
            param,
            api.Array(values),
            "PyPSA"
        ])
        #api.import_data(spinedb,**datadict)
        param = "snapshot_weightings"#, "investment_period_weightings"]
        values =  getattr(n, param).to_dict()
        name_arr = []
        in_value_arr = []
        for name, time_val in values.items():
            time_arr= []
            val_arr= []
            for time, val in time_val.items():
                time_arr.append(time.isoformat())
                val_arr.append(val)
            in_value_arr.append(api.Map(
                    time_arr,
                    val_arr,
                    index_name="snapshot"
                    ))
            name_arr.append(name)
        out_value = api.Map(
                    name_arr,
                    in_value_arr,
                    index_name="Type"
                    )
        datadict["parameter_values"].append([
            'Network',
            'Time',
            param,
            out_value,
            "PyPSA"
        ])

        param = "investment_period_weightings"
        values =  getattr(n, param).to_dict()

        name_arr = []
        in_value_arr = []
        for name, time_val in values.items():
            time_arr= []
            val_arr= []
            for time, val in time_val.items():
                time_arr.append(time)
                val_arr.append(val)
            if not time_arr:
                in_value_arr.append(None)
            else:
                in_value_arr.append(api.Map(
                        time_arr,
                        val_arr,
                        index_name="Year"
                        ))
            name_arr.append(name)
        out_value = api.Map(
                    name_arr,
                    in_value_arr,
                    index_name="Type"
                    )
        datadict["parameter_values"].append([
            'Network',
            'Time',
            param,
            out_value,
            "PyPSA"
        ])

        api.import_data(spinedb,**datadict)
        
        spinedb.commit_session(component + " entities and parameter values")

if __name__ == "__main__":
    input = sys.argv[1] # nc file
    output = sys.argv[2] # spine db
    
    main(input, output)