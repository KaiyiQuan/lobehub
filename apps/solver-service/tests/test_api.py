"""HTTP API tests: auth, size limit, time limit, infeasibility, verify round-trip."""

import pytest
from fastapi.testclient import TestClient

from solver_service import __version__
from solver_service.app import create_app
from solver_service.config import Settings

from .conftest import DATA_DIR, data_available

pytestmark = pytest.mark.skipif(not data_available(), reason="reference data missing; run scripts/fetch_data.py")

API_KEY = "test-api-key"


@pytest.fixture(scope="module")
def client():
    app = create_app(Settings(api_key=API_KEY, tp_data_dir=DATA_DIR, tp_splits=("validation",)))
    with TestClient(app) as client:
        yield client


def _auth(key=API_KEY):
    return {"Authorization": "Bearer {}".format(key)}


def _solve_body(**overrides):
    body = {
        "queryId": "validation_0",
        "spec": {
            "origin": "Washington",
            "destination": {"type": "city", "name": "Myrtle Beach"},
            "days": 3,
            "startDate": "2022-03-13",
            "visitingCityNumber": 1,
            "peopleNumber": 1,
            "budget": 1400,
            "houseRule": None,
            "roomType": None,
            "cuisines": None,
            "transportation": None,
            "soft": None,
        },
        "maxCandidates": 1,
    }
    body.update(overrides)
    return body


# --- health -----------------------------------------------------------------


def test_health_no_auth_reports_packs(client):
    res = client.get("/health")
    assert res.status_code == 200
    payload = res.json()
    assert payload["status"] == "ok"
    assert payload["version"] == __version__
    assert payload["packs"][0]["id"] == "travelplanner"
    assert payload["packs"][0]["queries"] == {"validation": 180}


# --- auth -------------------------------------------------------------------


def test_solve_rejects_missing_token(client):
    res = client.post("/v1/packs/travelplanner/solve", json=_solve_body())
    assert res.status_code == 401


def test_solve_rejects_wrong_token(client):
    res = client.post("/v1/packs/travelplanner/solve", json=_solve_body(), headers=_auth("wrong"))
    assert res.status_code == 401


def test_verify_rejects_missing_token(client):
    res = client.post("/v1/packs/travelplanner/verify", json=_solve_body(plan=[]))
    assert res.status_code == 401


def test_packs_listing_requires_auth(client):
    assert client.get("/v1/packs").status_code == 401
    res = client.get("/v1/packs", headers=_auth())
    assert res.status_code == 200
    assert res.json()["packs"][0]["id"] == "travelplanner"


def test_service_refuses_to_start_without_key():
    with pytest.raises(RuntimeError):
        create_app(Settings(api_key=None, dev_allow_no_auth=False, tp_data_dir=DATA_DIR))


def test_dev_flag_allows_unauthenticated_local_run():
    app = create_app(Settings(api_key=None, dev_allow_no_auth=True, tp_data_dir=DATA_DIR, tp_splits=("validation",)))
    with TestClient(app) as dev_client:
        res = dev_client.post("/v1/packs/travelplanner/solve", json=_solve_body())
        assert res.status_code == 200


# --- solve ------------------------------------------------------------------


def test_solve_optimal_round_trip(client):
    res = client.post("/v1/packs/travelplanner/solve", json=_solve_body(), headers=_auth())
    assert res.status_code == 200
    assert res.headers["x-request-id"]
    payload = res.json()
    assert payload["status"] == "optimal"
    assert payload["solverMeta"]["engine"] == "cp-sat"
    candidate = payload["candidates"][0]
    assert candidate["totalCost"] <= 1400

    verify = client.post(
        "/v1/packs/travelplanner/verify",
        json=_solve_body(plan=candidate["plan"]),
        headers=_auth(),
    )
    assert verify.status_code == 200
    outcome = verify.json()
    assert outcome["pass"] is True
    assert len(outcome["results"]) == 13


