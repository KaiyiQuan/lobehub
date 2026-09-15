"""Independent verifier for TravelPlanner plans (second code path).

INVARIANT: this package never reads the gold CSV fields (org/dest/days/
people_number/budget/local_constraint/level). All query parameters arrive via
the ConstraintSpec argument; entity data comes from the ref-info row.

This module deliberately does NOT import solve.py or share its constraint
encoding. It re-parses the plan strings with the official evaluator's own
regexes (reimplemented locally: ``extract_from_to``, ``extract_before_parenthesis``,
``get_valid_name_city``, ``transportation_match``), looks entities up in the
ref-info data, recomputes the cost, and checks all 13 official constraints.

Approximation (documented): the official evaluator looks entities up in the
full TravelPlanner databases (``Name.str.contains(re.escape(name))`` + exact
city, FIRST matching row); we look them up in the query's ref-info lists with
the same first-match rule. Ref info is a subset of the DB drawn for this
query, so for solver-produced plans the verdicts agree; the residual risk is
duplicate-name rows in the full DB whose first row differs from the ref-info
row (values could then differ from what the official evaluator would use).

Evaluator quirks mirrored here on purpose:
- ``is_valid_accommodation``: the minimum-nights rule is only enforced when
  EXACTLY ONE row matches (name contains + city); otherwise skipped.
- ``valid_cuisine``: meals in the origin city are skipped; cuisine membership
  is a substring test against the first matching row's ``Cuisines`` string.
- ``valid_transportation`` (hard): the substring tests ``'Flight' in value`` /
  ``'Self-driving' in value`` are case-sensitive, so the lowercase ref-info
  self-driving strings evade the official check. Mirrored verbatim.
- All ``min(question['days'], len(tested_data))`` truncations are kept.
"""

import math
import re

# ---------------------------------------------------------------------------
# Official evaluator helpers, reimplemented locally (do not import elsewhere).
# ---------------------------------------------------------------------------

_FROM_TO_PATTERN = r"from\s+(.+?)\s+to\s+([^,]+)(?=[,\s]|$)"
_NAME_CITY_PATTERN = r"(.*?),\s*([^,]+)(\(\w[\w\s]*\))?$"


def extract_from_to(text):
    """Official regex: 'from A to B', B ending at a comma or end of string."""
    matches = re.search(_FROM_TO_PATTERN, text)
    return matches.groups() if matches else (None, None)


def extract_before_parenthesis(s):
    match = re.search(r"^(.*?)\([^)]*\)", s)
    return match.group(1) if match else s


def get_valid_name_city(info):
    """Official 'Name, City' (optionally 'City(State)') splitter."""
    match = re.search(_NAME_CITY_PATTERN, info)
    if match:
        return match.group(1).strip(), extract_before_parenthesis(match.group(2).strip()).strip()
    return "-", "-"


def transportation_match(text):
    if "taxi" in text.lower():
        return "Taxi"
    if "self-driving" in text.lower():
        return "Self-driving"
    if "flight" in text.lower():
        return "Flight"
    return None


def _count_consecutive_values(lst):
    if not lst:
        return []
    result = []
    current = lst[0]
    count = 1
    for value in lst[1:]:
        if value == current:
            count += 1
        else:
            result.append((current, count))
            current = value
            count = 1
    result.append((current, count))
    return result


# ---------------------------------------------------------------------------
# Ref-info lookups (mirror the official first-match semantics on the subset).
# ---------------------------------------------------------------------------


