# solver-service

Stateless Python HTTP service that turns validated constraint specs into solved,
verified domain plans. The first domain pack is **TravelPlanner** (CP-SAT solver
+ independent verifier, ported from the `lobehub-wt-travelplanner-solver`
experiment, where it scored 179/180 oracle agreement with the official
evaluator). A warm request costs one CP-SAT solve (median ~160 ms engine time);
the ~1.3 s per-call overhead of the old CLI (interpreter start + `ortools`
import + data load) is paid once per worker at startup.

Pure Python — no `package.json`; the pnpm workspace is unaffected.

## Layout

```
src/solver_service/
  app.py            FastAPI app factory, routes, auth/size/time-limit wiring
  asgi.py           uvicorn entrypoint (one app instance per worker process)
  config.py         env-driven Settings
  auth.py           bearer-token dependency (hmac.compare_digest)
  middleware.py     request-id + JSON access log, body size limit (pure ASGI)
  registry.py       pack registry + generic-endpoint extension point
  packs/travelplanner/
    loader.py       ref-info parsing + RefDataStore (per-worker preload)
    spec.py         ConstraintSpec jsonschema validation
    spec.schema.json
    solve.py        CP-SAT solver (assumption-literal conflict extraction)
    verify.py       independent verifier (13 official constraints, 2nd code path)
scripts/fetch_data.py   sha256-pinned reference-data download
data/background/citySet_with_states.txt   committed (see "Reference data")
tests/            pack tests (ported) + API tests + multi-worker concurrency test
```

## API

All endpoints except `GET /health` require `Authorization: Bearer
$SOLVER_SERVICE_API_KEY` (401 when missing/wrong).

| Endpoint | Body | Response |
| --- | --- | --- |
| `GET /health` | — | `{"status":"ok","version":...,"packs":[{id,version,kind,splits,queries}]}` |
| `GET /v1/packs` | — | `{"packs":[...]}` |
| `POST /v1/packs/{pack}/solve` | `{"queryId","spec","maxCandidates"?,"timeLimitMs"?}` | `{"status":"optimal"\|"feasible_timeout"\|"infeasible"\|"error", ...}` |
| `POST /v1/packs/{pack}/verify` | `{"queryId","spec","plan"}` | `{"pass":bool,"results":[{constraint,pass,detail}]}` (13 checks) |

- `queryId` is `{split}_{idx0}`, e.g. `validation_0` (splits: `train`,
  `validation`, `test`). Unknown/Out-of-range ids → 400; unknown pack → 404.
- `spec` is a TravelPlanner ConstraintSpec (`packs/travelplanner/spec.schema.json`).
  Invalid specs return 200 with `{"status":"error","error":"invalid spec: ..."}`
  listing every violation — that is the model-facing repair signal.
- `timeLimitMs` is capped by `SOLVER_MAX_TIME_LIMIT_MS` (the effective value is
  echoed in `solverMeta.timeLimitMs`) and bounds the whole call. A solve cut
  off at the limit returns its best feasible plan with status
  `feasible_timeout` (vs `optimal` when optimality is proved).
- Infeasibility is structured: `conflicts[].constraint` (e.g. `budget`,
  `cuisine:Italian`, `structural:accommodationDomain`), a human-readable
  `explanation` (for budget it names the minimum feasible plan cost),
  `involvedFields` mapped back to spec field paths, and
  `suggestedRelaxations` for the repair loop. Extraction uses CP-SAT assumption
  literals (`sufficient_assumptions_for_infeasibility`) plus deterministic
  pre-solve structural checks.
- Request bodies over `SOLVER_MAX_REQUEST_BYTES` (default 1 MiB) → 413.
- Logs are JSON lines (request id, pack, op, status, solve_ms, duration_ms);
  request bodies and secrets are never logged. No persistent state anywhere.

## Production shape

