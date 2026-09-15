"""Tests for verify.verify_plan, including corrupted-plan negatives (ported)."""

import copy
import json

from solver_service.packs.travelplanner import loader
from solver_service.packs.travelplanner import solve as solve_module
from solver_service.packs.travelplanner import verify as verify_module


def _solved(data_dir, spec):
    ref = loader.load_ref_data("validation_0", data_dir)
    res = solve_module.solve(ref, spec, max_candidates=1)
    assert res["status"] == "optimal"
    return ref, res["candidates"][0]


def _results_by_name(outcome):
    return {r["constraint"]: r for r in outcome["results"]}


def test_solved_plan_passes_all_13(data_dir, oracle_spec_validation_0):
    ref, candidate = _solved(data_dir, oracle_spec_validation_0)
    outcome = verify_module.verify_plan(candidate["plan"], oracle_spec_validation_0, ref)
    assert len(outcome["results"]) == 13
    assert outcome["pass"], json.dumps(outcome, indent=2)


def test_repeated_restaurant_fails_diverse_restaurants(data_dir, oracle_spec_validation_0):
    ref, candidate = _solved(data_dir, oracle_spec_validation_0)
    plan = copy.deepcopy(candidate["plan"])
    plan[1]["lunch"] = plan[1]["breakfast"]  # repeat across days
    outcome = verify_module.verify_plan(plan, oracle_spec_validation_0, ref)
    results = _results_by_name(outcome)
    assert not outcome["pass"]
    assert not results["is_valid_restaurants"]["pass"]


def test_other_city_restaurant_fails_current_city(data_dir, oracle_spec_validation_0):
    ref, candidate = _solved(data_dir, oracle_spec_validation_0)
    plan = copy.deepcopy(candidate["plan"])
    name = plan[1]["dinner"].split(",")[0]
    plan[1]["dinner"] = "{}, Washington".format(name)  # stay day is Myrtle Beach only
    outcome = verify_module.verify_plan(plan, oracle_spec_validation_0, ref)
    results = _results_by_name(outcome)
    assert not outcome["pass"]
    assert not results["is_valid_information_in_current_city"]["pass"]


def test_budget_below_plan_cost_fails_valid_cost(data_dir, oracle_spec_validation_0):
    ref, candidate = _solved(data_dir, oracle_spec_validation_0)
    spec = dict(oracle_spec_validation_0, budget=candidate["totalCost"] - 1)
    outcome = verify_module.verify_plan(candidate["plan"], spec, ref)
    results = _results_by_name(outcome)
    assert not outcome["pass"]
    assert not results["valid_cost"]["pass"]


def test_missing_accommodation_day1_fails_not_absent(data_dir, oracle_spec_validation_0):
    ref, candidate = _solved(data_dir, oracle_spec_validation_0)
    plan = copy.deepcopy(candidate["plan"])
    plan[0]["accommodation"] = "-"
    outcome = verify_module.verify_plan(plan, oracle_spec_validation_0, ref)
    results = _results_by_name(outcome)
    assert not outcome["pass"]
    assert not results["is_not_absent"]["pass"]


def test_flight_plan_fails_no_flight_constraint(data_dir, oracle_spec_validation_0):
    # 'no self-driving' makes flights the cheapest option (164+87 < 2x693 taxi),
    # so the optimal plan is flight-based; verifying it against a 'no flight'
    # spec must then fail the hard valid_transportation check.
    ref = loader.load_ref_data("validation_0", data_dir)
    flight_spec = dict(oracle_spec_validation_0, transportation="no self-driving")
    res = solve_module.solve(ref, flight_spec, max_candidates=1)
    assert res["status"] == "optimal"
    flight_plan = res["candidates"][0]["plan"]
    assert any("Flight Number" in day["transportation"] for day in flight_plan)
    spec = dict(oracle_spec_validation_0, transportation="no flight")
    outcome = verify_module.verify_plan(flight_plan, spec, ref)
    results = _results_by_name(outcome)
    assert not outcome["pass"]
    assert not results["valid_transportation"]["pass"]


def test_room_type_violation_detected(data_dir, oracle_spec_validation_0):
    ref = loader.load_ref_data("validation_0", data_dir)
    # pick a 'Private room' accommodation from ref info, build a minimal valid-ish
    # plan around it, then verify against roomType 'entire room'.
    private = next(
        a for a in ref.accommodations["Myrtle Beach"] if a["room type"] == "Private room" and a["minimum nights"] <= 2
    )
    res = solve_module.solve(ref, oracle_spec_validation_0, max_candidates=1)
    plan = copy.deepcopy(res["candidates"][0]["plan"])
    plan[0]["accommodation"] = "{}, Myrtle Beach".format(private["NAME"])
    plan[1]["accommodation"] = "{}, Myrtle Beach".format(private["NAME"])
    spec = dict(oracle_spec_validation_0, roomType="entire room")
    outcome = verify_module.verify_plan(plan, spec, ref)
    results = _results_by_name(outcome)
    assert not results["valid_room_type"]["pass"]


def test_min_nights_quirk_single_match_enforced(data_dir, oracle_spec_validation_0):
    # an accommodation with minimum nights = 5 stays only 2 nights in the plan;
    # the quirk (single matching row) means the check IS enforced here.
    ref = loader.load_ref_data("validation_0", data_dir)
    picky = next(a for a in ref.accommodations["Myrtle Beach"] if a["minimum nights"] > 2)
    matches = [a for a in ref.accommodations["Myrtle Beach"] if picky["NAME"] in str(a["NAME"])]
    assert len(matches) == 1, "test needs a uniquely-named accommodation"
    res = solve_module.solve(ref, oracle_spec_validation_0, max_candidates=1)
    plan = copy.deepcopy(res["candidates"][0]["plan"])
    plan[0]["accommodation"] = "{}, Myrtle Beach".format(picky["NAME"])
    plan[1]["accommodation"] = "{}, Myrtle Beach".format(picky["NAME"])
    outcome = verify_module.verify_plan(plan, oracle_spec_validation_0, ref)
    results = _results_by_name(outcome)
    assert not results["is_valid_accommodation"]["pass"]
