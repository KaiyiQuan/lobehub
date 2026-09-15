"""Tests for solve.solve (CP-SAT engine), ported from the experiment solver.

Status contract changed for the service: "feasible" is now reported as
"optimal" (optimality proved) or "feasible_timeout" (best feasible plan found
before the per-request time limit). Without a time limit these small models
always prove optimality, so the ported assertions expect "optimal".
"""

import json

from solver_service.packs.travelplanner import loader
from solver_service.packs.travelplanner import solve as solve_module
from solver_service.packs.travelplanner import verify as verify_module


def _solve(data_dir, query_id, spec, **kwargs):
    ref = loader.load_ref_data(query_id, data_dir)
    return ref, solve_module.solve(ref, spec, **kwargs)


def test_oracle_spec_optimal(data_dir, oracle_spec_validation_0):
    ref, res = _solve(data_dir, "validation_0", oracle_spec_validation_0, max_candidates=3)
    assert res["status"] == "optimal"
    assert res["solverMeta"]["engine"] == "cp-sat"
    assert 1 <= len(res["candidates"]) <= 3

    rendered = set()
    for candidate in res["candidates"]:
        assert candidate["totalCost"] <= 1400
        breakdown = candidate["costBreakdown"]
        assert abs(breakdown["transportation"] + breakdown["meals"] + breakdown["accommodation"] - breakdown["total"]) < 1e-9
        assert abs(breakdown["total"] - candidate["totalCost"]) < 1e-9
        rendered.add(json.dumps(candidate["plan"], sort_keys=True))
        # every candidate passes the independent verifier on all 13 constraints
        outcome = verify_module.verify_plan(candidate["plan"], oracle_spec_validation_0, ref)
        assert outcome["pass"], json.dumps(outcome, indent=2)
    assert len(rendered) == len(res["candidates"]), "candidates must be distinct"


def test_budget_infeasible_reports_min_cost(data_dir, oracle_spec_validation_0):
    spec = dict(oracle_spec_validation_0, budget=100)
    _, res = _solve(data_dir, "validation_0", spec, max_candidates=1)
    assert res["status"] == "infeasible"
    budget_conflicts = [c for c in res["conflicts"] if c["constraint"] == "budget"]
    assert budget_conflicts, res["conflicts"]
    assert "budget" in budget_conflicts[0]["involvedFields"]
    # the explanation names the minimum feasible cost
    assert "minimum feasible plan cost" in budget_conflicts[0]["explanation"]
    # and that cost is itself achievable without the budget assumption
    _, feasible = _solve(data_dir, "validation_0", oracle_spec_validation_0, max_candidates=1)
    min_cost = feasible["candidates"][0]["totalCost"]
    assert "{:.2f}".format(min_cost) in budget_conflicts[0]["explanation"]
    # structured suggestion for the repair loop
    assert budget_conflicts[0]["suggestedRelaxations"]


def test_structural_conflict_empty_accommodation_domain(data_dir, oracle_spec_validation_0):
    # 'shared room' + 'pets' empties the Myrtle Beach accommodation domain
    # (only Private room / Entire home/apt exist there).
    spec = dict(oracle_spec_validation_0, roomType="shared room", houseRule="pets")
    _, res = _solve(data_dir, "validation_0", spec, max_candidates=1)
    assert res["status"] == "infeasible"
    structural = [c for c in res["conflicts"] if c["constraint"].startswith("structural:")]
    assert structural
    assert "Myrtle Beach" in structural[0]["explanation"]
    assert "roomType" in structural[0]["involvedFields"]
    assert any("roomType" in s for s in structural[0]["suggestedRelaxations"])


def test_no_flight_with_tiny_budget_infeasible(data_dir, oracle_spec_validation_0):
    # without flights the cheapest transport is self-driving (34/leg);
    # a $50 budget is still impossible -> budget conflict, not structural.
    spec = dict(oracle_spec_validation_0, transportation="no flight", budget=50)
    _, res = _solve(data_dir, "validation_0", spec, max_candidates=1)
    assert res["status"] == "infeasible"
    assert any(c["constraint"] == "budget" for c in res["conflicts"])


def test_no_flight_optimal_uses_driving_or_taxi(data_dir, oracle_spec_validation_0):
    spec = dict(oracle_spec_validation_0, transportation="no flight")
    ref, res = _solve(data_dir, "validation_0", spec, max_candidates=1)
    assert res["status"] == "optimal"
    for day in res["candidates"][0]["plan"]:
        assert "Flight" not in day["transportation"]
    outcome = verify_module.verify_plan(res["candidates"][0]["plan"], spec, ref)
    assert outcome["pass"], json.dumps(outcome, indent=2)