- **Auth**: bearer token, constant-time compare. The service refuses to start
  without `SOLVER_SERVICE_API_KEY` unless `SOLVER_DEV_ALLOW_NO_AUTH=1` (local
  dev only).
- **Concurrency**: uvicorn runs N worker processes (`SOLVER_WORKERS`, default
  4 in the Dockerfile); each worker imports the app (and preloads ortools +
  reference data) before accepting connections, and solves run via
  `run_in_threadpool` so the event loop stays free. One CP-SAT solve is
  single-threaded (`num_workers=1`, fixed seed — deterministic), so throughput
  scales with worker processes, and `/health` stays responsive under load.
  Verified by `tests/test_concurrency.py`: 8 parallel solves against 4 workers
  finish in well under half the serialized estimate.
- **Reference data**: fetched at image build time by `scripts/fetch_data.py`
  from Hugging Face `osunlp/TravelPlanner`, revision
  `8736504ecfc31b7f8b7e40122873c337e83fff7c`, every file SHA-256 verified
  (pins in the script). Chosen over committing 33 MB of JSONL to git: the
  pinned revision + hashes give the same reproducibility without bloating the
  monorepo and the PR diff; the risk accepted is HF availability at build time
  (a changed/corrupt file fails the build loudly). Only
  `data/background/citySet_with_states.txt` (~8 KB, 312 city→state entries,
  sha256 `a3d18b5c…a4d6`) is committed, because its sole upstream is a Google
  Drive zip that is not reliably scriptable in build environments. Size impact:
  +33 MB in the image (train 1.1 MB, validation 4.8 MB, test 27 MB), zero in
  git. `SOLVER_TP_SPLITS` limits which splits are loaded per worker (e.g.
  `validation` only) — loaded raw lines cost ~their on-disk size in RAM each.

## Configuration

| Env var | Default | Meaning |
| --- | --- | --- |
| `SOLVER_SERVICE_API_KEY` | — (required) | bearer token for all non-health endpoints |
| `SOLVER_DEV_ALLOW_NO_AUTH` | off | `1` = run without auth (local dev only) |
| `SOLVER_MAX_REQUEST_BYTES` | `1048576` | body size limit (413) |
| `SOLVER_MAX_TIME_LIMIT_MS` | `30000` | server cap for per-request `timeLimitMs` |
| `SOLVER_MAX_CANDIDATES` | `10` | server cap for per-request `maxCandidates` |
| `SOLVER_TP_DATA_DIR` | `<repo>/data` | TravelPlanner pack data directory |
| `SOLVER_TP_SPLITS` | `train,validation,test` | splits preloaded per worker |
| `SOLVER_WORKERS` | `4` (Dockerfile) | uvicorn worker processes |
| `PORT` | `8000` | listen port (Railway injects this) |

## Local development

```bash
cd apps/solver-service
python3 -m venv .venv
./.venv/bin/pip install -r requirements-dev.txt
python3 scripts/fetch_data.py          # sha256-verified download into data/

# run tests (55 tests: ported pack tests, API tests, multi-worker concurrency)
./.venv/bin/python -m pytest

# run the service (dev mode, no auth)
SOLVER_DEV_ALLOW_NO_AUTH=1 PYTHONPATH=src ./.venv/bin/python -m solver_service
```

## Docker

```bash
cd apps/solver-service
docker build -t lobehub-solver-service .
docker run --rm -p 8000:8000 -e SOLVER_SERVICE_API_KEY=local-dev-key lobehub-solver-service

curl localhost:8000/health
curl -X POST localhost:8000/v1/packs/travelplanner/solve \
  -H "Authorization: Bearer local-dev-key" -H "content-type: application/json" \
  -d '{"queryId":"validation_0","spec":{...},"maxCandidates":1}'
```

## Railway

Live deployment (as of 2026-09-15): `https://solver-production-5e66.up.railway.app`
— Railway project `lobehub-solver-service`, service `solver`, region **us-west2**,
1 replica, usage-based resources (Hobby plan; elastic, well under the 8 vCPU /
8 GB cap). `SOLVER_WORKERS=4`, `SOLVER_TP_SPLITS=validation` (~5 MB reference
data per worker). Reference data is downloaded from the pinned Hugging Face
revision at image build time.

