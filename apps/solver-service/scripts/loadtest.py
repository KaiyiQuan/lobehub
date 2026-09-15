#!/usr/bin/env python3
"""Reproducible load test for the deployed solver service.

Stdlib-only. Reads SOLVER_SERVICE_URL and SOLVER_SERVICE_API_KEY from the
environment. Runs `solve` or `verify` against the live HTTPS URL at a fixed
concurrency for a fixed request count, round-robin over a fixed mix of 8
feasible TravelPlanner validation specs (generated once from
data/validation_ref_info.jsonl and pinned below). For `verify`, plans are
obtained by solving each spec once during warmup (solves are deterministic).

Usage:
  SOLVER_SERVICE_URL=... SOLVER_SERVICE_API_KEY=... \
    python3 scripts/loadtest.py --op solve --concurrency 8 --requests 96 --out results/solve-c8.json

Output: JSON summary (throughput, p50/p95/p99, error rate) plus the raw
per-request latency list, written to --out.
"""

import argparse
import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request

# Fixed mix: 8 feasible validation specs (single-city, 3 days, 1 person).
# Budgets confirmed above each query's minimum feasible plan cost.
SPEC_MIX = [
    {"queryId": "validation_0", "maxCandidates": 1, "spec": {
        "origin": "Washington", "destination": {"type": "city", "name": "Myrtle Beach"},
        "days": 3, "startDate": "2022-03-13", "visitingCityNumber": 1, "peopleNumber": 1,
        "budget": 1400, "houseRule": None, "roomType": None, "cuisines": None,
        "transportation": None, "soft": None}},
    {"queryId": "validation_1", "maxCandidates": 1, "spec": {
        "origin": "Oakland", "destination": {"type": "city", "name": "Tucson"},
        "days": 3, "startDate": "2022-03-15", "visitingCityNumber": 1, "peopleNumber": 1,
        "budget": 1800, "houseRule": None, "roomType": None, "cuisines": ["Mexican"],
        "transportation": None, "soft": None}},
    {"queryId": "validation_2", "maxCandidates": 1, "spec": {
        "origin": "Buffalo", "destination": {"type": "city", "name": "Atlanta"},
        "days": 3, "startDate": "2022-03-02", "visitingCityNumber": 1, "peopleNumber": 1,
        "budget": 2200, "houseRule": None, "roomType": None, "cuisines": None,
        "transportation": None, "soft": None}},
    {"queryId": "validation_3", "maxCandidates": 1, "spec": {
        "origin": "Ontario", "destination": {"type": "city", "name": "Honolulu"},
        "days": 3, "startDate": "2022-03-04", "visitingCityNumber": 1, "peopleNumber": 1,
        "budget": 3000, "houseRule": None, "roomType": None, "cuisines": None,
        "transportation": None, "soft": None}},
    {"queryId": "validation_4", "maxCandidates": 1, "spec": {
        "origin": "West Palm Beach", "destination": {"type": "city", "name": "Atlanta"},
        "days": 3, "startDate": "2022-03-13", "visitingCityNumber": 1, "peopleNumber": 1,
        "budget": 1500, "houseRule": None, "roomType": "entire room", "cuisines": None,
        "transportation": None, "soft": None}},
    {"queryId": "validation_5", "maxCandidates": 1, "spec": {
        "origin": "Detroit", "destination": {"type": "city", "name": "San Diego"},
        "days": 3, "startDate": "2022-03-05", "visitingCityNumber": 1, "peopleNumber": 1,
        "budget": 3000, "houseRule": None, "roomType": None, "cuisines": None,
        "transportation": None, "soft": None}},
    {"queryId": "validation_6", "maxCandidates": 1, "spec": {
        "origin": "Missoula", "destination": {"type": "city", "name": "Dallas"},
        "days": 3, "startDate": "2022-03-23", "visitingCityNumber": 1, "peopleNumber": 1,
        "budget": 2400, "houseRule": None, "roomType": None, "cuisines": None,
        "transportation": None, "soft": None}},
    {"queryId": "validation_7", "maxCandidates": 1, "spec": {
        "origin": "Boston", "destination": {"type": "city", "name": "San Juan"},
        "days": 3, "startDate": "2022-03-28", "visitingCityNumber": 1, "peopleNumber": 1,
        "budget": 3000, "houseRule": None, "roomType": None, "cuisines": None,
        "transportation": None, "soft": None}},
]

