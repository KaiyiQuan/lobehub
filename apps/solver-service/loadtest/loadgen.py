#!/usr/bin/env python3
"""In-region load generator for the solver service (runs as a Railway service).

Why a service: a client far from the target region measures its own network
(the first load test from mainland China saw ~670 ms RTT). Deployed next to the
target, this process measures the service itself, over Railway private
networking (``http://<svc>.railway.internal:<port>``) or the public domain.

It is a small aiohttp control server; experiments are submitted as JSON and run
inside the container, results are polled back:

  GET  /health                 no auth; container resources (nproc, cgroup cpu/mem)
  POST /runs                   bearer LOADGEN_KEY; body = run plan (below) -> {"id"}
  GET  /runs/{id}              bearer; {"state": "running"|"done"|"failed", "result"}

Run plan::

  {"target": "http://solver-loadtest.railway.internal:8000",
   "apiKey": "<target bearer>",           # or env TARGET_API_KEY
   "scenarios": [                         # all scenarios run concurrently
     {"name": "solve-c64", "op": "solve", "mix": "representative",
      "mode": "closed", "concurrency": 64,  # closed loop: N in-flight at all times
      "durationS": 30, "warmupS": 5, "processes": 4,
      "maxCandidates": null, "timeLimitMs": null},
     {"name": "open-200", "op": "verify", "mode": "open", "rps": 200, ...}
   ]}

- ``mix`` names a list in mix.json (entries: queryId, spec, maxCandidates,
  class). Requests round-robin through a shuffled copy per process.
- ``op: verify`` first solves every feasible mix entry once to get plans.
- ``mode: open`` issues Poisson arrivals at ``rps`` and measures latency from
  the scheduled send time (no coordinated omission); in-flight is capped by
  ``maxInFlight`` (default 4096) and overflow counts as ``client_overflow``.
- Only requests *started* after warmup and *finished* before the end are
  counted. Error kinds: http_<code>, timeout, conn_reset, conn_refused,
  server_disconnected, client_overflow, other:<ExceptionName>.
- Load is spread over ``processes`` OS processes so the client's own event
  loop is not the bottleneck; each process reports its CPU seconds so a
  saturated client is visible (``clientCpuPerProcess`` near 1.0 = suspect).
"""

import asyncio
import json
import multiprocessing as mp
import os
import random
import resource
import time
import uuid

import aiohttp
from aiohttp import web

HERE = os.path.dirname(os.path.abspath(__file__))
MIX = json.load(open(os.path.join(HERE, "mix.json")))
LOADGEN_KEY = os.environ.get("LOADGEN_KEY", "")
REQUEST_TIMEOUT_S = float(os.environ.get("LOADGEN_REQUEST_TIMEOUT_S", "60"))


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def container_resources():
    return {
        "nproc": os.cpu_count(),
        "schedAffinity": len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
        "cgroupCpuMax": _read("/sys/fs/cgroup/cpu.max"),
        "cgroupMemoryMax": _read("/sys/fs/cgroup/memory.max"),
        "memTotalKb": next((l.split()[1] for l in (_read("/proc/meminfo") or "").splitlines() if l.startswith("MemTotal")), None),
    }


def classify_error(exc):
    if isinstance(exc, asyncio.TimeoutError):
        return "timeout"
    if isinstance(exc, aiohttp.ServerDisconnectedError):
        return "server_disconnected"
    if isinstance(exc, aiohttp.ClientConnectorError):
        text = str(exc).lower()
        if "refused" in text:
            return "conn_refused"
        return "conn_error"
    if isinstance(exc, (ConnectionResetError, aiohttp.ClientOSError)):
        return "conn_reset"
    return "other:" + type(exc).__name__


def build_bodies(scenario, plans):
    entries = MIX[scenario.get("mix", "representative")]
    bodies = []
    for e in entries:
        if scenario["op"] == "solve":
            body = {"queryId": e["queryId"], "spec": e["spec"],
                    "maxCandidates": scenario.get("maxCandidates") or e.get("maxCandidates", 3)}
            if scenario.get("timeLimitMs"):
                body["timeLimitMs"] = scenario["timeLimitMs"]
            bodies.append(body)
        else:
            plan = plans.get(e["queryId"])
            if plan is not None:
                bodies.append({"queryId": e["queryId"], "spec": e["spec"], "plan": plan})
    if not bodies:
        raise RuntimeError("scenario {} has no request bodies".format(scenario.get("name")))
    return bodies