Deploy method: **CLI upload (`railway up`) from `apps/solver-service/`**, not a
GitHub source. The upload makes the service directory itself the build root, so
the `Dockerfile` works unchanged and the monorepo does not need a Railway
GitHub connection or a separate branch layout. Redeploy:

```bash
cd apps/solver-service
railway up -p <project-id> -s <service-id> -e production -d -m "why"
# then poll: deployment status goes BUILDING -> DEPLOYING -> SUCCESS
# (SUCCESS is gated on the /health healthcheck)
```

Variables (`railway variables` or the dashboard): `SOLVER_SERVICE_API_KEY`
(required, random secret), `SOLVER_WORKERS`, `SOLVER_TP_SPLITS`. **Rotating the
API key** = update the variable, which triggers an automatic redeploy; update
every client (e.g. LobeHub `SOLVER_SERVICE_API_KEY`) at the same time. Roll
back via the Railway dashboard deployments list.

**Scaling**: throughput ≈ `SOLVER_WORKERS` solves in parallel per replica.
Vertical: raise `SOLVER_WORKERS` toward the instance's vCPUs. Horizontal: raise
replica count behind the same URL (`numReplicas` / multi-region config). RAM ≈
150–250 MB baseline per worker (interpreter + ortools) + loaded splits (~33 MB
all three, ~5 MB validation-only) per worker. On small instances prefer
`SOLVER_WORKERS=2` and/or `SOLVER_TP_SPLITS=validation`.

### Load test (2026-09-15, against the deployed URL)

Script: `scripts/loadtest.py` (stdlib-only, fixed mix of 8 feasible validation
specs; raw results in `.records/solver-service-loadtest/`, not committed).
Client was a macOS machine in China, so ~670 ms of every latency figure is
network RTT to us-west2 (measured separately on `/health`: median 0.674 s);
server-side engine time is `solverMeta.solveMs` ≈ 10–110 ms per solve.

| op | concurrency | requests | throughput | p50 | p95 | error rate |
| --- | --- | --- | --- | --- | --- | --- |
| solve | 1 | 60 | 0.93 req/s | 992 ms | 1465 ms | 0% |
| solve | 8 | 96 | 6.47 req/s | 1156 ms | 1798 ms | 0% |
| solve | 32 | 160 | 17.69 req/s | 1586 ms | 2492 ms | 0% |
| verify | 1 | 60 | 1.04 req/s | 888 ms | 1240 ms | 0% |
| verify | 8 | 96 | 6.90 req/s | 1108 ms | 1482 ms | 0% |
| verify | 32 | 160 | 16.54 req/s | 1515 ms | 2528 ms | 0% |

Subtracting RTT, one replica with 4 workers sustains far more than 17 req/s of
solver work; the c=32 numbers are bounded by the client's network, not CPU.
Scale out when server-side queueing shows up as p95 >> RTT + solveMs, not
before.

**Cold start** (redeploy with cached image layers): deployment created →
SUCCESS (healthcheck-gated) in ~48 s, of which container start + 4-worker
data preload → first healthy `/health` ≈ 13 s. The first solve right after
SUCCESS answered `optimal` in 0.72 s end-to-end (solveMs 42). Rolling deploys
keep the old replica serving, so `/health` never went down from the client's
perspective. Implication for callers: a 30–60 s HTTP timeout covers both
steady-state latency and a cold first request.

### In-region capacity test harness (`loadtest/`)

The load test above ran from a far-away client and measured its network. To
measure the service itself, load is generated from inside Railway, in the same
region, against a **temporary copy** of the service (never the production
`solver` service):

