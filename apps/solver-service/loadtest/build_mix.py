#!/usr/bin/env python3
"""Build loadtest/mix.json from the TravelPlanner validation CSV.

Gold ConstraintSpecs are mapped mechanically from the benchmark CSV
(org/dest/days/date/people/budget/local_constraint), the same mapping the
formulation experiment used, so the load mix is the real validation
distribution (easy/medium/hard x 3/5/7 days, 1-3 cities, one infeasible query)
rather than a hand-picked light subset. The CSV is not shipped with the
service; pass its path. Every spec must pass the service's own validator.
``--timings`` (optional) is a JSON list of {queryId, mc3: {status, solveMs}}
used to pick the ``heavy`` mix (slowest 8 at maxCandidates=3).

  python loadtest/build_mix.py --csv /path/to/validation.csv [--timings corpus.json]
"""

import argparse
import ast
import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from solver_service.packs.travelplanner import spec as spec_module  # noqa: E402


def gold_spec(row):
    local = ast.literal_eval(row["local_constraint"])
    dates = ast.literal_eval(row["date"])
    vcn = int(row["visiting_city_number"])
    return {
        "origin": row["org"],
        "destination": {"type": "city" if vcn == 1 else "state", "name": row["dest"]},
        "days": int(row["days"]),
        "startDate": dates[0],
        "visitingCityNumber": vcn,
        "peopleNumber": int(row["people_number"]),
        "budget": float(row["budget"]),
        "houseRule": local.get("house rule"),
        "roomType": local.get("room type"),
        "cuisines": local.get("cuisine") or None,
        "transportation": local.get("transportation"),
        "soft": None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--timings")
    ap.add_argument("--out", default=os.path.join(HERE, "mix.json"))
    args = ap.parse_args()
    rows = list(csv.DictReader(open(args.csv, encoding="utf-8")))
    entries = []
    for idx, row in enumerate(rows):
        spec = gold_spec(row)
        errors = spec_module.validate_spec(spec)
        if errors:
            sys.exit("validation_{}: {}".format(idx, errors))
        entries.append({"queryId": "validation_{}".format(idx), "class": "{}-{}d".format(row["level"], row["days"]),
                        "maxCandidates": 3, "spec": spec})
    mix = {"representative": entries}
    if args.timings:
        timings = {t["queryId"]: t for t in json.load(open(args.timings))}
        ranked = sorted((e for e in entries if timings[e["queryId"]]["mc3"]["status"] == "optimal"),
                        key=lambda e: -timings[e["queryId"]]["mc3"]["solveMs"])
        mix["heavy"] = ranked[:8]
    with open(args.out, "w") as f:
        json.dump(mix, f, separators=(",", ":"))
    print({k: len(v) for k, v in mix.items()})


if __name__ == "__main__":
    main()