def test_cuisine_constraint_satisfied(data_dir, oracle_spec_validation_0):
    spec = dict(oracle_spec_validation_0, cuisines=["Indian"])
    ref, res = _solve(data_dir, "validation_0", spec, max_candidates=1)
    assert res["status"] == "optimal"
    meals = [
        day[key]
        for day in res["candidates"][0]["plan"]
        for key in ("breakfast", "lunch", "dinner")
        if day[key] != "-"
    ]
    indian = [r for r in ref.restaurants["Myrtle Beach"] if "Indian" in str(r["Cuisines"])]
    assert any(any(r["Name"] in m for r in indian) for m in meals)
    outcome = verify_module.verify_plan(res["candidates"][0]["plan"], spec, ref)
    assert outcome["pass"], json.dumps(outcome, indent=2)


def test_impossible_cuisine_infeasible(data_dir, oracle_spec_validation_0):
    spec = dict(oracle_spec_validation_0, cuisines=["Zzz Not A Cuisine"])
    _, res = _solve(data_dir, "validation_0", spec, max_candidates=1)
    assert res["status"] == "infeasible"
    cuisine = [c for c in res["conflicts"] if c["constraint"] == "cuisine:Zzz Not A Cuisine"]
    assert cuisine
    assert any("Zzz Not A Cuisine" in s for s in cuisine[0]["suggestedRelaxations"])


def test_spec_error_status(data_dir, oracle_spec_validation_0):
    spec = dict(oracle_spec_validation_0, days=4)
    _, res = _solve(data_dir, "validation_0", spec, max_candidates=1)
    assert res["status"] == "error"


def test_time_limit_returns_best_feasible_or_times_out(data_dir, oracle_spec_validation_0):
    """With a 1 ms budget the solve is cut off: either CP-SAT already has a
    feasible plan (status feasible_timeout, plan must still verify) or it has
    nothing (status error). It must never come back 'optimal' from a cut-off
    solve, and the effective limit is echoed in solverMeta."""
    ref, res = _solve(data_dir, "validation_0", oracle_spec_validation_0, max_candidates=1, time_limit_ms=1)
    assert res["solverMeta"]["timeLimitMs"] == 1
    assert res["status"] in ("feasible_timeout", "error")
    if res["status"] == "feasible_timeout":
        assert res["candidates"]
        outcome = verify_module.verify_plan(res["candidates"][0]["plan"], oracle_spec_validation_0, ref)
        assert outcome["pass"], json.dumps(outcome, indent=2)


def test_time_limit_shares_budget_across_candidates(data_dir, oracle_spec_validation_0):
    """The limit bounds the whole call: 3 candidates with a 30 s budget never
    take materially longer than the budget."""
    _, res = _solve(data_dir, "validation_0", oracle_spec_validation_0, max_candidates=3, time_limit_ms=30000)
    assert res["status"] == "optimal"
    assert res["solverMeta"]["timeLimitMs"] == 30000
    assert res["solverMeta"]["solveMs"] <= 30000 + 5000  # build/render overhead slack


def test_no_conflicting_transportation_mix_regression(data_dir):
    """Regression: the solver used to pick transport modes per leg independently,
    producing plans that mix Self-driving+Flight across legs, which the official
    commonsense is_valid_transportation rejects (found via oracle run train_27).
    Spec below is written from the train_27 query text (7-day California trip)."""
    spec = {
        "origin": "St. Louis",
        "destination": {"type": "state", "name": "California"},
        "days": 7,
        "startDate": "2022-03-05",
        "visitingCityNumber": 3,
        "peopleNumber": 2,
        "budget": 9500,
        "houseRule": None,
        "roomType": None,
        "cuisines": ["American", "Chinese"],
        "transportation": None,
        "soft": None,
    }
    ref, res = _solve(data_dir, "train_27", spec, max_candidates=3)
    assert res["status"] == "optimal"
    for candidate in res["candidates"]:
        modes = set()
        for day in candidate["plan"]:
            value = day["transportation"]
            if value == "-":
                continue
            if "flight" in value.lower():
                modes.add("flight")
            elif "self-driving" in value.lower():
                modes.add("self-driving")
            elif "taxi" in value.lower():
                modes.add("taxi")
        assert not ("flight" in modes and "self-driving" in modes)
        assert not ("taxi" in modes and "self-driving" in modes)
        outcome = verify_module.verify_plan(candidate["plan"], spec, ref)
        assert outcome["pass"], json.dumps(outcome, indent=2)