class _Tables(object):
    def __init__(self, ref):
        self.ref = ref
        self.restaurants = [row for rows in ref.restaurants.values() for row in rows]
        self.accommodations = [row for rows in ref.accommodations.values() for row in rows]
        self.attractions = [row for rows in ref.attractions.values() for row in rows]
        self.flights = [f for leg in ref.legs for f in leg.flights]
        self.driving = {}
        for leg in ref.legs:
            if leg.self_driving is not None:
                self.driving[(leg.origin, leg.dest, "self-driving")] = leg.self_driving
            if leg.taxi is not None:
                self.driving[(leg.origin, leg.dest, "taxi")] = leg.taxi

    def find_restaurant(self, name, city):
        for row in self.restaurants:
            if name in str(row["Name"]) and row["City"] == city:
                return row
        return None

    def match_restaurants(self, name, city):
        return [row for row in self.restaurants if name in str(row["Name"]) and row["City"] == city]

    def find_accommodation(self, name, city):
        for row in self.accommodations:
            if name in str(row["NAME"]) and row["city"] == city:
                return row
        return None

    def match_accommodations(self, name, city):
        return [row for row in self.accommodations if name in str(row["NAME"]) and row["city"] == city]

    def find_attraction(self, name, city):
        for row in self.attractions:
            if name in str(row["Name"]) and row["City"] == city:
                return row
        return None

    def flight_exists(self, number, origin, dest):
        return any(
            f["Flight Number"] == number and f["OriginCityName"] == origin and f["DestCityName"] == dest
            for f in self.flights
        )

    def flight_price(self, number):
        for f in self.flights:
            if f["Flight Number"] == number:
                return f["Price"]
        return None

    def driving_option(self, origin, dest, mode):
        return self.driving.get((origin, dest, mode))


# ---------------------------------------------------------------------------
# Constraint checks (official names, official semantics).
# ---------------------------------------------------------------------------


def _iter_days(question, plan):
    return range(min(question["days"], len(plan)))


def _is_valid_days(question, plan):
    lens = 0
    for i in _iter_days(question, plan):
        unit = plan[i]
        if unit != {} and unit.get("current_city") != "You don't need to fill in the information for this or later days.":
            lens += 1
    if lens != question["days"]:
        return False, "The number of days should be {}.".format(question["days"])
    return True, None


def _is_valid_visiting_city_number(question, plan):
    city_set = set()
    for i in _iter_days(question, plan):
        city_value = plan[i]["current_city"]
        if "from" in city_value:
            city1, city2 = extract_from_to(city_value)
            city1 = extract_before_parenthesis(city1)
            city2 = extract_before_parenthesis(city2)
            if i == 0 and city1 != question["org"]:
                return False, "The first day's city should be {}.".format(question["org"])
            city_set.add(city1)
            city_set.add(city2)
        else:
            city_set.add(extract_before_parenthesis(city_value))
    city_set.discard(question["org"])
    if len(city_set) != question["visiting_city_number"]:
        return False, "The number of visiting cities should be {}.".format(question["visiting_city_number"])
    return True, None


def _is_not_absent(question, plan):
    needed_info = 6 * question["days"]
    total_valid_info = 0

    if not _is_valid_days(question, plan)[0]:
        return False, "Invalid Days"
    if not _is_valid_visiting_city_number(question, plan)[0]:
        return False, "Invalid City Number"

    for i in _iter_days(question, plan):
        unit = plan[i]
        for key, label in (
            ("transportation", "Transportation"),
            ("breakfast", "Breakfast"),
            ("lunch", "Lunch"),
            ("dinner", "Dinner"),
            ("attraction", "Attraction"),
            ("accommodation", "Accommodation"),
        ):
            if key not in unit:
                return False, "No {} Info.".format(label)

        if ("from " in unit["current_city"] or "to " in unit["current_city"]) and unit["transportation"] in ["", "-"]:
            return False, "No transportation in day {} is not allowed.".format(i + 1)
        if ("from " not in unit["current_city"] and " to " not in unit["current_city"]) and unit["attraction"] in ["", "-"]:
            return False, "No attaction in day {} is not allowed.".format(i + 1)
        if i != question["days"] - 1 and unit["accommodation"] in ["", "-"]:
            return False, "No accommodation in day {} is not allowed.".format(i + 1)
        if (unit["breakfast"] in ["", "-"] or unit["lunch"] in ["", "-"] or unit["dinner"] in ["", "-"]) and (
            "from " not in unit["current_city"]
        ):
            return False, "No meal in day {} is not allowed.".format(i + 1)

        for key in unit:
            if unit[key] and unit[key] != "-":
                total_valid_info += 1

    if total_valid_info * 1.0 / needed_info < 0.5:
        return False, "The absent information is more than 50%."
    return True, None


