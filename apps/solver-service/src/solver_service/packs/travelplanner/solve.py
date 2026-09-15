"""CP-SAT solver for TravelPlanner queries (primary and only engine).

INVARIANT: this package never reads the gold CSV fields (org/dest/days/
people_number/budget/local_constraint/level). All query parameters arrive via
the ConstraintSpec argument; only ref-info data (loader.RefData) is read.

Design notes:
- Single engine (OR-Tools CP-SAT) by decision: a second engine would risk
  semantic drift between solver and verifier. Conflicts are extracted with
  CP-SAT assumption literals (``model.add_assumption`` +
  ``solver.sufficient_assumptions_for_infeasibility``).
- The route is pinned by the ref-info flight-leg keys (see loader): city ci
  occupies days 2i-1 (travel-in) and 2i (stay); day D = 2N+1 returns home.
- All money math is integer cents. The official evaluator works in float
  dollars with the same per-item formulas, so the totals agree:
    flight       Price * people
    self-driving cost * ceil(people/5)      (cost = int(distance_km*0.05))
    taxi         cost * ceil(people/4)      (cost = int(distance_km))
    meal         Average Cost * people
    accommodation price * ceil(people/maximum occupancy)  per night
- Accommodation domains are pre-filtered by roomType + houseRule +
  ``minimum nights <= 2`` (the pinned route always stays exactly 2 nights per
  city). The official min-nights check has a quirk (skipped unless exactly one
  DB row matches); filtering always is stricter and safe, and verify.py
  mirrors the official quirk when re-checking.
- Result status is ``optimal`` when CP-SAT proved optimality, or
  ``feasible_timeout`` when the time limit cut the search off with a feasible
  (best-so-far) plan. ``time_limit_ms`` bounds the whole call: each candidate
  re-solve gets only the remaining budget.
"""

import math
import time

from ortools.sat.python import cp_model

from . import loader
from . import spec as spec_module

# Soft penalties are added to cost_cents * BIG so they only break cost ties.
# Max possible penalty is a few dozen (slots + cities), far below BIG.
_BIG = 100000

_HOUSE_RULE_BAN = {
    "smoking": "No smoking",
    "parties": "No parties",
    "children under 10": "No children under 10",
    "visitors": "No visitors",
    "pets": "No pets",
}

_ROOM_TYPE_MATCH = {
    "shared room": lambda rt: rt == "Shared room",
    "not shared room": lambda rt: rt != "Shared room",
    "private room": lambda rt: rt == "Private room",
    "entire room": lambda rt: rt == "Entire home/apt",
}


class _Choice(object):
    """One choosable option (transport / accommodation / meal / attraction)."""

    __slots__ = ("var", "cost_cents", "penalty", "render", "kind", "meta")

    def __init__(self, var, cost_cents, penalty, render, kind, meta=None):
        self.var = var
        self.cost_cents = cost_cents
        self.penalty = penalty
        self.render = render
        self.kind = kind
        self.meta = meta or {}


