def map_preprocess(iodb):
	# instead choose an intermediate format that is closer to spine?
	# e.g. [] instead of {}?
	iodb = iodb | {
			"Bus" : {},
			"Generator" : {},
			"Link" : {},
			"Load" : {}
		}
	return

def map_postprocess(iodb):
	#filter out None values
	for (entitytype,entities) in iodb.items():
		for (entityname,entitityattributes) in entities.items():
			for (attribute,value) in entitityattributes.items():
				if value == None:
					iodb[entitytype][entityname].pop(attribute)
	return

# Function map for entity classes; specific to the generic structure
def map_constraint(iodb,entities,parameters):
	return

def map_link(iodb,entities,parameters):
	entityname = entities[1]
	parameter = parameters[1]
	iodb["Link"] = iodb["Link"].get(entityname,{}) | {
		"efficiency" : 1,
		"marginal_cost" : 0.0,
		"p_min_pu" : -1,
		"p_nom" : parameter["capacity"]
	}
	return

def map_node(iodb,entities,parameters):
	# can be a node, load or source
	entityname = entities[1]
	parameter = parameters[1]
	iodb["Bus"].get(entityname,{})
	iodb["Load"] = iodb["Load"].get("load "+entityname, {}) | {
		"bus" : entityname,
		"p_set" : parameter["demand_profile"]
	}
	iodb["Generator"] = iodb["Generator"].get("generator "+entityname, {}) | {
		"bus" : entityname,
		"marginal_cost" : parameter["commodity_price"],
		"p_nom" : parameter["upper_limit"]
	}
	return

def map_period(iodb,entities,parameters):
	return

def map_set(iodb,entities,parameters):
	return

def map_solve_pattern(iodb,entities,parameters):
	return

def map_system(iodb,entities,parameters):
	return

def map_temporality(iodb,entities,parameters):
	return

def map_tool(iodb,entities,parameters):
	return

def map_unit(iodb,entities,parameters):
	entityname = entities[1]
	parameter = parameters[1]

	iodb["Link"] = iodb["Link"].get(entityname, {}) | {
		"efficiency" : parameter["efficiency"]
	}
	return

def map_node__to_unit(iodb,entities,parameters):
	entityname = entities[2]
	parameter = parameters[1]
	iodb["Link"] = iodb["Link"].get(entityname, {}) | {
		"efficiency" : parameter["conversion_coefficient"],
		"marginal_cost" : parameter["other_operational_cost"],
		"p_nom" : parameter["capacity_per_unit"]
	}
	return

def map_set__link(iodb,entities,parameters):
	return

def map_set_node(iodb,entities,parameters):
	return

def map_set_temporality(iodb,entities,parameters):
	return

def map_set__unit(iodb,entities,parameters):
	return

def map_tool_set(iodb,entities,parameters):
	return

def map_unit__to_node(iodb,entities,parameters):
	entityname = entities[2]
	busname = entities[3]
	parameter = parameters[1]
	iodb["Link"] = iodb["Link"].get(entityname, {}) | {
		"bus1" : busname,
		"marginal_cost" : parameter["other_operational_cost"],
		"p_nom" : parameter["capacity_per_unit"]
	}
	return

def map_node__link__node(iodb,entities,parameters):
	entityname = entities[1]
	busname0 = entities[2]
	busname1 = entities[4]

	iodb["Link"] = iodb["Link"].get(entityname, {}) | {
		"bus0" : busname0,
		"bus1" : busname1
	}
	return

def map_set__node__temporality(iodb,entities,parameters):
	return

def map_set__node__unit(iodb,entities,parameters):
	entityname = entities[3]
	busname = entities[2]

	iodb["Link"] = iodb["Link"].get(entityname, {}) | {
		"bus1" : busname
	}
	return