| File                                          | Role                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| --------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `loadtest/build_mix.py`                       | Builds `loadtest/mix.json` from the TravelPlanner validation CSV: `representative` = all 180 gold specs (easy/medium/hard x 3/5/7 days, 1-3 cities, maxCandidates 3, one infeasible), `heavy` = the 8 slowest specs.                                                                                                                                                                                                                          |
| `loadtest/loadgen.py` + `loadtest/Dockerfile` | aiohttp control server deployed as a temporary `loadgen` service. Runs closed-loop (fixed concurrency) or open-loop (Poisson at a target rps, latency from the scheduled send time) scenarios across several processes, concurrently if more than one is given. Reports throughput, p50/p95/p99, error kinds (`http_5xx`, `timeout`, `conn_reset`, ...), server `solveMs`, response statuses, a per-second timeline and the client's own CPU. |
| `loadtest/run_plan.py`                        | Submits a run plan to the loadgen and saves the JSON result.                                                                                                                                                                                                                                                                                                                                                                                  |
| `loadtest/railway_ctl.py`                     | Creates, configures, deploys, scales, reads metrics/logs for and deletes the temporary services through the backboard GraphQL API. Refuses to touch any service other than `solver-loadtest` and `loadgen`.                                                                                                                                                                                                                                   |

Workflow (workspace token in `RAILWAY_API_TOKEN`, plus `RAILWAY_PROJECT_ID`
and `RAILWAY_ENVIRONMENT_ID`):

```bash
cd apps/solver-service
ctl() { python3 loadtest/railway_ctl.py --state /tmp/lt-state.json "$@"; }
ctl create --name solver-loadtest --port 8000 --healthcheck /health
ctl vars   --name solver-loadtest SOLVER_SERVICE_API_KEY=<new random key> SOLVER_WORKERS=4 \
           SOLVER_TP_SPLITS=validation PORT=8000 HOST=::
ctl deploy --name solver-loadtest --dir .
ctl create --name loadgen --port 8080 --healthcheck /health
ctl vars   --name loadgen LOADGEN_KEY=<random> TARGET_API_KEY=<same key as above> PORT=8080
ctl deploy --name loadgen --dir loadtest
LOADGEN_URL=https://<loadgen domain> LOADGEN_KEY=... python3 loadtest/run_plan.py \
  --target http://solver-loadtest.railway.internal:8000 \
  --scenario '{"name":"solve-c64","op":"solve","mode":"closed","concurrency":64,"durationS":40,"warmupS":5,"processes":2}' \
  --out /tmp/solve-c64.json
ctl metrics --name solver-loadtest --since <ISO start>   # CPU / memory per replica
ctl delete --name loadgen; ctl delete --name solver-loadtest; ctl list   # only `solver` must remain
```

`HOST=::` makes uvicorn listen on IPv6 as well, which private networking may
use. Changing variables or replicas takes effect on the next `ctl deploy`.

### Capacity results (2026-09-15, in-region, us-west2)

Temporary `solver-loadtest` service built from this directory, loaded by the
temporary `loadgen` service over private networking
(`solver-loadtest.railway.internal`). Both were deleted afterwards; production
was not touched. Mix: all 180 validation gold specs, `maxCandidates` 3,
default time limit. Closed loop, 40 s measured after a 5 s warmup. The client's
own CPU stayed below 0.35 core per process in every step. Raw JSON lives in
`.records/solver-service-capacity/` (scratch, not committed).

