# ines-pypsa
Translation between the ines specification and the PyPSA structure

## use
Make a workflow in Spine Toolbox with the following elements:
+ data connection for inputfile in nc format
+ tool for conversion from pypsa to spine
    + main file = pypsa_to_spine.py
+ database for pypsa data in spine format
+ tool for conversion from pypsa to ines
    + main file = pypsa_to_ines.py
    + auxiliary file = ines transform from the ines-tools repository
+ database for pypsa data in ines format

## development
Currently only general framework from PyPSA to ines. The next steps are to fill in the yaml files and create specific functions for specific conversions.

+ Fill in the yaml files for the quick conversions
+ add specific functions for specific conversions
+ main() is general to any conversion script so it can to move to ines transform
+ the yaml conversion function also allows to put the conversion directly in a python format such that in principle the yaml files are not needed and there is only need for the pypsa_to_ines.py file
+ the yaml conversion function can be rewritten such that it is independent of the data and as such can also move to ines_transform. The idea is that a new developer for a conversion script only needs to add functions or dictionaries to a python file that calls the main and other functions.
+ convert_ines_pypsa.py and convert_pypsa_ines.py are to be deleted. However, currently they are kept because the migration is not yet complete and there is still some useful code there.
+ certify_pypsa.py and run_pypsa_in_spinetools.py need to be updated or removed.
