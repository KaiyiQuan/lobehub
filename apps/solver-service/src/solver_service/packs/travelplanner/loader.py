"""Loader for TravelPlanner per-query reference information.

INVARIANT: this package never reads the gold CSV fields (org/dest/days/
people_number/budget/local_constraint/level). Everything the solver and
verifier know about a query comes from the ConstraintSpec argument plus the
ref-info JSONL row loaded here (and the database background city/state file).

Ref-info row anatomy (one JSON object per query, line ``idx0`` of
``{dataDir}/{split}_ref_info.jsonl``):

- ``Attractions in {City}`` / ``Restaurants in {City}`` /
  ``Accommodations in {City}``: entity lists for each visiting city.
- ``Flight from {A} to {B} on {YYYY-MM-DD}``: flight options for one leg.
  The file order of these keys pins the route: org -> c1 -> ... -> cN -> org.
- ``Self-driving from {A} to {B}`` / ``Taxi from {A} to {B}``: strings like
  ``self-driving, from Washington to Myrtle Beach, duration: 6 hours 47 mins, distance: 693 km, cost: 34``.

Route model (mirrors the official evaluator): city ci occupies days 2i-1
(travel-in) and 2i (stay); day D = 2N+1 is the return day.
"""

import json
import os
import re

_FLIGHT_KEY_RE = re.compile(r"^Flight from (.+) to (.+) on (\d{4}-\d{2}-\d{2})$")
_DRIVING_KEY_RE = re.compile(r"^(Self-driving|Taxi) from (.+) to (.+)$")
_ENTITY_KEY_RE = re.compile(r"^(Attractions|Restaurants|Accommodations) in (.+)$")
_COST_RE = re.compile(r"cost:\s*(\d+)")
_DISTANCE_RE = re.compile(r"distance:\s*([\d,]+)\s*km")
_DURATION_RE = re.compile(r"duration:\s*([^,]+)")


class RefDataError(Exception):
    """Raised when a ref-info row is missing or malformed."""


class DrivingOption(object):
    """One self-driving / taxi option for a leg, parsed from the ref-info string."""

    __slots__ = ("mode", "raw", "duration", "distance_km", "cost")

    def __init__(self, mode, raw, duration, distance_km, cost):
        self.mode = mode  # "self-driving" | "taxi"
        self.raw = raw  # verbatim ref-info string, emitted into the plan
        self.duration = duration
        self.distance_km = distance_km
        self.cost = cost  # None => invalid in the official sandbox (e.g. 'day' durations)


class Leg(object):
    """One travel leg A -> B with its transport options."""

    __slots__ = ("origin", "dest", "date", "flights", "self_driving", "taxi")

    def __init__(self, origin, dest, date=None):
        self.origin = origin
        self.dest = dest
        self.date = date
        self.flights = []
        self.self_driving = None
        self.taxi = None


class RefData(object):
    """Typed view over one ref-info JSONL row."""

    __slots__ = (
        "query_id",
        "split",
        "idx",
        "origin",
        "cities",
        "days",
        "legs",
        "restaurants",
        "accommodations",
        "attractions",
        "city_state",
        "raw",
    )

    def __init__(self, **kwargs):
        for key in self.__slots__:
            setattr(self, key, kwargs.get(key))


def parse_driving_option(mode, raw):
    """Parse a ref-info self-driving/taxi string into a DrivingOption.

    Mirrors tools/googleDistanceMatrix: cost comes from the ``cost: N`` field
    (itself ``int(distance_km * 0.05)`` for self-driving, ``int(distance_km)``
    for taxi in the official pipeline). A 'day' duration makes the official
    evaluator treat the option as cost=None (invalid in sandbox); mirror that.
    """
    cost_match = _COST_RE.search(raw)
    dist_match = _DISTANCE_RE.search(raw)
    dur_match = _DURATION_RE.search(raw)
    if "no valid information" in raw:
        return DrivingOption(mode, raw, None, None, None)
    duration = dur_match.group(1).strip() if dur_match else None
    distance_km = int(dist_match.group(1).replace(",", "")) if dist_match else None
    cost = int(cost_match.group(1)) if cost_match else None
    if duration is not None and "day" in duration:
        cost = None
    return DrivingOption(mode, raw, duration, distance_km, cost)


def load_city_state_map(db_dir):
    """Load city -> state from ``{db_dir}/background/citySet_with_states.txt`` (tab-separated)."""
    path = os.path.join(db_dir, "background", "citySet_with_states.txt")
    city_state = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f.read().split("\n"):
            if not line.strip():
                continue
            city, state = line.split("\t")
            city_state[city] = state
    return city_state


def resolve_db_dir(data_dir, db_dir=None):
    if db_dir:
        return db_dir
    env = os.environ.get("TRAVELPLANNER_DB_DIR")
    if env:
        return env
    # Service layout: the committed background/ directory lives inside the pack
    # data dir itself (only citySet_with_states.txt is needed, not the full DB).
    return data_dir