**Instance**: each replica gets a cgroup limit of 24 vCPU / 24 GB (`nproc`
reports the host's 48 cores; do not size workers from `nproc`). Railway bills
actual usage, not the limit.

**One replica, `SOLVER_WORKERS=4`, `SOLVER_TP_SPLITS=validation`**

| op | concurrency | throughput | p50 | p95 | p99 | errors | server solveMs p50 / p95 | replica CPU |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| solve | 1 | 8.8 rps | 100 ms | 238 ms | 311 ms | 0 | 89 / 229 | 1.0 vCPU |
| solve | 8 | 58.7 rps | 116 ms | 289 ms | 397 ms | 0 | 101 / 273 | 6.7 vCPU |
| solve | 16 | 110.6 rps | 123 ms | 307 ms | 433 ms | 0 | 106 / 291 | 14.7 vCPU |
| solve | 32 | 136.1 rps | 204 ms | 480 ms | 670 ms | 0 | 154 / 410 | ~19 vCPU |
| solve | 64 | 140.6 rps | 320 ms | 1122 ms | 1451 ms | 0 | 209 / 584 | — |
| solve | 128 | 144.9 rps | 808 ms | 1603 ms | 1934 ms | 0 | 281 / 667 | ~13–24 vCPU |
| solve | 256 | 138.6 rps | 1707 ms | 2959 ms | 3228 ms | 0 | 319 / 731 | ~17 vCPU |
| solve | 512 | 137.6 rps | 2311 ms | 6764 ms | 7234 ms | 0 | 303 / 702 | ~24 vCPU |
| verify | 8 | 156 rps | 51 ms | 58 ms | 62 ms | 0 | — | <1 vCPU |
| verify | 32 | 621 rps | 51 ms | 59 ms | 65 ms | 0 | — | ~1 vCPU |
| verify | 64 | 1036 rps | 58 ms | 86 ms | 110 ms | 0 | — | ~2 vCPU |
| verify | 128 | 1685 rps | — | 112 ms | — | 0 | — | ~3.5 vCPU |
| verify | 256 | 1686 rps | 141 ms | 302 ms | 347 ms | 0 | — | ~3.5 vCPU |
| verify | 512 | 1458 rps | 100 ms | 1219 ms | 1357 ms | 0 | — | ~3 vCPU |

CPU figures are Railway 30 s samples, so treat them as approximate.

- **solve**: the knee is at concurrency 16–32, about **110–135 rps with p95 ≤ 0.5 s**. Throughput then stays flat at ~140 rps while latency grows linearly with the queue: p95 1.1 s at c=64, 3 s at c=256, 6.8 s at c=512. No errors appeared up to c=512, so overload shows up as queueing rather than failures; callers need their own timeout (the lobe-solver client uses 60 s). Cost is about **0.13 vCPU-seconds per solve**, and at the plateau the replica is close to its 24 vCPU limit. Each uvicorn worker runs CP-SAT solves in parallel in its threadpool (CP-SAT releases the GIL), so 4 workers already use ~20 vCPU.
- **verify**: about **1000 rps at p95 < 90 ms** (c=64) and **~1700 rps at p95 ≤ 0.3 s** (c=128–256). Throughput drops and p95 exceeds 1 s at c=512. Verify is pure Python, so its ceiling is the GIL of the 4 workers (~3.5 vCPU used), not the replica's CPU.
- **Server-side `solveMs` grows under load** (p50 89 → ~300 ms): solves contend for CPU and the GIL inside the same worker.

**Worker tuning: more workers breaks the service.** With `SOLVER_WORKERS=24`
(= vCPU limit) the replica failed **94–99 % of solve requests from c=32 on**
(`http_500`, `server_disconnected`, `conn_reset`). The logs show
`RuntimeError: can't start new thread`. The container has `pids.max=1000`
(cgroup), and every worker can grow an anyio threadpool of up to 40 threads
under load, so 24 workers x 40 threads exceeds the thread ceiling. The
traceback flood also hit Railway's 500 logs/s cap, and log lines were dropped.
`SOLVER_WORKERS=32` was not run after that. Idle memory is 0.34 GB with 4
workers and 1.67 GB with 24, about **65 MB per worker** (validation split
only). Loading all three splits was not measured.

**Recommendation**: keep `SOLVER_WORKERS=4`. Up to ~16 would stay under
`pids.max` (16 x 41 threads plus the master), but that was not measured.
Scale solve throughput with replicas, not workers. If more per-worker
parallelism is ever needed, cap the threadpool explicitly (anyio
`total_tokens`) instead of raising the worker count.

**Two replicas, `SOLVER_WORKERS=4`** (same private hostname, Railway spreads
connections across replicas):

| op | concurrency | throughput | p50 | p95 | p99 | errors | vs 1 replica |
| --- | --- | --- | --- | --- | --- | --- | --- |
| solve | 128 | 213.6 rps | 340 ms | 2419 ms | 3508 ms | 0 | 1.47x (144.9 rps) |
| verify | 512 | 3610 rps | 96 ms | 440 ms | 593 ms | 0 | 2.5x (1458 rps; 2.1x the 1686 rps peak) |

Verify scales linearly. Solve scaled 1.47x, and p95 was worse than one replica
at the same concurrency, because the split was uneven: during the solve step
one replica used 20.5 vCPU and the other 10.8. Keep-alive connections are
pinned to a replica, so a few long-lived client connections can land unevenly.
Plan on roughly 1.5x per added replica for solve unless callers open fresh
connections or the balance is confirmed. 4 replicas were not measured.

**Head-of-line blocking** (2 replicas): 60 rps of open-loop background solves
alone gave p95 598 ms. With 8 concurrent heavy solves added next to it (the 8
slowest specs, `maxCandidates` 10, `timeLimitMs` 30000; 7.2 rps, solveMs p95
2.7 s, max ~3.7 s) the background p95 was 606 ms, and no errors appeared in
either run. Solves run in threadpools, not one at a time per worker, so a
heavy solve does not block the requests queued behind it while CPU headroom
remains. No real spec came near the 30 s limit: the heaviest took ~3.7 s even
at 10 candidates. Pathological specs that do run to the limit were not
measured. The remaining risk is CPU exhaustion when many heavy solves arrive
together, which shows up as the queueing in the table above. Cap
`SOLVER_MAX_TIME_LIMIT_MS` (for example 10 s) and `SOLVER_MAX_CANDIDATES`
if that becomes a concern.

**Cold start**: a `railway up` redeploy took 240–281 s from upload to SUCCESS,
almost all of it build queue and image build (BUILDING at ~110–140 s,
DEPLOYING at ~230–270 s). Container start to all 4 workers healthy took
1.2–3.6 s per replica, so a replica serves traffic a few seconds after its
container starts. Cold start under load (scale-up while traffic is flowing)
was not measured separately.

**Cost** (Railway usage pricing: $20 per vCPU-month, $10 per GB-month): a
sustained 100 rps of solve uses ~13 vCPU + ~1 GB, about **$270 / month**. A
sustained 1000 rps of verify uses ~2–3 vCPU + ~1.7 GB, about **$60–80 / month**.
An idle 4-worker replica costs a few dollars a month.

## Extension point: generic declarative endpoint

Deliberately **not implemented**. The formulation experiment (typed spec vs
SMT-LIB2/MiniZinc) decides whether a domain-agnostic declarative endpoint is
added; see the marked extension point in `src/solver_service/registry.py`.
Executing model-written code inside this service is out of scope by design.

## Adding a second domain pack

Add `src/solver_service/packs/<domain>/` implementing the pack interface
(`id`, `info()`, `solve(query_id, spec, max_candidates, time_limit_ms)`,
`verify(query_id, spec, plan)`), then one `registry.register(...)` line in
`app.create_app`. Auth, limits, logging and routes are reused unchanged.

## Notes

- `z3-solver` is intentionally not a dependency: the single-engine decision
  (CP-SAT only) avoids solver/verifier semantic drift; see
  `packs/travelplanner/solve.py` docstring.
- The pack never reads the gold CSV fields of the benchmark; all query
  parameters arrive via the ConstraintSpec (invariant documented in the pack
  modules).
- The oracle conformance suite (official evaluator cross-check, 179/180) lives
  in the experiment checkout and is not ported; it requires the full
  TravelPlanner vendor repo + database, which this service deliberately does
  not ship.
