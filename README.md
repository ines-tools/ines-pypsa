# ines-pypsa
Translation between the ines specification and the PyPSA structure

## use
Get spine-toolbox "https://github.com/spine-tools/Spine-Toolbox", follow its install instructions
Get ines-tools repository "https://github.com/energy-modelling-workbench/ines-tools" and install it "pip install ." 

Open folder spine_workflow as a spine project. 
It contains 5 elements:
+ 1. pypsa_nc: data connection to inputfile in nc format (change the path to your file)
+ 2. pypsa_to_spine_transform: tool for conversion from pypsa to spine
    + main file = pypsa_to_spine.py
+ 3. database for pypsa data in spine format
    + main file = pypsa_db_template.sqlite
+ 4. pypsa_to_ines_transform: tool for conversion from pypsa to ines 
    + main file = pypsa_to_ines.py
    + additional file = pypsa_to_ines_settings.yaml
+ 5. database for pypsa data in ines format
    + main file = ines_spec_db_template.sqlite

Alternatively you can create your own workflow and add these elements there, with the possible transform to another format from ines.

Currently only PyPSA -> Ines direction is functional.

## Information lost in the format transfromation

### Voltage and frequency
Currently only the energy flow is implemented in the INES-spec. So all parameters related to voltage, angle, reactive power ect. are excluded.
This includes also entities: 
 - LineType
 - ShuntImpedance
 - Transformer
 - TransformerType

### Investments:
- PyPSA uses the annuity ('capital_cost') directly but the INES format uses investment cost and interest rate. Calculating these both is not possible from the annuity. Instead the interest rate is assumed. This is set in the settings file 'pypsa_to_ines_settings.yaml'. The investment_cost can then be caluculated. This means that both of these values individually are wrong and should not be used, but they can be used together to calculate the same annuity.
- INES format requires a module capacity if investments are allowed. If it is not set, a default value from the settings file is used 

### Lifetime
- Infinite lifetimes are transformed to a number set in the settings file 'pypsa_to_ines_settings.yaml'

### List of parameters not implemented:
- GlobalConstraint:
    - Completely missing
- Bus: 
    - 'x': coordinates not planned to implement
    - 'y': coordinates not planned to implement
    - 'v_mag_pu_set'
    - 'v_mag_pu_min'    placeholder
    - 'v_mag_pu_max'    placeholder
    - 'v_nom'
- Carrier
    - co2_emmissions: not in ines format yet
    - max_relative_growth: not in ines format yet
- Generator
    - 'build_year'
    - 'control'
    - 'down_time_before: hot start not in ines
    - 'e_sum_max: max production of energy in period
    - 'e_sum_min: min production of energy in period
    - 'marginal_cost_quadratic'
    - 'min_down_time' not in ines format yet
    - 'min_up_time' not in ines format yet
    - 'p_set'
    - 'q_set'
    - 'ramp_limit_shut_down': not in ines format yet
    - 'ramp_limit_start_up: not in ines format yet
    - 'stand_by_cost': not in ines format yet
    - 'up_time_before': not in ines format yet
    - 'weight': #for network clustering
- Line
    - 'b'
    - 'build_year'
    - 'g'
    - 'length', not in ines
    - 'num_parallel'
    - 'r'
    - 'terrain_factor'
    - 'type': ready types of lines
    - 'v_ang_max':  placeholder
    - 'v_ang_min':  placeholder
    - 'x':
- Link
    - 'build_year'
    - 'control'
    - 'down_time_before: hot start not in ines
    - 'e_sum_max: max production of energy in period
    - 'e_sum_min: min production of energy in period
    - 'marginal_cost_quadratic'
    - 'min_down_time': not in ines format yet
    - 'min_up_time': not in ines format yet
    - 'p_set'
    - 'q_set'
    - 'ramp_limit_shut_down': not in ines format yet
    - 'ramp_limit_start_up: not in ines format yet
    - 'sign' power_flow stuff
    - 'stand_by_cost': not in ines format yet
    - 'terrain_factor'
    - 'up_time_before': not in ines format yet
- Load
    - 'p_set'
    - 'q_set'
    - 'sign'
    - 'type' placeholder
- StorageUnit:
    - 'build_year'
    - 'control'
    - 'cyclic_state_of_charge'
    - 'cyclic_state_of_charge_per_period'
    - 'marginal_cost_quadratic'
    - 'marginal_cost_storage'
    - 'p_set'
    - 'q_set'
    - 'sign'
    - 'standing_loss'
    - 'state_of_charge_initial'
    - 'state_of_charge_initial_per_period'
    - 'type'  placeholder
- Store
    - 'build_year'
    - 'control'
    - 'e_cyclic'
    - 'e_cyclic_per_period'
    - 'e_initial'
    - 'e_initial_per_period'
    - 'marginal_cost'
    - 'marginal_cost_quadratic'
    - 'marginal_cost_storage'
    - 'p_set'
    - 'q_set'
    - 'sign'
    - 'standing_loss'

## development
Currently only first version from PyPSA format to INES. Testing is still needed.
+ Add multi-input/output links
+ Add units also between market nodes and stores with 'marginal cost'
+ Parameters from the missing parameters list can be included if they are addded to the ines spec
+ main() is general to any conversion script so it can to move to ines transform
+ remove yaml files excluding the setting file
+ convert_ines_pypsa.py and convert_pypsa_ines.py are to be deleted. However, currently they are kept because the migration is not yet complete and there is still some useful code there.
+ certify_pypsa.py and run_pypsa_in_spinetools.py need to be updated or removed.