def _read_jsonl_line(path, idx):
    with open(path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i == idx:
                return json.loads(line)
    raise RefDataError("ref-info file {} has no line {}".format(path, idx))


def parse_query_id(query_id):
    """Split ``{split}_{idx0}`` into (split, idx), validating both parts."""
    try:
        split, idx_str = query_id.rsplit("_", 1)
        idx = int(idx_str)
    except ValueError:
        raise RefDataError(
            "queryId must look like '{{split}}_{{idx0}}' (e.g. 'validation_0'), got {!r}".format(query_id)
        )
    if split not in ("train", "validation", "test"):
        raise RefDataError("unknown split {!r} in queryId {!r}".format(split, query_id))
    return split, idx


def load_ref_data(query_id, data_dir, db_dir=None):
    """Load the ref-info row for ``query_id`` (``{split}_{idx0}``) into a RefData."""
    split, idx = parse_query_id(query_id)
    path = os.path.join(data_dir, "{}_ref_info.jsonl".format(split))
    if not os.path.isfile(path):
        raise RefDataError("ref-info file not found: {}".format(path))
    row = _read_jsonl_line(path, idx)
    return parse_ref_row(query_id, row, load_city_state_map(resolve_db_dir(data_dir, db_dir)))


def parse_ref_row(query_id, row, city_state):
    """Parse one ref-info JSONL row (already json-loaded) into a RefData.

    ``city_state`` is the shared city -> state map (loaded once per process).
    """
    split, idx = parse_query_id(query_id)

    legs = []
    leg_by_cities = {}
    restaurants = {}
    accommodations = {}
    attractions = {}

    # JSON object key order is preserved by json.loads and pins the route.
    for key, value in row.items():
        flight_match = _FLIGHT_KEY_RE.match(key)
        if flight_match:
            origin, dest, date = flight_match.groups()
            leg = Leg(origin, dest, date)
            # some legs have no flights: the value is then a string like
            # "There is no flight from A to B on <date>."
            leg.flights = list(value) if isinstance(value, list) else []
            legs.append(leg)
            leg_by_cities[(origin, dest)] = leg
            continue
        driving_match = _DRIVING_KEY_RE.match(key)
        if driving_match:
            mode_word, origin, dest = driving_match.groups()
            mode = "self-driving" if mode_word == "Self-driving" else "taxi"
            leg = leg_by_cities.get((origin, dest))
            if leg is None:
                leg = Leg(origin, dest)
                legs.append(leg)
                leg_by_cities[(origin, dest)] = leg
            option = parse_driving_option(mode, value)
            if mode == "self-driving":
                leg.self_driving = option
            else:
                leg.taxi = option
            continue
        entity_match = _ENTITY_KEY_RE.match(key)
        if entity_match:
            kind, city = entity_match.groups()
            if kind == "Attractions":
                attractions[city] = list(value)
            elif kind == "Restaurants":
                restaurants[city] = list(value)
            else:
                accommodations[city] = list(value)
            continue
        raise RefDataError("unrecognized ref-info key: {!r}".format(key))

    if not legs:
        raise RefDataError("ref-info row {} contains no flight legs".format(query_id))
    for leg in legs:
        if leg.date is None:
            raise RefDataError(
                "ref-info row {}: leg {} -> {} has no flight key (route cannot be dated)".format(
                    query_id, leg.origin, leg.dest
                )
            )

    origin = legs[0].origin
    cities = [leg.dest for leg in legs[:-1]]
    if legs[-1].dest != origin:
        raise RefDataError(
            "ref-info row {}: route is not a closed circle (ends at {}, origin is {})".format(
                query_id, legs[-1].dest, origin
            )
        )
    days = 2 * len(cities) + 1

    return RefData(
        query_id=query_id,
        split=split,
        idx=idx,
        origin=origin,
        cities=cities,
        days=days,
        legs=legs,
        restaurants=restaurants,
        accommodations=accommodations,
        attractions=attractions,
        city_state=city_state,
        raw=row,
    )


class RefDataStore(object):
    """Process-wide cache of ref-info rows, loaded once at worker startup.

    Raw JSONL lines are kept in memory (~33 MB for all three splits) and the
    single requested line is json-parsed per query (~1 ms); this keeps per-worker
    memory near the on-disk size instead of materializing 1225 parsed rows.
    """

    def __init__(self, data_dir, splits=("train", "validation", "test")):
        self.data_dir = data_dir
        self.city_state = load_city_state_map(resolve_db_dir(data_dir))
        self._lines = {}
        for split in splits:
            path = os.path.join(data_dir, "{}_ref_info.jsonl".format(split))
            if not os.path.isfile(path):
                raise RefDataError(
                    "ref-info file not found: {} (run scripts/fetch_data.py)".format(path)
                )
            with open(path, "r", encoding="utf-8") as f:
                self._lines[split] = f.read().splitlines()

    @property
    def splits(self):
        return sorted(self._lines)

    def query_count(self):
        return {split: len(lines) for split, lines in sorted(self._lines.items())}

    def get(self, query_id):
        split, idx = parse_query_id(query_id)
        lines = self._lines.get(split)
        if lines is None:
            raise RefDataError("split {!r} is not loaded in this worker".format(split))
        if idx < 0 or idx >= len(lines):
            raise RefDataError(
                "queryId {!r} out of range: split {!r} has {} queries".format(query_id, split, len(lines))
            )
        return parse_ref_row(query_id, json.loads(lines[idx]), self.city_state)