def _is_valid_information_in_sandbox(question, plan, tables):
    for i in _iter_days(question, plan):
        unit = plan[i]
        if unit["transportation"] and unit["transportation"] != "-":
            value = unit["transportation"]
            org_city, dest_city = extract_from_to(value)
            if org_city is None or dest_city is None:
                org_city, dest_city = extract_from_to(unit["current_city"])
            if "flight number" in value.lower():
                try:
                    org_city = extract_before_parenthesis(org_city)
                    dest_city = extract_before_parenthesis(dest_city)
                except TypeError:
                    return False, "The transportation {} in day {} can not be parsed.".format(value, i + 1)
                number = value.split("Flight Number: ")[1].split(",")[0]
                if not tables.flight_exists(number, org_city, dest_city):
                    return False, "The flight number in day {} is invalid in the sandbox.".format(i + 1)
            elif "self-driving" in value.lower() or "taxi" in value.lower():
                try:
                    org_city = extract_before_parenthesis(org_city)
                    dest_city = extract_before_parenthesis(dest_city)
                except TypeError:
                    org_city = "-"
                    dest_city = "-"
                mode = "self-driving" if "self-driving" in value.lower() else "taxi"
                option = tables.driving_option(org_city, dest_city, mode)
                if option is None or option.cost is None:
                    return False, "The {} in day {} is invalid in the sandbox.".format(mode, i + 1)

        for meal_key in ("breakfast", "lunch", "dinner"):
            if meal_key in unit and unit[meal_key] and unit[meal_key] != "-":
                name, city = get_valid_name_city(unit[meal_key])
                if tables.find_restaurant(name, city) is None:
                    return False, "The {} in day {} is invalid in the sandbox.".format(meal_key, i + 1)

        if "attraction" in unit and unit["attraction"] and unit["attraction"] != "-":
            for attraction in unit["attraction"].split(";")[:-1]:
                name, city = get_valid_name_city(attraction)
                if tables.find_attraction(name, city) is None:
                    return False, "The attraction {} in day {} is invalid in the sandbox.".format(attraction, i + 1)

        if "accommodation" in unit and unit["accommodation"] and unit["accommodation"] != "-":
            name, city = get_valid_name_city(unit["accommodation"])
            if tables.find_accommodation(name, city) is None:
                return False, "The accommodation in day {} is invalid in the sandbox.".format(i + 1)

    return True, None


def _is_valid_information_in_current_city(question, plan):
    for i in _iter_days(question, plan):
        unit = plan[i]
        current_city = unit["current_city"]
        if "from" in current_city:
            city1, city2 = extract_from_to(current_city)
            final_city_list = [extract_before_parenthesis(city1), extract_before_parenthesis(city2)]
        else:
            final_city_list = [extract_before_parenthesis(current_city)]

        if "transportation" in unit and unit["transportation"] and unit["transportation"] != "-":
            for city in final_city_list:
                if city not in unit["transportation"]:
                    return False, "The transportation in day {} is invalid city choice.".format(i + 1)

        for meal_key in ("breakfast", "lunch", "dinner"):
            if meal_key in unit and unit[meal_key] and unit[meal_key] != "-":
                if not any(city in unit[meal_key] for city in final_city_list):
                    return False, "The {} in day {} is invalid city choice.".format(meal_key, i + 1)

        if "attraction" in unit and unit["attraction"] and unit["attraction"] != "-":
            for attraction in unit["attraction"].split(";")[:-1]:
                if not any(city in attraction for city in final_city_list):
                    return False, "The attraction in day {} is invalid city choice.".format(i + 1)

        if "accommodation" in unit and unit["accommodation"] and unit["accommodation"] != "-":
            if final_city_list[-1] not in unit["accommodation"]:
                return False, "The accommodation in day {} is invalid city choice.".format(i + 1)

    return True, None


