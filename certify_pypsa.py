# this is a temporary file, it should instead be executed like the main translation accompanied with a conversion file

import sys
import json
import spinedb_api as api

tool_file = sys.argv[1]
if len(sys.argv)>2:
    collection = sys.argv[2]
else:
    from pathlib import Path
    path = Path(__file__).resolve().parent
    collection = str(path.joinpath("data/collected_results.json"))

# load results
with open(tool_file) as tf:
    tool_data = json.load(tf)

format_data={
    "entities" : [
        [
            "tool",
            "PyPSA",
            None
        ]
    ],
    "parameter_values" : [
        [
            "tool",
            "PyPSA",
            "time",
            tool_data["time"],
            "PyPSA"
        ],
        [
            "tool",
            "PyPSA",
            "objective value",
            tool_data["objective"],
            "PyPSA"
        ],
        [
            "tool",
            "PyPSA",
            "number of variables",
            tool_data["#variables"],
            "PyPSA"
        ],
        [
            "tool",
            "PyPSA",
            "number of constraints",
            tool_data["#constraints"],
            "PyPSA"
        ]
    ],
    "alternatives" : [
        [
            "PyPSA",
            ""
        ]
    ]
}

# store results in database with ines-certify format
with api.DatabaseMapping(collection) as target_db:
    api.import_data(target_db,**format_data)
    target_db.commit_session("import data")