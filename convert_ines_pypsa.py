def map_preprocess(iodb):
	# instead choose an intermediate format that is closer to spine?
	# e.g. [] instead of {}?
	iodb = iodb.update({
			"Bus" : {},
			"Generator" : {},
			"Link" : {},
			"Load" : {}
		})
	return

def map_postprocess(iodb):
	#filter out None values
	for (entitytype,entities) in iodb.items():
		for (entityname,entitityattributes) in entities.items():
			deleteitems = []
			for (attribute,value) in entitityattributes.items():
				if value == None:
					deleteitems.append((entitytype,entityname,attribute))
	for (entitytype,entityname,attribute) in deleteitems:
		iodb[entitytype][entityname].pop(attribute)
	return

# Function map for entity classes; specific to the generic structure
def map_constraint(iodb,entities,parameters):
	return

def map_link(iodb,entities,parameters):
	entityname = entities[0]
	parameter = parameters[0]
	if entityname not in iodb["Link"]:
		iodb["Link"][entityname] = {}
	iodb["Link"][entityname].update({
		"efficiency" : 1,
		"marginal_cost" : 0.0,
		"p_min_pu" : -1,
		"p_nom" : parameter["capacity"]
	})
	return

def map_node(iodb,entities,parameters):
	# can be a node, load or source
	entityname = entities[0]
	parameter = parameters[0]
	if entityname not in iodb["Bus"]:
		iodb["Bus"][entityname] = {}
	if "load "+entityname not in iodb["Load"]:
		iodb["Load"]["load "+entityname] = {}
	iodb["Load"]["load "+entityname].update({
		"bus" : entityname,
		"p_set" : parameter["flow_profile"]
	})
	if "generator "+entityname not in iodb["Generator"]:
		iodb["Generator"]["generator "+entityname] = {}
	iodb["Generator"]["generator "+entityname].update({
		"bus" : entityname,
		"marginal_cost" : parameter["commodity_price"],
		#"p_nom" : parameter["profile_limit_upper"]#needs to be translated differently?
	})
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
	entityname = entities[0]
	parameter = parameters[0]

	if entityname not in iodb["Link"]:
		iodb["Link"][entityname] = {}
	iodb["Link"][entityname].update({
		"efficiency" : parameter["efficiency"]
	})
	return

def map_node__to_unit(iodb,entities,parameters):
	entityname = entities[1]
	parameter = parameters[0]
	if entityname not in iodb["Link"]:
		iodb["Link"][entityname] = {}
	iodb["Link"][entityname].update({
		"efficiency" : parameter["conversion_coefficient"],
		"marginal_cost" : parameter["other_operational_cost"],
		"p_nom" : parameter["capacity"]
	})
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
	entityname = entities[1]
	busname = entities[2]
	parameter = parameters[0]

	if entityname not in iodb["Link"]:
		iodb["Link"][entityname] = {}
	iodb["Link"][entityname].update({
		"bus1" : busname,
		"marginal_cost" : parameter["other_operational_cost"],
		"p_nom" : parameter["capacity"]
	})
	return

def map_node__link__node(iodb,entities,parameters):
	entityname = entities[0]
	busname0 = entities[1]
	busname1 = entities[3]

	if entityname not in iodb["Link"]:
		iodb["Link"][entityname] = {}
	iodb["Link"][entityname].update({
		"bus0" : busname0,
		"bus1" : busname1
	})
	return

def map_set__node__temporality(iodb,entities,parameters):
	return

def map_set__node__unit(iodb,entities,parameters):
	entityname = entities[2]
	busname = entities[1]

	if entityname not in iodb["Link"]:
		iodb["Link"][entityname] = {}
	iodb["Link"][entityname].update({
		"bus1" : busname
	})
	return