async def _one(session, url, headers, body, rec, t_sched, window):
    t_start = time.monotonic()
    t0 = t_sched if t_sched is not None else t_start
    kind, solve_ms, status = None, None, None
    try:
        async with session.post(url, data=body, headers=headers) as res:
            raw = await res.read()
            if res.status != 200:
                kind = "http_{}".format(res.status)
            else:
                payload = json.loads(raw)
                status = payload.get("status") or ("pass" if payload.get("pass") else "fail")
                solve_ms = (payload.get("solverMeta") or {}).get("solveMs")
    except Exception as exc:  # noqa: BLE001 - every failure is a data point
        kind = classify_error(exc)
    t_end = time.monotonic()
    if window[0] <= t0 and t_end <= window[1]:
        bucket = rec["timeline"].setdefault(str(int(t0 - window[0])), [[], 0])
        if kind is None:
            bucket[0].append(round((t_end - t0) * 1000, 1))
        else:
            bucket[1] += 1
        rec["lat"].append(round((t_end - t0) * 1000, 2))
        rec["okLat" if kind is None else "errLat"].append(round((t_end - t0) * 1000, 2))
        if kind:
            rec["errors"][kind] = rec["errors"].get(kind, 0) + 1
        if status:
            rec["statuses"][status] = rec["statuses"].get(status, 0) + 1
        if solve_ms is not None:
            rec["solveMs"].append(solve_ms)
        if t_sched is not None:
            rec["sendLagMs"].append(round((t_start - t_sched) * 1000, 2))


async def _run_process(cfg):
    scenario, bodies, target, api_key, proc_idx, share, t_begin = cfg
    url = target.rstrip("/") + "/v1/packs/travelplanner/" + scenario["op"]
    headers = {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}
    encoded = [json.dumps(b).encode() for b in bodies]
    rnd = random.Random(proc_idx * 7919 + 1)
    rnd.shuffle(encoded)
    warm = float(scenario.get("warmupS", 5))
    dur = float(scenario.get("durationS", 30))
    # wall-clock start shared across processes; monotonic offsets inside
    now_wall = time.time()
    await asyncio.sleep(max(0.0, t_begin - now_wall))
    t0 = time.monotonic()
    window = (t0 + warm, t0 + warm + dur)
    stop_at = window[1]
    rec = {"lat": [], "okLat": [], "errLat": [], "errors": {}, "statuses": {}, "solveMs": [], "sendLagMs": [],
           "overflow": 0, "timeline": {}}
    cpu0 = time.process_time()
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S)
    mode = scenario.get("mode", "closed")
    limit = share if mode == "closed" else int(scenario.get("maxInFlight", 4096)) // max(1, scenario.get("processes", 1))
    connector = aiohttp.TCPConnector(limit=limit, limit_per_host=limit, force_close=False, ttl_dns_cache=300)
    counter = {"i": proc_idx}
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        if mode == "closed":
            async def loop():
                while time.monotonic() < stop_at:
                    body = encoded[counter["i"] % len(encoded)]
                    counter["i"] += 1
                    await _one(session, url, headers, body, rec, None, window)
            await asyncio.gather(*(loop() for _ in range(share)))
        else:
            rate = share  # per-process rps
            inflight = set()
            t_next = time.monotonic()
            while t_next < stop_at:
                delay = t_next - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                if len(inflight) >= limit:
                    if window[0] <= t_next <= window[1]:
                        rec["overflow"] += 1
                else:
                    body = encoded[counter["i"] % len(encoded)]
                    counter["i"] += 1
                    task = asyncio.ensure_future(_one(session, url, headers, body, rec, t_next, window))
                    inflight.add(task)
                    task.add_done_callback(inflight.discard)
                t_next += rnd.expovariate(rate)
            if inflight:
                await asyncio.wait(inflight, timeout=REQUEST_TIMEOUT_S + 5)
    rec["cpuS"] = time.process_time() - cpu0
    rec["wallS"] = time.monotonic() - t0
    rec["window"] = dur
    return rec


def _process_entry(cfg):
    return asyncio.run(_run_process(cfg))


def _pct(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return round(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo), 1)


def _dist(vals):
    s = sorted(vals)
    return {"n": len(s), "p50": _pct(s, 0.5), "p90": _pct(s, 0.9), "p95": _pct(s, 0.95),
            "p99": _pct(s, 0.99), "max": s[-1] if s else None,
            "mean": round(sum(s) / len(s), 1) if s else None}