def _is_valid_city_sequence(city_list):
    if len(city_list) < 3:
        return False
    visited = set()
    i = 0
    while i < len(city_list):
        city = city_list[i]
        if city in visited and (i != 0 and i != len(city_list) - 1):
            return False
        count = 0
        while i < len(city_list) and city_list[i] == city:
            count += 1
            i += 1
        if count == 1 and 0 < i - 1 < len(city_list) - 1:
            return False
        visited.add(city)
    return True


def _is_reasonable_visiting_city(question, plan, city_state_map):
    city_list = []
    for i in _iter_days(question, plan):
        city_value = plan[i]["current_city"]
        if "from" in city_value:
            city1, city2 = extract_from_to(city_value)
            city1 = extract_before_parenthesis(city1)
            city2 = extract_before_parenthesis(city2)
            if i == 0 and city1 != question["org"]:
                return False, "The first day's city should be {}.".format(question["org"])
            city_list += [city1, city2]
        else:
            city_list.append(extract_before_parenthesis(city_value))

    if not city_list:
        return False, "The trip should be a closed circle."
    if city_list[0] != city_list[-1]:
        return False, "The trip should be a closed circle."
    if not _is_valid_city_sequence(city_list):
        return False, "The city sequence is invalid."
    for idx, city in enumerate(city_list):
        if city not in city_state_map:
            return False, "{} is not a valid city.".format(city)
        if idx not in [0, len(city_list) - 1] and question["days"] > 3 and city_state_map[city] != question["dest"]:
            return False, "{} is not in {}.".format(city, question["dest"])
    return True, None


def _is_valid_restaurants(question, plan):
    restaurants_list = []
    for i in _iter_days(question, plan):
        unit = plan[i]
        for meal_key in ("breakfast", "lunch", "dinner"):
            if meal_key in unit and unit[meal_key] and unit[meal_key] != "-":
                if unit[meal_key] not in restaurants_list:
                    restaurants_list.append(unit[meal_key])
                else:
                    return False, "The restaurant in day {} {} is repeated.".format(i + 1, meal_key)
    return True, None


def _is_valid_attractions(question, plan):
    attractions_list = []
    for i in _iter_days(question, plan):
        unit = plan[i]
        if "attraction" in unit and unit["attraction"] and unit["attraction"] != "-":
            for attraction in unit["attraction"].split(";")[:-1]:
                if attraction not in attractions_list:
                    attractions_list.append(attraction)
                else:
                    return False, "The attraction '{}' in day {} is repeated.".format(attraction, i + 1)
    return True, None


def _commonsense_is_valid_transportation(question, plan):
    if plan[0]["transportation"] and plan[0]["transportation"] != "-":
        transportation_list = [transportation_match(plan[0]["transportation"])]
    else:
        return False, "The transportation in day 1 should not be empty."

    for i in _iter_days(question, plan):
        unit = plan[i]
        if "transportation" in unit and unit["transportation"] and unit["transportation"] != "-":
            transportation_list.append(transportation_match(unit["transportation"]))

    if ("Self-driving" in transportation_list and "Flight" in transportation_list) or (
        "Taxi" in transportation_list and "Self-driving" in transportation_list
    ):
        return False, "The transportation is conflicting."
    return True, None


def _is_valid_accommodation(question, plan, tables):
    """Minimum-nights rule. Official quirk: only enforced when EXACTLY ONE row
    matches (name-contains + city); runs of any other match count are skipped."""
    data = []
    for i in _iter_days(question, plan):
        unit = plan[i]
        if "accommodation" not in unit:
            return False, "No Accommodation Info."
        data.append(unit["accommodation"])
    for value, count in _count_consecutive_values(data):
        if value and value not in ["-", ""]:
            name, city = get_valid_name_city(value)
            matches = tables.match_accommodations(name, city)
            if len(matches) == 1 and count < matches[0]["minimum nights"]:
                return False, "The accommodation {} do not obey the minumum nights rule.".format(value)
    return True, None


