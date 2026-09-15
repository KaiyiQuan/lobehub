#!/usr/bin/env python3
"""Manage the TEMPORARY Railway services used for capacity testing.

Stdlib-only wrapper over the backboard GraphQL API (workspace tokens cannot use
`railway init` / `whoami`). It refuses to touch any service whose name is not
in ALLOWED_NAMES, so the production `solver` service cannot be modified or
deleted by accident.

Env: RAILWAY_API_TOKEN, RAILWAY_PROJECT_ID, RAILWAY_ENVIRONMENT_ID.
State (service ids, domains) is kept in --state (JSON scratch file).

  railway_ctl.py create  --name solver-loadtest --port 8000 [--healthcheck /health] [--start-command ...]
  railway_ctl.py vars    --name solver-loadtest KEY=VALUE ...     # skipDeploys; takes effect on next deploy
  railway_ctl.py scale   --name solver-loadtest --replicas 2      # takes effect on next deploy
  railway_ctl.py deploy  --name solver-loadtest --dir <build dir> # `railway up`, waits for SUCCESS
  railway_ctl.py status  --name solver-loadtest
  railway_ctl.py metrics --name solver-loadtest --since 2026-09-16T10:00:00Z [--until ...]
  railway_ctl.py logs    --name solver-loadtest [--filter RES] [--limit 200]
  railway_ctl.py delete  --name solver-loadtest
  railway_ctl.py list                                               # all services in the project
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

API = "https://backboard.railway.com/graphql/v2"
ALLOWED_NAMES = {"solver-loadtest", "loadgen"}


def gql(query, variables=None):
    req = urllib.request.Request(
        API,
        data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={"Authorization": "Bearer " + os.environ["RAILWAY_API_TOKEN"], "Content-Type": "application/json",
                 "User-Agent": "solver-loadtest-ctl"},
    )
    with urllib.request.urlopen(req, timeout=60) as res:
        payload = json.load(res)
    if payload.get("errors"):
        raise RuntimeError(json.dumps(payload["errors"]))
    return payload["data"]


def load_state(path):
    return json.load(open(path)) if os.path.exists(path) else {}


def save_state(path, state):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    json.dump(state, open(path, "w"), indent=1)


def guard(name):
    if name not in ALLOWED_NAMES:
        sys.exit("refusing to manage service {!r}: not a temporary load-test service".format(name))


def project_services():
    data = gql("query($id:String!){ project(id:$id){ services{ edges{ node{ id name } } } } }",
               {"id": os.environ["RAILWAY_PROJECT_ID"]})
    return {e["node"]["name"]: e["node"]["id"] for e in data["project"]["services"]["edges"]}


def service_id(state, name):
    guard(name)
    sid = project_services().get(name)
    if not sid:
        sys.exit("service {!r} not found".format(name))
    return sid


def latest_deployments(sid, first=3):
    data = gql(
        "query($s:String!,$e:String!,$n:Int){ deployments(first:$n, input:{serviceId:$s, environmentId:$e})"
        "{ edges{ node{ id status createdAt updatedAt } } } }",
        {"s": sid, "e": os.environ["RAILWAY_ENVIRONMENT_ID"], "n": first})
    return [e["node"] for e in data["deployments"]["edges"]]


def update_instance(sid, inst):
    gql("mutation($s:String!,$e:String!,$i:ServiceInstanceUpdateInput!){ serviceInstanceUpdate(serviceId:$s, environmentId:$e, input:$i) }",
        {"s": sid, "e": os.environ["RAILWAY_ENVIRONMENT_ID"], "i": inst})


def cmd_create(args, state):
    guard(args.name)
    env = os.environ["RAILWAY_ENVIRONMENT_ID"]
    sid = project_services().get(args.name)
    if not sid:
        sid = gql("mutation($i:ServiceCreateInput!){ serviceCreate(input:$i){ id } }",
                  {"i": {"projectId": os.environ["RAILWAY_PROJECT_ID"], "environmentId": env, "name": args.name}})["serviceCreate"]["id"]
    inst = {"multiRegionConfig": {args.region: {"numReplicas": 1}}, "restartPolicyType": "ON_FAILURE",
            "restartPolicyMaxRetries": 3}
    if args.healthcheck:
        inst.update({"healthcheckPath": args.healthcheck, "healthcheckTimeout": 300})
    if args.start_command:
        inst["startCommand"] = args.start_command
    update_instance(sid, inst)
    domain = gql("mutation($i:ServiceDomainCreateInput!){ serviceDomainCreate(input:$i){ domain } }",
                 {"i": {"serviceId": sid, "environmentId": env, "targetPort": args.port}})["serviceDomainCreate"]["domain"]
    state[args.name] = {"id": sid, "domain": domain, "privateHost": "{}.railway.internal".format(args.name), "port": args.port}
    print(json.dumps(state[args.name]))


def cmd_vars(args, state):
    sid = service_id(state, args.name)
    variables = dict(kv.split("=", 1) for kv in args.pairs)
    gql("mutation($i:VariableCollectionUpsertInput!){ variableCollectionUpsert(input:$i) }",
        {"i": {"projectId": os.environ["RAILWAY_PROJECT_ID"], "environmentId": os.environ["RAILWAY_ENVIRONMENT_ID"],
               "serviceId": sid, "variables": variables, "skipDeploys": True}})
    print("set", sorted(variables))


def cmd_scale(args, state):
    sid = service_id(state, args.name)
    update_instance(sid, {"multiRegionConfig": {args.region: {"numReplicas": args.replicas}}})
    print("replicas ->", args.replicas, "(takes effect on next deploy)")


def cmd_deploy(args, state):
    sid = service_id(state, args.name)
    before = {d["id"] for d in latest_deployments(sid, 5)}
    t0 = time.time()
    subprocess.run(["railway", "up", "-p", os.environ["RAILWAY_PROJECT_ID"], "-s", sid, "-e",
                    os.environ["RAILWAY_ENVIRONMENT_ID"], "-d", "-m", args.message], cwd=args.dir, check=True,
                   stdout=subprocess.DEVNULL)
    t_up = time.time()
    seen = {}
    while True:
        deps = [d for d in latest_deployments(sid, 5) if d["id"] not in before]
        dep = deps[0] if deps else None
        if dep:
            seen.setdefault(dep["status"], round(time.time() - t0, 1))
            if dep["status"] in ("SUCCESS", "FAILED", "CRASHED", "REMOVED"):
                break
        time.sleep(3)
    print(json.dumps({"deployment": dep, "uploadS": round(t_up - t0, 1), "firstSeenStatusAtS": seen,
                      "totalS": round(time.time() - t0, 1)}))
    if dep["status"] != "SUCCESS":
        sys.exit(1)


def cmd_status(args, state):
    sid = service_id(state, args.name)
    print(json.dumps({"state": state.get(args.name), "deployments": latest_deployments(sid)}, indent=1))


def cmd_metrics(args, state):
    sid = service_id(state, args.name)
    data = gql(
        "query($s:String!,$e:String!,$st:DateTime!,$en:DateTime,$r:Int){ metrics(serviceId:$s, environmentId:$e, "
        "startDate:$st, endDate:$en, sampleRateSeconds:$r, measurements:[CPU_USAGE, CPU_LIMIT, MEMORY_USAGE_GB, "
        "MEMORY_LIMIT_GB], groupBy:[DEPLOYMENT_INSTANCE_ID]){ measurement tags{ deploymentInstanceId } values{ ts value } } }",
        # the API rejects sample rates below 30 s ("Invalid input")
        {"s": sid, "e": os.environ["RAILWAY_ENVIRONMENT_ID"], "st": args.since, "en": args.until, "r": max(30, args.sample)})
    print(json.dumps(data["metrics"]))


def cmd_logs(args, state):
    sid = service_id(state, args.name)
    dep = latest_deployments(sid, 1)[0]
    data = gql("query($d:String!,$l:Int,$f:String){ deploymentLogs(deploymentId:$d, limit:$l, filter:$f){ timestamp message } }",
               {"d": dep["id"], "l": args.limit, "f": args.filter})
    for line in data["deploymentLogs"]:
        print(line["timestamp"], line["message"])


def cmd_delete(args, state):
    sid = service_id(state, args.name)
    gql("mutation($id:String!){ serviceDelete(id:$id) }", {"id": sid})
    state.pop(args.name, None)
    print("deleted", args.name)


def cmd_list(_args, _state):
    print(json.dumps(project_services(), indent=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=os.path.join(os.getcwd(), ".railway-loadtest-state.json"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("create"); p.add_argument("--name", required=True); p.add_argument("--region", default="us-west2")
    p.add_argument("--port", type=int, required=True); p.add_argument("--healthcheck"); p.add_argument("--start-command")
    p = sub.add_parser("vars"); p.add_argument("--name", required=True); p.add_argument("pairs", nargs="+")
    p = sub.add_parser("scale"); p.add_argument("--name", required=True); p.add_argument("--replicas", type=int, required=True)
    p.add_argument("--region", default="us-west2")
    p = sub.add_parser("deploy"); p.add_argument("--name", required=True); p.add_argument("--dir", required=True)
    p.add_argument("--message", default="capacity test")
    for n in ("status", "delete"):
        p = sub.add_parser(n); p.add_argument("--name", required=True)
    p = sub.add_parser("metrics"); p.add_argument("--name", required=True); p.add_argument("--since", required=True)
    p.add_argument("--until"); p.add_argument("--sample", type=int, default=10)
    p = sub.add_parser("logs"); p.add_argument("--name", required=True); p.add_argument("--filter")
    p.add_argument("--limit", type=int, default=200)
    sub.add_parser("list")
    args = ap.parse_args()
    state = load_state(args.state)
    globals()["cmd_" + args.cmd](args, state)
    save_state(args.state, state)


if __name__ == "__main__":
    main()