def _ceil_div(a, b):
    return -(-a // b)


def _cents(dollars):
    return int(round(float(dollars) * 100))


def _flight_transport_string(flight):
    return "Flight Number: {}, from {} to {}, Departure Time: {}, Arrival Time: {}".format(
        flight["Flight Number"],
        flight["OriginCityName"],
        flight["DestCityName"],
        flight["DepTime"],
        flight["ArrTime"],
    )


class _Model(object):
    """Built CP-SAT model plus everything needed to render / diagnose it."""

    def __init__(self):
        self.model = cp_model.CpModel()
        self.leg_choices = []  # per leg: list of _Choice
        self.acc_choices = []  # per city: list of _Choice
        self.meal_slots = []  # per day: {"breakfast": [choices], "lunch": [...], "dinner": [...]}
        self.attraction_slots = []  # per day: list of slots, each a list of _Choice
        self.cost_expr = None
        self.penalty_expr = None
        self.all_choice_vars = []
        self.assumption_names = {}  # literal index -> ("budget", None) | ("cuisine", name)


def _prefilter_accommodations(city_accs, spec):
    """Apply roomType / houseRule / minimum-nights<=2 filters. Returns (kept, dropped_count)."""
    room_type = spec.get("roomType")
    house_rule = spec.get("houseRule")
    banned = _HOUSE_RULE_BAN.get(house_rule) if house_rule else None
    kept = []
    for acc in city_accs:
        if float(acc["minimum nights"]) > 2:
            continue
        if room_type and not _ROOM_TYPE_MATCH[room_type](acc["room type"]):
            continue
        if banned and banned in str(acc["house_rules"]):
            continue
        kept.append(acc)
    return kept, len(city_accs) - len(kept)


def _structural_conflicts(ref, spec):
    """Deterministic pre-solve checks; each conflict is precise and human-readable."""
    conflicts = []

    if spec["origin"] != ref.origin:
        conflicts.append(
            {
                "constraint": "structural:origin",
                "explanation": (
                    "spec origin {!r} does not match the route origin {!r} pinned by the "
                    "query's reference information".format(spec["origin"], ref.origin)
                ),
                "involvedFields": ["origin"],
            }
        )
    if spec["days"] != ref.days:
        conflicts.append(
            {
                "constraint": "structural:days",
                "explanation": (
                    "spec days={} does not match the route pinned by the reference information "
                    "({} visiting cities => {} days)".format(spec["days"], len(ref.cities), ref.days)
                ),
                "involvedFields": ["days", "visitingCityNumber"],
            }
        )

    transportation = spec.get("transportation")
    leg_modes = []
    for i, leg in enumerate(ref.legs):
        modes = set()
        if transportation != "no flight" and leg.flights:
            modes.add("flight")
        if transportation != "no self-driving" and leg.self_driving is not None and leg.self_driving.cost is not None:
            modes.add("self-driving")
        if leg.taxi is not None and leg.taxi.cost is not None:
            modes.add("taxi")
        leg_modes.append(modes)
        if not modes:
            conflicts.append(
                {
                    "constraint": "structural:legOptions",
                    "explanation": (
                        "leg {} ({} -> {}) has no usable transport option under "
                        "transportation restriction {!r}".format(i + 1, leg.origin, leg.dest, transportation)
                    ),
                    "involvedFields": ["transportation"],
                }
            )

    # forced mode mixes the non-conflicting-transportation rule forbids:
    # a leg with ONLY flights plus a leg with ONLY self-driving, or a leg with
    # ONLY taxi plus a leg with ONLY self-driving, can never yield a valid plan
    only = [next(iter(m)) if len(m) == 1 else None for m in leg_modes]
    forced_mix = None
    if "flight" in only and "self-driving" in only:
        forced_mix = "flight + self-driving"
    elif "taxi" in only and "self-driving" in only:
        forced_mix = "taxi + self-driving"
    if forced_mix:
        conflicts.append(
            {
                "constraint": "structural:transportationMix",
                "explanation": (
                    "the pinned route forces a {} mix across legs (some leg has only one mode "
                    "available), which the non-conflicting-transportation rule forbids"
                ).format(forced_mix),
                "involvedFields": ["transportation"],
            }
        )

    for city in ref.cities:
        city_accs = ref.accommodations.get(city, [])
        if not city_accs:
            conflicts.append(
                {
                    "constraint": "structural:accommodationDomain",
                    "explanation": "no accommodations at all in ref info for city {!r}".format(city),
                    "involvedFields": [],
                }
            )
            continue
        kept, dropped = _prefilter_accommodations(city_accs, spec)
        if not kept:
            fields = [f for f in ("roomType", "houseRule") if spec.get(f)]
            conflicts.append(
                {
                    "constraint": "structural:accommodationDomain",
                    "explanation": (
                        "no accommodation in {!r} survives the filters ({} of {} filtered out by "
                        "roomType={!r}, houseRule={!r} and minimum nights <= 2)".format(
                            city, dropped, len(city_accs), spec.get("roomType"), spec.get("houseRule")
                        )
                    ),
                    "involvedFields": fields,
                }
            )

    for city in ref.cities:
        rests = ref.restaurants.get(city, [])
        if len(rests) < 3:
            conflicts.append(
                {
                    "constraint": "structural:restaurantPool",
                    "explanation": (
                        "city {!r} has only {} restaurant(s) in ref info; the stay day needs 3 "
                        "distinct ones (diverse-restaurants rule)".format(city, len(rests))
                    ),
                    "involvedFields": [],
                }
            )
        if not ref.attractions.get(city):
            conflicts.append(
                {
                    "constraint": "structural:attractionPool",
                    "explanation": "city {!r} has no attractions in ref info; the stay day needs one".format(city),
                    "involvedFields": [],
                }
            )

    return conflicts


def _build_model(ref, spec, with_budget=True):
    """Build the CP-SAT model. ``with_budget=False`` is used to find the min feasible cost."""
    people = spec["peopleNumber"]
    soft = spec.get("soft") or {}
    min_rest_rating = soft.get("minRestaurantRating")
    min_acc_rate = soft.get("minAccommodationReviewRate")
    preferred_cuisines = soft.get("preferredCuisines") or []

    built = _Model()
    model = built.model
    cost_terms = []
    penalty_terms = []

    # --- transport: exactly one option per leg ---
    transportation = spec.get("transportation")
    for leg in ref.legs:
        choices = []
        if transportation != "no flight":
            for flight in leg.flights:
                var = model.new_bool_var("leg_{}_{}".format(len(built.leg_choices), flight["Flight Number"]))
                cost = _cents(flight["Price"] * people)
                choices.append(
                    _Choice(var, cost, 0, _flight_transport_string(flight), "flight", {"flight": flight})
                )
        if transportation != "no self-driving" and leg.self_driving is not None and leg.self_driving.cost is not None:
            option = leg.self_driving
            var = model.new_bool_var("leg_{}_driving".format(len(built.leg_choices)))
            cost = _cents(option.cost * _ceil_div(people, 5))
            choices.append(_Choice(var, cost, 0, option.raw, "self-driving"))
        if leg.taxi is not None and leg.taxi.cost is not None:
            option = leg.taxi
            var = model.new_bool_var("leg_{}_taxi".format(len(built.leg_choices)))
            cost = _cents(option.cost * _ceil_div(people, 4))
            choices.append(_Choice(var, cost, 0, option.raw, "taxi"))
        model.add_exactly_one([c.var for c in choices])
        built.leg_choices.append(choices)

    # --- non-conflicting transportation (commonsense is_valid_transportation):
    # the plan may not mix Self-driving+Flight or Taxi+Self-driving across legs
    # (Flight+Taxi is allowed by the official evaluator) ---
    flight_vars = [c.var for choices in built.leg_choices for c in choices if c.kind == "flight"]
    driving_vars = [c.var for choices in built.leg_choices for c in choices if c.kind == "self-driving"]
    taxi_vars = [c.var for choices in built.leg_choices for c in choices if c.kind == "taxi"]

    def any_indicator(vars_, name):
        indicator = model.new_bool_var(name)
        if vars_:
            model.add(sum(vars_) >= 1).only_enforce_if(indicator)
            model.add(sum(vars_) == 0).only_enforce_if(indicator.negated())
        else:
            model.add(indicator == 0)
        return indicator

    flight_any = any_indicator(flight_vars, "any_flight")
    driving_any = any_indicator(driving_vars, "any_driving")
    taxi_any = any_indicator(taxi_vars, "any_taxi")
    model.add(flight_any + driving_any <= 1)
    model.add(taxi_any + driving_any <= 1)

    # --- accommodation: exactly one per city (2 nights each) ---
    for city in ref.cities:
        kept, _ = _prefilter_accommodations(ref.accommodations.get(city, []), spec)
        choices = []
        for j, acc in enumerate(kept):
            var = model.new_bool_var("acc_{}_{}".format(city, j))
            rooms = _ceil_div(people, int(acc["maximum occupancy"]))
            cost = _cents(acc["price"]) * rooms * 2  # pinned route: always exactly 2 nights
            penalty = 0
            if min_acc_rate is not None and float(acc["review rate number"]) < min_acc_rate:
                penalty += 1
            render = "{}, {}".format(acc["NAME"], city)
            choices.append(_Choice(var, cost, penalty, render, "accommodation", {"row": acc, "city": city}))
        model.add_exactly_one([c.var for c in choices])
        built.acc_choices.append(choices)

    # --- meals ---
    # Day layout (1-based): day 1 travel-in to c1; day 2i stay in ci; day 2i-1
    # travel-in to ci (i>=2); day 2N+1 return. Stay-day meals are mandatory;
    # travel-day meals optional (pool = arrival city; return day = last city).
    n = len(ref.cities)
    days = 2 * n + 1
    restaurant_use = {}  # (city, restaurant index) -> [vars]

    def meal_choices_for(city, day, slot_name, mandatory):
        rests = ref.restaurants.get(city, [])
        choices = []
        for r, rest in enumerate(rests):
            var = model.new_bool_var("meal_{}_{}_{}".format(day, slot_name, r))
            restaurant_use.setdefault((city, r), []).append(var)
            cost = _cents(rest["Average Cost"] * people)
            penalty = 0
            if min_rest_rating is not None and float(rest["Aggregate Rating"]) < min_rest_rating:
                penalty += 1
            if preferred_cuisines and not any(c in str(rest["Cuisines"]) for c in preferred_cuisines):
                penalty += 1
            render = "{}, {}".format(rest["Name"], city)
            choices.append(_Choice(var, cost, penalty, render, "meal", {"row": rest, "city": city}))
        if mandatory:
            model.add_exactly_one([c.var for c in choices])
        else:
            model.add_at_most_one([c.var for c in choices])
        return choices

    for day in range(1, days + 1):
        if day == days:
            pool_city = ref.cities[-1]  # return day: optional meals in the last city
            mandatory = False
        elif day % 2 == 0:
            pool_city = ref.cities[day // 2 - 1]  # stay day in c_{day/2}
            mandatory = True
        else:
            pool_city = ref.cities[(day + 1) // 2 - 1]  # travel-in day to c_{(day+1)/2}
            mandatory = False
        built.meal_slots.append(
            {
                "breakfast": meal_choices_for(pool_city, day, "b", mandatory),
                "lunch": meal_choices_for(pool_city, day, "l", mandatory),
                "dinner": meal_choices_for(pool_city, day, "d", mandatory),
            }
        )

    # --- attractions ---
    # Stay day: slot 1 mandatory + slot 2 optional. Travel-in day: one optional
    # slot (arrival city). Return day: none.
    attraction_use = {}
    for day in range(1, days + 1):
        slots = []
        if day == days:
            pass
        else:
            if day % 2 == 0:
                city = ref.cities[day // 2 - 1]
                n_slots = 2
            else:
                city = ref.cities[(day + 1) // 2 - 1]
                n_slots = 1
            attractions = ref.attractions.get(city, [])
            for s in range(n_slots):
                choices = []
                for a, attr in enumerate(attractions):
                    var = model.new_bool_var("attr_{}_{}_{}".format(day, s, a))
                    attraction_use.setdefault((city, a), []).append(var)
                    render = "{}, {}".format(attr["Name"], city)
                    choices.append(_Choice(var, 0, 0, render, "attraction", {"row": attr, "city": city}))
                mandatory = day % 2 == 0 and s == 0
                if mandatory:
                    model.add_exactly_one([c.var for c in choices])
                else:
                    model.add_at_most_one([c.var for c in choices])
                slots.append(choices)
        built.attraction_slots.append(slots)

    # --- diversity: every restaurant / attraction used at most once plan-wide ---
    for vars_ in restaurant_use.values():
        if len(vars_) > 1:
            model.add_at_most_one(vars_)
    for vars_ in attraction_use.values():
        if len(vars_) > 1:
            model.add_at_most_one(vars_)

    # --- cost ---
    for choices in built.leg_choices + built.acc_choices:
        for c in choices:
            cost_terms.append(c.var * c.cost_cents)
            if c.penalty:
                penalty_terms.append(c.var * c.penalty)
    for day_slots in built.meal_slots:
        for slot_choices in day_slots.values():
            for c in slot_choices:
                cost_terms.append(c.var * c.cost_cents)
                if c.penalty:
                    penalty_terms.append(c.var * c.penalty)
    built.cost_expr = sum(cost_terms)
    built.penalty_expr = sum(penalty_terms) if penalty_terms else 0
    built.all_choice_vars = [c.var for choices in built.leg_choices + built.acc_choices for c in choices]
    for day_slots in built.meal_slots:
        for slot_choices in day_slots.values():
            built.all_choice_vars.extend(c.var for c in slot_choices)
    for slots in built.attraction_slots:
        for slot_choices in slots:
            built.all_choice_vars.extend(c.var for c in slot_choices)

    # --- budget (assumption-guarded) ---
    if with_budget:
        budget_lit = model.new_bool_var("assume_budget")
        model.add(built.cost_expr <= _cents(spec["budget"])).only_enforce_if(budget_lit)
        model.add_assumption(budget_lit)
        built.assumption_names[budget_lit.index] = ("budget", None)

    # --- cuisine coverage (assumption-guarded, one literal per cuisine) ---
    # The official evaluator skips meals in the origin city; every meal pool
    # here is a visiting city, so all chosen meals count. Membership mirrors
    # the official substring test `cuisine in row['Cuisines']`.
    cuisines = spec.get("cuisines") or []
    if cuisines:
        # Coverage sum needs, per cuisine, every slot var of restaurants
        # offering it (uniqueness is already enforced plan-wide).
        rest_to_vars = {}
        for (city, r), vars_ in restaurant_use.items():
            rest_to_vars[(city, r)] = vars_
        for cuisine in cuisines:
            covering = []
            for city in ref.cities + [ref.origin]:
                for r, rest in enumerate(ref.restaurants.get(city, [])):
                    if cuisine in str(rest["Cuisines"]):
                        covering.extend(rest_to_vars.get((city, r), []))
            lit = model.new_bool_var("assume_cuisine_{}".format(len(built.assumption_names)))
            if covering:
                model.add(sum(covering) >= 1).only_enforce_if(lit)
            else:
                # No restaurant anywhere offers this cuisine: always conflicting.
                model.add(0 >= 1).only_enforce_if(lit)
            model.add_assumption(lit)
            built.assumption_names[lit.index] = ("cuisine", cuisine)

    model.minimize(built.cost_expr * _BIG + built.penalty_expr)
    return built


def _new_solver(time_limit_ms):
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1  # deterministic
    solver.parameters.random_seed = 0
    if time_limit_ms:
        solver.parameters.max_time_in_seconds = time_limit_ms / 1000.0
    return solver


def _render_candidate(built, solver, ref, spec):
    """Render one feasible solution into official plan JSON + cost info."""
    people = spec["peopleNumber"]
    days = ref.days
    plan = []
    transport_cost = 0
    meal_cost = 0
    acc_cost = 0

    leg_strings = []
    for i, choices in enumerate(built.leg_choices):
        chosen = next(c for c in choices if solver.value(c.var))
        leg_strings.append(chosen.render)
        transport_cost += chosen.cost_cents

    acc_strings = []  # per city
    for choices in built.acc_choices:
        chosen = next(c for c in choices if solver.value(c.var))
        acc_strings.append(chosen.render)
        acc_cost += chosen.cost_cents

    for day in range(1, days + 1):
        entry = {"days": day}
        if day % 2 == 1:
            # travel day: leg index = day//2 (day1 -> leg0, day3 -> leg1, ...)
            leg = ref.legs[day // 2]
            entry["current_city"] = "from {} to {}".format(leg.origin, leg.dest)
            entry["transportation"] = leg_strings[day // 2]
        else:
            entry["current_city"] = ref.cities[day // 2 - 1]
            entry["transportation"] = "-"

        day_meals = built.meal_slots[day - 1]
        for slot_name, key in (("breakfast", "breakfast"), ("lunch", "lunch"), ("dinner", "dinner")):
            chosen = [c for c in day_meals[slot_name] if solver.value(c.var)]
            if chosen:
                entry[key] = chosen[0].render
                meal_cost += chosen[0].cost_cents
            else:
                entry[key] = "-"

        slots = built.attraction_slots[day - 1]
        chosen_attrs = []
        for slot_choices in slots:
            chosen = [c for c in slot_choices if solver.value(c.var)]
            if chosen:
                chosen_attrs.append(chosen[0].render)
        entry["attraction"] = "".join(a + ";" for a in chosen_attrs) if chosen_attrs else "-"

        if day == days:
            entry["accommodation"] = "-"
        elif day % 2 == 1:
            entry["accommodation"] = acc_strings[(day + 1) // 2 - 1]  # arrival city c_{(day+1)/2}
        else:
            entry["accommodation"] = acc_strings[day // 2 - 1]
        plan.append(entry)

    total = transport_cost + meal_cost + acc_cost
    return {
        "plan": plan,
        "totalCost": total / 100.0,
        "costBreakdown": {
            "transportation": transport_cost / 100.0,
            "meals": meal_cost / 100.0,
            "accommodation": acc_cost / 100.0,
            "total": total / 100.0,
        },
    }


def _suggest_relaxations(conflict):
    """Human-actionable relaxations derived from the conflict's involved fields."""
    constraint = conflict["constraint"]
    suggestions = []
    if constraint == "budget":
        if "minimum feasible plan cost" in conflict["explanation"]:
            suggestions.append("raise $.budget to at least the minimum feasible plan cost in the explanation")
        else:
            suggestions.append("raise $.budget")
    elif constraint.startswith("cuisine:"):
        suggestions.append("drop cuisine {!r} from $.cuisines".format(constraint.split(":", 1)[1]))
    for field in conflict.get("involvedFields") or []:
        suggestion = "relax or remove $.{}".format(field)
        if suggestion not in suggestions and field != "cuisines":
            suggestions.append(suggestion)
    return suggestions


def _attach_suggestions(conflicts):
    for conflict in conflicts:
        conflict["suggestedRelaxations"] = _suggest_relaxations(conflict)
    return conflicts


def _solver_meta(solve_ms, time_limit_ms):
    return {"engine": "cp-sat", "solveMs": solve_ms, "timeLimitMs": time_limit_ms}


def _diagnose_infeasible(ref, spec, built, solver, solve_ms, time_limit_ms, remaining_ms):
    """Map an infeasibility core back to human-readable conflicts.

    ``time_limit_ms`` is the effective request limit (echoed in solverMeta);
    ``remaining_ms`` is what is left of it for the relaxed re-solve.
    """
    core = set(solver.sufficient_assumptions_for_infeasibility())
    conflicts = []
    budget_in_core = False
    for lit_index in core:
        kind_value = built.assumption_names.get(lit_index)
        if kind_value is None:
            continue
        kind, value = kind_value
        if kind == "budget":
            budget_in_core = True
        elif kind == "cuisine":
            conflicts.append(
                {
                    "constraint": "cuisine:{}".format(value),
                    "explanation": (
                        "no combination of distinct restaurants from the ref-info pools can cover "
                        "cuisine {!r} alongside the other constraints".format(value)
                    ),
                    "involvedFields": ["cuisines"],
                }
            )

    if budget_in_core:
        # Re-solve once without the budget assumption to report the min feasible cost.
        relaxed = _build_model(ref, spec, with_budget=False)
        relaxed_solver = _new_solver(remaining_ms)
        status = relaxed_solver.solve(relaxed.model)
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            min_cost = relaxed_solver.value(relaxed.cost_expr) / 100.0
            conflicts.append(
                {
                    "constraint": "budget",
                    "explanation": (
                        "budget {:.2f} is below the minimum feasible plan cost {:.2f} "
                        "(cheapest combination of transport, meals and accommodation)".format(
                            float(spec["budget"]), min_cost
                        )
                    ),
                    "involvedFields": ["budget"],
                }
            )
        else:
            conflicts.append(
                {
                    "constraint": "budget",
                    "explanation": "budget {:.2f} cannot be met".format(float(spec["budget"])),
                    "involvedFields": ["budget"],
                }
            )

    if not conflicts:
        conflicts.append(
            {
                "constraint": "structural:unknown",
                "explanation": "solver reported infeasibility without an assumption core",
                "involvedFields": [],
            }
        )
    return {
        "status": "infeasible",
        "conflicts": _attach_suggestions(conflicts),
        "solverMeta": _solver_meta(solve_ms, time_limit_ms),
    }


def solve(ref, spec, max_candidates=3, time_limit_ms=None):
    """Solve one query. ``ref`` is a loader.RefData; ``spec`` a validated ConstraintSpec.

    Returns {"status": "optimal"|"feasible_timeout", "candidates": [...], "solverMeta": {...}},
    {"status": "infeasible", "conflicts": [...], "solverMeta": {...}} or
    {"status": "error", "error": <msg>, "solverMeta": {...}}.

    ``time_limit_ms`` bounds the whole call: candidate re-solves share the
    budget, and a solve cut off at the limit still returns its best feasible
    plan with status ``feasible_timeout``.
    """
    start = time.monotonic()

    def remaining_ms():
        if time_limit_ms is None:
            return None
        # clamp to >= 1: _new_solver treats 0/None as "no limit"
        return max(1, time_limit_ms - int((time.monotonic() - start) * 1000))

    spec_errors = spec_module.validate_spec(spec)
    if spec_errors:
        return {
            "status": "error",
            "error": "invalid spec: " + "; ".join(spec_errors),
            "solverMeta": _solver_meta(0, time_limit_ms),
        }

    structural = _structural_conflicts(ref, spec)
    if structural:
        return {
            "status": "infeasible",
            "conflicts": _attach_suggestions(structural),
            "solverMeta": _solver_meta(int((time.monotonic() - start) * 1000), time_limit_ms),
        }

    built = _build_model(ref, spec, with_budget=True)
    candidates = []
    status = None
    best_status = None
    for _ in range(max(1, max_candidates)):
        solver = _new_solver(remaining_ms())
        status = solver.solve(built.model)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            break
        if best_status is None:
            best_status = status
        candidates.append(_render_candidate(built, solver, ref, spec))
        # no-good cut over all choice booleans: forbid this exact combination
        chosen = [c.var for choices in built.leg_choices + built.acc_choices for c in choices if solver.value(c.var)]
        for day_slots in built.meal_slots:
            for slot_choices in day_slots.values():
                chosen.extend(c.var for c in slot_choices if solver.value(c.var))
        for slots in built.attraction_slots:
            for slot_choices in slots:
                chosen.extend(c.var for c in slot_choices if solver.value(c.var))
        built.model.add(sum(chosen) <= len(chosen) - 1)

    solve_ms = int((time.monotonic() - start) * 1000)

    if not candidates:
        if status == cp_model.INFEASIBLE:
            return _diagnose_infeasible(ref, spec, built, solver, solve_ms, time_limit_ms, remaining_ms())
        return {
            "status": "error",
            "error": "solver finished without a solution (status {})".format(status),
            "solverMeta": _solver_meta(solve_ms, time_limit_ms),
        }

    return {
        # OPTIMAL proves cheapest; FEASIBLE means the time limit cut the search
        # off and the best plan found so far is returned unproved.
        "status": "optimal" if best_status == cp_model.OPTIMAL else "feasible_timeout",
        "candidates": candidates,
        "solverMeta": _solver_meta(solve_ms, time_limit_ms),
    }


def solve_query(query_id, data_dir, spec, max_candidates=3, time_limit_ms=None, db_dir=None):
    """Convenience wrapper: load ref info for ``query_id`` then solve."""
    ref = loader.load_ref_data(query_id, data_dir, db_dir)
    return solve(ref, spec, max_candidates=max_candidates, time_limit_ms=time_limit_ms)
