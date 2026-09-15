"""Concurrency test: N parallel solves against a real multi-worker uvicorn.

Spawns ``uvicorn solver_service.asgi:app --workers 4`` as a subprocess (only
the validation split preloaded, to keep startup fast) and shows that 8 parallel
solves finish in well under 8 x single-request latency — i.e. CPU-bound solves
in separate worker processes are not serialized behind one event loop — while
/health stays responsive under load.
"""

import os
import subprocess
import sys
import time

import httpx
import pytest

from concurrent.futures import ThreadPoolExecutor

from .conftest import ROOT, DATA_DIR, data_available

pytestmark = pytest.mark.skipif(not data_available(), reason="reference data missing; run scripts/fetch_data.py")

API_KEY = "concurrency-test-key"
PORT = 18099
BASE = "http://127.0.0.1:{}".format(PORT)
WORKERS = 4
PARALLEL = 8

SOLVE_BODY = {
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


@pytest.fixture(scope="module")
def server():
    env = dict(
        os.environ,
        PYTHONPATH=os.path.join(ROOT, "src"),
        SOLVER_SERVICE_API_KEY=API_KEY,
        SOLVER_TP_DATA_DIR=DATA_DIR,
        SOLVER_TP_SPLITS="validation",
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "solver_service.asgi:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(PORT),
            "--workers",
            str(WORKERS),
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 60
        while True:
            if proc.poll() is not None:
                raise RuntimeError("uvicorn exited early:\n{}".format(proc.stdout.read()))
            try:
                res = httpx.get(BASE + "/health", timeout=1)
                if res.status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError("uvicorn did not become healthy in 60 s")
            time.sleep(0.2)
        yield BASE
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _solve(client):
    start = time.monotonic()
    res = client.post(
        "/v1/packs/travelplanner/solve",
        json=SOLVE_BODY,
        headers={"Authorization": "Bearer {}".format(API_KEY)},
        timeout=60,
    )
    elapsed = time.monotonic() - start
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "optimal"
    return elapsed


def test_parallel_solves_are_not_serialized(server):
    headers = {"Authorization": "Bearer {}".format(API_KEY)}
    with httpx.Client(base_url=server, headers=headers) as client:
        # warmup + single-request latency (median of 3)
        singles = sorted(_solve(client) for _ in range(3))
        single = singles[1]

        # health stays responsive while 8 solves are in flight (one retry: the
        # first health hit can land on a worker still finishing its preload)
        with ThreadPoolExecutor(max_workers=PARALLEL + 1) as pool:
            solve_futures = [pool.submit(_solve, client) for _ in range(PARALLEL)]

            def _health():
                for _ in range(2):
                    try:
                        return client.get("/health", timeout=15)
                    except httpx.TransportError:
                        time.sleep(0.5)
                return client.get("/health", timeout=15)

            health_future = pool.submit(_health)
            wall_start = time.monotonic()
            latencies = [f.result() for f in solve_futures]
            wall = time.monotonic() - wall_start
            health_res = health_future.result()

        assert health_res.status_code == 200

    serial_estimate = PARALLEL * single
    print(
        "\nsingle median: {:.0f} ms | {} parallel solves wall: {:.0f} ms "
        "(serial estimate {:.0f} ms, speedup {:.1f}x, workers={})".format(
            single * 1000, PARALLEL, wall * 1000, serial_estimate * 1000, serial_estimate / wall, WORKERS
        )
    )
    # With 4 workers the ideal wall is ~2x single; require well under serial
    # (half of it) to prove solves are not serialized behind one event loop.
    assert wall < serial_estimate * 0.5