def summarize(scenario, recs):
    merged = {"lat": [], "okLat": [], "solveMs": [], "sendLagMs": [], "errors": {}, "statuses": {}, "overflow": 0}
    for r in recs:
        for k in ("lat", "okLat", "solveMs", "sendLagMs"):
            merged[k].extend(r[k])
        for k in ("errors", "statuses"):
            for kk, v in r[k].items():
                merged[k][kk] = merged[k].get(kk, 0) + v
        merged["overflow"] += r["overflow"]
    if merged["overflow"]:
        merged["errors"]["client_overflow"] = merged["overflow"]
    # per-second timeline by request start (for cold start / head-of-line views)
    buckets = {}
    for r in recs:
        for sec, (lats, errs_) in r["timeline"].items():
            b = buckets.setdefault(int(sec), [[], 0])
            b[0].extend(lats)
            b[1] += errs_
    timeline = []
    for sec in sorted(buckets):
        lats = sorted(buckets[sec][0])
        timeline.append({"s": sec, "ok": len(lats), "err": buckets[sec][1], "p50": _pct(lats, 0.5), "p95": _pct(lats, 0.95)})
    dur = float(scenario.get("durationS", 30))
    total = len(merged["lat"]) + merged["overflow"]
    errs = sum(merged["errors"].values())
    return {
        "scenario": scenario,
        "requests": total,
        "ok": len(merged["okLat"]),
        "errorRate": round(errs / total, 4) if total else None,
        "errors": merged["errors"],
        "throughputRps": round(len(merged["okLat"]) / dur, 2),
        "offeredRps": scenario.get("rps"),
        "latencyMs": _dist(merged["okLat"]),
        "solveMs": _dist(merged["solveMs"]),
        "sendLagMs": _dist(merged["sendLagMs"]) if merged["sendLagMs"] else None,
        "statuses": merged["statuses"],
        "clientCpuPerProcess": [round(r["cpuS"] / r["wallS"], 3) for r in recs],
        "timeline": timeline,
    }


async def fetch_plans(target, api_key, mix_name):
    plans = {}
    url = target.rstrip("/") + "/v1/packs/travelplanner/solve"
    headers = {"Authorization": "Bearer " + api_key}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as s:
        for e in MIX[mix_name]:
            async with s.post(url, json={"queryId": e["queryId"], "spec": e["spec"], "maxCandidates": 1}, headers=headers) as res:
                payload = await res.json()
                if payload.get("status") in ("optimal", "feasible_timeout"):
                    plans[e["queryId"]] = payload["candidates"][0]["plan"]
    return plans


RUNS = {}
POOL = None


async def execute(run_id, plan):
    try:
        target = plan["target"]
        api_key = plan.get("apiKey") or os.environ.get("TARGET_API_KEY", "")
        loop = asyncio.get_running_loop()
        t_begin = time.time() + 2.0
        futures = []
        plans_cache = {}
        for sc in plan["scenarios"]:
            plans = {}
            if sc["op"] == "verify":
                mix_name = sc.get("mix", "representative")
                if mix_name not in plans_cache:
                    plans_cache[mix_name] = await fetch_plans(target, api_key, mix_name)
                plans = plans_cache[mix_name]
        t_begin = time.time() + 2.0
        for sc in plan["scenarios"]:
            plans = plans_cache.get(sc.get("mix", "representative"), {})
            bodies = build_bodies(sc, plans)
            procs = max(1, int(sc.get("processes", 1)))
            total = int(sc["concurrency"]) if sc.get("mode", "closed") == "closed" else float(sc["rps"])
            cfgs = []
            for i in range(procs):
                share = total // procs + (1 if i < total % procs else 0) if isinstance(total, int) else total / procs
                if share:
                    cfgs.append((sc, bodies, target, api_key, i, share, t_begin))
            futures.append((sc, [loop.run_in_executor(POOL, _process_entry, c) for c in cfgs]))
        results = []
        for sc, futs in futures:
            recs = await asyncio.gather(*futs)
            results.append(summarize(sc, recs))
        RUNS[run_id] = {"state": "done", "result": {"startedAt": t_begin, "scenarios": results}}
    except Exception as exc:  # noqa: BLE001
        RUNS[run_id] = {"state": "failed", "error": repr(exc)}


def _authorized(request):
    return LOADGEN_KEY and request.headers.get("Authorization") == "Bearer " + LOADGEN_KEY


async def health(_request):
    return web.json_response({"status": "ok", "resources": container_resources(), "mixes": {k: len(v) for k, v in MIX.items()}})


async def post_run(request):
    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    plan = await request.json()
    run_id = uuid.uuid4().hex[:12]
    RUNS[run_id] = {"state": "running"}
    asyncio.ensure_future(execute(run_id, plan))
    return web.json_response({"id": run_id})


async def get_run(request):
    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    run = RUNS.get(request.match_info["id"])
    if run is None:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response(run)


def main():
    global POOL
    from concurrent.futures import ProcessPoolExecutor

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
    POOL = ProcessPoolExecutor(max_workers=int(os.environ.get("LOADGEN_MAX_PROCESSES", "32")), mp_context=mp.get_context("spawn"))
    app = web.Application(client_max_size=8 << 20)
    app.add_routes([web.get("/health", health), web.post("/runs", post_run), web.get("/runs/{id}", get_run)])
    # Bind both families explicitly: asyncio sets IPV6_V6ONLY on "::", so an
    # IPv6-only bind fails Railway's IPv4 healthcheck, while private
    # networking (<svc>.railway.internal) may resolve to IPv6.
    web.run_app(app, host=["0.0.0.0", "::"], port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
