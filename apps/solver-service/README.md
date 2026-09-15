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
