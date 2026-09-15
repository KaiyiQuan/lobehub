#!/usr/bin/env python3
"""Submit a run plan to the in-region loadgen and save the result (stdlib only).

  LOADGEN_URL=https://loadgen-....up.railway.app LOADGEN_KEY=... \
    python loadtest/run_plan.py --plan plan.json --out results/step.json
  # or inline: --scenario '{"name":"solve-c64","op":"solve","concurrency":64}' (repeatable)

Target defaults to --target (e.g. http://solver-loadtest.railway.internal:8000);
the target API key is read by the loadgen from its TARGET_API_KEY variable, so
it never passes through this client. Prints one compact line per scenario.
"""

import argparse
import json
import os
import sys
import time
import urllib.request


def call(method, url, key, body=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as res:
        return json.load(res)


def line(s):
    sc = s["scenario"]
    lat = s["latencyMs"]
    return ("{name:<24} thr={thr:>7} rps  p50={p50} p95={p95} p99={p99} ms  err={err} {kinds}  "
            "solveMs p50={sp50} p95={sp95}  client_cpu={cpu}").format(
        name=sc.get("name", "?"), thr=s["throughputRps"], p50=lat["p50"], p95=lat["p95"], p99=lat["p99"],
        err=s["errorRate"], kinds=s["errors"] or "", sp50=s["solveMs"]["p50"], sp95=s["solveMs"]["p95"],
        cpu=max(s["clientCpuPerProcess"] or [0]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan")
    ap.add_argument("--scenario", action="append", default=[])
    ap.add_argument("--target", default="http://solver-loadtest.railway.internal:8000")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    base = os.environ["LOADGEN_URL"].rstrip("/")
    key = os.environ["LOADGEN_KEY"]
    plan = json.load(open(args.plan)) if args.plan else {"target": args.target, "scenarios": [json.loads(s) for s in args.scenario]}
    plan.setdefault("target", args.target)
    t0 = time.time()
    run_id = call("POST", base + "/runs", key, plan)["id"]
    while True:
        time.sleep(3)
        run = call("GET", base + "/runs/" + run_id, key)
        if run["state"] != "running":
            break
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    json.dump({"submittedAt": t0, "plan": plan, "run": run}, open(args.out, "w"), indent=1)
    if run["state"] != "done":
        sys.exit("run failed: " + str(run.get("error")))
    for s in run["result"]["scenarios"]:
        print(line(s))


if __name__ == "__main__":
    main()