def test_verify_detects_corrupted_plan(client):
    solved = client.post("/v1/packs/travelplanner/solve", json=_solve_body(), headers=_auth()).json()
    plan = solved["candidates"][0]["plan"]
    plan[1]["lunch"] = plan[1]["breakfast"]  # repeated restaurant
    res = client.post("/v1/packs/travelplanner/verify", json=_solve_body(plan=plan), headers=_auth())
    outcome = res.json()
    assert outcome["pass"] is False
    failed = {r["constraint"] for r in outcome["results"] if not r["pass"]}
    assert "is_valid_restaurants" in failed


def test_unknown_pack_404(client):
    res = client.post("/v1/packs/nope/solve", json=_solve_body(), headers=_auth())
    assert res.status_code == 404


def test_bad_query_id_400(client):
    res = client.post("/v1/packs/travelplanner/solve", json=_solve_body(queryId="nope_0"), headers=_auth())
    assert res.status_code == 400


def test_out_of_range_query_id_400(client):
    res = client.post("/v1/packs/travelplanner/solve", json=_solve_body(queryId="validation_9999"), headers=_auth())
    assert res.status_code == 400


def test_invalid_spec_returns_structured_error(client):
    body = _solve_body()
    body["spec"]["days"] = 4
    res = client.post("/v1/packs/travelplanner/solve", json=body, headers=_auth())
    assert res.status_code == 200
    assert res.json()["status"] == "error"
    assert "invalid spec" in res.json()["error"]


# --- size limit -------------------------------------------------------------


def test_request_body_size_limit_413():
    app = create_app(Settings(api_key=API_KEY, max_request_bytes=256, tp_data_dir=DATA_DIR, tp_splits=("validation",)))
    with TestClient(app) as small_client:
        res = small_client.post("/v1/packs/travelplanner/solve", json=_solve_body(), headers=_auth())
        assert res.status_code == 413
        # health and small requests still pass
        assert small_client.get("/health").status_code == 200


# --- time limit -------------------------------------------------------------


def test_time_limit_capped_by_server_max(client):
    body = _solve_body(timeLimitMs=10_000_000)
    res = client.post("/v1/packs/travelplanner/solve", json=body, headers=_auth())
    assert res.status_code == 200
    assert res.json()["solverMeta"]["timeLimitMs"] == 30000  # Settings default


def test_time_limit_path(client):
    body = _solve_body(timeLimitMs=1)
    res = client.post("/v1/packs/travelplanner/solve", json=body, headers=_auth())
    assert res.status_code == 200
    payload = res.json()
    assert payload["solverMeta"]["timeLimitMs"] == 1
    # cut-off solve: best feasible plan so far, or an honest error — never "optimal"
    assert payload["status"] in ("feasible_timeout", "error")
    if payload["status"] == "feasible_timeout":
        assert payload["candidates"]


# --- structured infeasibility -----------------------------------------------


def test_infeasible_returns_structured_conflicts(client):
    body = _solve_body()
    body["spec"]["budget"] = 100
    res = client.post("/v1/packs/travelplanner/solve", json=body, headers=_auth())
    assert res.status_code == 200
    payload = res.json()
    assert payload["status"] == "infeasible"
    budget = [c for c in payload["conflicts"] if c["constraint"] == "budget"]
    assert budget
    conflict = budget[0]
    assert conflict["involvedFields"] == ["budget"]
    assert "minimum feasible plan cost" in conflict["explanation"]
    assert conflict["suggestedRelaxations"]


def test_infeasible_structural_conflict_maps_to_spec_fields(client):
    body = _solve_body()
    body["spec"]["roomType"] = "shared room"
    body["spec"]["houseRule"] = "pets"
    res = client.post("/v1/packs/travelplanner/solve", json=body, headers=_auth())
    payload = res.json()
    assert payload["status"] == "infeasible"
    structural = [c for c in payload["conflicts"] if c["constraint"].startswith("structural:")]
    assert structural
    assert "roomType" in structural[0]["involvedFields"]
    assert any("roomType" in s for s in structural[0]["suggestedRelaxations"])