def _get_total_cost(question, plan, tables):
    """Official cost formula, replayed against ref-info data."""
    total_cost = 0.0
    for i in _iter_days(question, plan):
        unit = plan[i]
        if unit["transportation"] and unit["transportation"] != "-":
            value = unit["transportation"]
            org_city, dest_city = extract_from_to(value)
            if org_city is None or dest_city is None:
                org_city, dest_city = extract_from_to(unit["current_city"])
            if org_city is not None and dest_city is not None:
                if "flight number" in value.lower():
                    number = value.split("Flight Number: ")[1].split(",")[0]
                    price = tables.flight_price(number)
                    if price is not None:
                        total_cost += price * question["people_number"]
                elif "self-driving" in value.lower() or "taxi" in value.lower():
                    mode = "self-driving" if "self-driving" in value.lower() else "taxi"
                    option = tables.driving_option(
                        extract_before_parenthesis(org_city), extract_before_parenthesis(dest_city), mode
                    )
                    if option is not None and option.cost is not None:
                        if mode == "self-driving":
                            total_cost += option.cost * math.ceil(question["people_number"] * 1.0 / 5)
                        else:
                            total_cost += option.cost * math.ceil(question["people_number"] * 1.0 / 4)

        for meal_key in ("breakfast", "lunch", "dinner"):
            if unit[meal_key] and unit[meal_key] != "-":
                name, city = get_valid_name_city(unit[meal_key])
                row = tables.find_restaurant(name, city)
                if row is not None:
                    total_cost += row["Average Cost"] * question["people_number"]

        if unit["accommodation"] and unit["accommodation"] != "-":
            name, city = get_valid_name_city(unit["accommodation"])
            row = tables.find_accommodation(name, city)
            if row is not None:
                total_cost += row["price"] * math.ceil(question["people_number"] * 1.0 / row["maximum occupancy"])
    return total_cost


def _valid_cost(question, plan, tables):
    total = _get_total_cost(question, plan, tables)
    if total <= question["budget"]:
        return True, "total cost {:.2f} <= budget {:.2f}".format(total, float(question["budget"]))
    return False, "total cost {:.2f} exceeds budget {:.2f}".format(total, float(question["budget"]))


_HOUSE_RULE_BAN = {
    "smoking": "No smoking",
    "parties": "No parties",
    "children under 10": "No children under 10",
    "visitors": "No visitors",
    "pets": "No pets",
}


def _valid_room_rule(question, plan, tables):
    house_rule = question["local_constraint"]["house rule"]
    if house_rule is None:
        return True, "skipped: no houseRule in spec"
    banned = _HOUSE_RULE_BAN[house_rule]
    for i in _iter_days(question, plan):
        unit = plan[i]
        if unit["accommodation"] and unit["accommodation"] != "-":
            name, city = get_valid_name_city(unit["accommodation"])
            row = tables.find_accommodation(name, city)
            if row is not None and banned in str(row["house_rules"]):
                return False, "The house rule should be {}.".format(house_rule)
    return True, None


def _valid_cuisine(question, plan, tables):
    cuisines = question["local_constraint"]["cuisine"]
    if not cuisines:
        return True, "skipped: no cuisines in spec"
    cuisine_set = set()
    for i in _iter_days(question, plan):
        unit = plan[i]
        for meal_key in ("breakfast", "lunch", "dinner"):
            if unit[meal_key] and unit[meal_key] != "-":
                name, city = get_valid_name_city(unit[meal_key])
                if city == question["org"]:
                    continue
                row = tables.find_restaurant(name, city)
                if row is not None:
                    for cuisine in cuisines:
                        if cuisine in str(row["Cuisines"]):
                            cuisine_set.add(cuisine)
    if len(cuisine_set) == len(cuisines):
        return True, None
    for cuisine in cuisines:
        if cuisine not in cuisine_set:
            return False, "The cuisine {} is not satisfied.".format(cuisine)
    return False, "The cuisines are not satisfied."