TIMEOUT_S = 60


def post(url, key, path, body):
    req = urllib.request.Request(
        url + path,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as res:
            payload = json.load(res)
            return time.perf_counter() - t0, res.status, payload, None
    except urllib.error.HTTPError as e:
        return time.perf_counter() - t0, e.code, None, f"http {e.code}"
    except Exception as e:  # noqa: BLE001 - load test records any failure
        return time.perf_counter() - t0, 0, None, repr(e)


def percentile(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def run(op, concurrency, requests, url, key):
    bodies = []
    if op == "solve":
        bodies = list(SPEC_MIX)
    else:  # verify: warm up by solving each spec once, reuse the returned plan
        print("warmup: solving each spec once to obtain plans for verify", flush=True)
        for item in SPEC_MIX:
            lat, status, payload, err = post(url, key, "/v1/packs/travelplanner/solve", item)
            if err or payload.get("status") not in ("optimal", "feasible_timeout"):
                print(f"warmup solve failed for {item['queryId']}: {err or payload}", file=sys.stderr)
                sys.exit(2)
            bodies.append({"queryId": item["queryId"], "spec": item["spec"],
                           "plan": payload["candidates"][0]["plan"]})
    path = f"/v1/packs/travelplanner/{op}"

    results = []  # (latency_s, http_status, error)
    lock = threading.Lock()
    counter = {"i": 0}

    def worker():
        while True:
            with lock:
                i = counter["i"]
                counter["i"] += 1
            if i >= requests:
                return
            lat, status, _payload, err = post(url, key, path, bodies[i % len(bodies)])
            with lock:
                results.append((lat, status, err))

    t0 = time.perf_counter()
    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - t0

    ok = [lat for lat, status, err in results if err is None and status == 200]
    err_count = len(results) - len(ok)
    lat_ms = sorted(lat * 1000 for lat in ok)
    summary = {
        "url": url,
        "op": op,
        "concurrency": concurrency,
        "requests_planned": requests,
        "requests_done": len(results),
        "ok": len(ok),
        "errors": err_count,
        "error_rate": err_count / len(results) if results else None,
        "wall_s": round(wall, 3),
        "throughput_rps": round(len(results) / wall, 3) if wall else None,
        "latency_ms": {
            "min": round(lat_ms[0], 1) if lat_ms else None,
            "p50": round(percentile(lat_ms, 0.50), 1) if lat_ms else None,
            "p95": round(percentile(lat_ms, 0.95), 1) if lat_ms else None,
            "p99": round(percentile(lat_ms, 0.99), 1) if lat_ms else None,
            "max": round(lat_ms[-1], 1) if lat_ms else None,
            "mean": round(statistics.fmean(lat_ms), 1) if lat_ms else None,
        },
        "error_samples": [{"status": s, "error": e} for _l, s, e in results if e is not None or s != 200][:5],
        "raw_latencies_ms": [round(v, 1) for v in lat_ms],
    }
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--op", choices=["solve", "verify"], required=True)
    ap.add_argument("--concurrency", type=int, required=True)
    ap.add_argument("--requests", type=int, required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    url = os.environ.get("SOLVER_SERVICE_URL", "").rstrip("/")
    key = os.environ.get("SOLVER_SERVICE_API_KEY", "")
    if not url or not key:
        sys.exit("SOLVER_SERVICE_URL and SOLVER_SERVICE_API_KEY must be set")

    summary = run(args.op, args.concurrency, args.requests, url, key)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(summary, f, indent=1)
    print(json.dumps({k: v for k, v in summary.items() if k != "raw_latencies_ms"}, indent=1))


if __name__ == "__main__":
    main()
