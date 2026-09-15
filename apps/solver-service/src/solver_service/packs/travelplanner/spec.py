"""ConstraintSpec validation for the TravelPlanner solver.

INVARIANT: this package never reads the gold CSV fields (org/dest/days/
people_number/budget/local_constraint/level). All query parameters arrive via
the ConstraintSpec argument; only ref-info JSONL rows and database background
files are read from disk.

``validate_spec`` returns a list of human-readable error strings; an empty
list means the spec is valid. Validation is jsonschema (draft-07) against
``spec.schema.json`` plus semantic cross-checks the schema cannot express.
"""

import datetime
import json
import os

import jsonschema

_SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spec.schema.json")

with open(_SCHEMA_PATH, "r", encoding="utf-8") as _f:
    SCHEMA = json.load(_f)

_VALIDATOR = jsonschema.Draft7Validator(SCHEMA)


def _format_schema_error(error):
    path = "$" + "".join(
        "[{!r}]".format(p) if isinstance(p, int) else ".{}".format(p) for p in error.absolute_path
    )
    return "{}: {}".format(path, error.message)


def validate_spec(spec):
    """Validate a ConstraintSpec dict. Returns a list of error strings (empty = valid)."""
    errors = []

    if not isinstance(spec, dict):
        return ["spec must be a JSON object, got {}".format(type(spec).__name__)]

    for error in sorted(_VALIDATOR.iter_errors(spec), key=lambda e: list(e.absolute_path)):
        errors.append(_format_schema_error(error))
    if errors:
        return errors

    # Semantic cross-checks.
    try:
        datetime.date.fromisoformat(spec["startDate"])
    except ValueError:
        errors.append("$.startDate: {!r} is not a real calendar date".format(spec["startDate"]))

    # The TravelPlanner route model pins one city per 2 days: city ci occupies
    # days 2i-1 (travel-in) and 2i (stay), plus one return day.
    expected_days = 2 * spec["visitingCityNumber"] + 1
    if spec["days"] != expected_days:
        errors.append(
            "$.days: days={} is inconsistent with visitingCityNumber={} "
            "(route model requires days == 2 * visitingCityNumber + 1 == {})".format(
                spec["days"], spec["visitingCityNumber"], expected_days
            )
        )

    destination = spec["destination"]
    if destination["type"] == "city":
        if spec["visitingCityNumber"] != 1:
            errors.append(
                "$.visitingCityNumber: a single destination city implies visitingCityNumber=1, "
                "got {}".format(spec["visitingCityNumber"])
            )
        if destination["name"] == spec["origin"]:
            errors.append("$.destination.name: destination city must differ from origin")

    return errors