def _valid_room_type(question, plan, tables):
    room_type = question["local_constraint"]["room type"]
    if room_type is None:
        return True, "skipped: no roomType in spec"
    for i in _iter_days(question, plan):
        unit = plan[i]
        if unit["accommodation"] and unit["accommodation"] != "-":
            name, city = get_valid_name_city(unit["accommodation"])
            row = tables.find_accommodation(name, city)
            if row is None:
                continue
            actual = row["room type"]
            if room_type == "not shared room" and actual == "Shared room":
                return False, "The room type should be {}.".format(room_type)
            if room_type == "shared room" and actual != "Shared room":
                return False, "The room type should be {}.".format(room_type)
            if room_type == "private room" and actual != "Private room":
                return False, "The room type should be {}.".format(room_type)
            if room_type == "entire room" and actual != "Entire home/apt":
                return False, "The room type should be {}.".format(room_type)
    return True, None


def _hard_valid_transportation(question, plan):
    """Official hard transportation check. Quirk mirrored verbatim: the
    substring tests are case-sensitive, so the lowercase ref-info self-driving
    strings never trip 'no self-driving' (the solver filters them upstream)."""
    transportation = question["local_constraint"]["transportation"]
    if transportation is None:
        return True, "skipped: no transportation restriction in spec"
    for i in _iter_days(question, plan):
        unit = plan[i]
        if unit["transportation"] and unit["transportation"] != "-":
            value = unit["transportation"]
            if transportation == "no flight" and "Flight" in value:
                return False, "The transportation should not be {}.".format(transportation)
            if transportation == "no self-driving" and "Self-driving" in value:
                return False, "The transportation should not be {}.".format(transportation)
    return True, None


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------


def question_from_spec(spec):
    """Build the evaluator-style question dict from a ConstraintSpec."""
    return {
        "org": spec["origin"],
        "dest": spec["destination"]["name"],
        "days": spec["days"],
        "visiting_city_number": spec["visitingCityNumber"],
        "people_number": spec["peopleNumber"],
        "budget": spec["budget"],
        "local_constraint": {
            "house rule": spec.get("houseRule"),
            "cuisine": spec.get("cuisines"),
            "room type": spec.get("roomType"),
            "transportation": spec.get("transportation"),
        },
    }


def verify_plan(plan, spec, ref):
    """Check all 13 official constraints. Returns
    {"pass": bool, "results": [{"constraint", "pass", "detail"}]}."""
    question = question_from_spec(spec)
    tables = _Tables(ref)

    checks = [
        ("is_reasonable_visiting_city", lambda: _is_reasonable_visiting_city(question, plan, ref.city_state)),
        ("is_valid_restaurants", lambda: _is_valid_restaurants(question, plan)),
        ("is_valid_attractions", lambda: _is_valid_attractions(question, plan)),
        ("is_valid_accommodation", lambda: _is_valid_accommodation(question, plan, tables)),
        ("is_valid_transportation", lambda: _commonsense_is_valid_transportation(question, plan)),
        ("is_valid_information_in_current_city", lambda: _is_valid_information_in_current_city(question, plan)),
        ("is_valid_information_in_sandbox", lambda: _is_valid_information_in_sandbox(question, plan, tables)),
        ("is_not_absent", lambda: _is_not_absent(question, plan)),
        ("valid_cuisine", lambda: _valid_cuisine(question, plan, tables)),
        ("valid_room_rule", lambda: _valid_room_rule(question, plan, tables)),
        ("valid_transportation", lambda: _hard_valid_transportation(question, plan)),
        ("valid_room_type", lambda: _valid_room_type(question, plan, tables)),
        ("valid_cost", lambda: _valid_cost(question, plan, tables)),
    ]

    results = []
    for name, check in checks:
        try:
            passed, detail = check()
        except Exception as exc:  # a crash is a failed verification, never a pass
            passed, detail = False, "verifier error: {!r}".format(exc)
        results.append({"constraint": name, "pass": bool(passed), "detail": detail})

    return {"pass": all(r["pass"] for r in results), "results": results}
