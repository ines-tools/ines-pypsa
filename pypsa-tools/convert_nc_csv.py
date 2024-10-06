import sys
import pypsa

ARGS = sys.argv[1:]
inputpath = ARGS[0] # nc file
outputpath = ARGS[1] # folder for csv files

n = pypsa.Network(inputpath)

n.export_to_csv_folder(outputpath)