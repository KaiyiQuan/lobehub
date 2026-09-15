"""FastAPI application factory for the solver service.

Endpoints:
- ``GET  /health``                    no auth; version + loaded packs
- ``GET  /v1/packs``                  pack registry listing
- ``POST /v1/packs/{pack_id}/solve``  domain solve
- ``POST /v1/packs/{pack_id}/verify`` domain verify (independent verifier)

CPU-bound pack work runs via ``run_in_threadpool`` so a solve never blocks the
event loop; real parallelism comes from multiple uvicorn worker processes
(``--workers`` / ``SOLVER_WORKERS``), each with ortools + reference data
preloaded at import time.
"""

import logging

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from typing import Optional

from . import __version__
from .auth import make_auth_dependency
from .config import Settings
from .json_logging import setup_logging
from .middleware import MaxBodySizeMiddleware, RequestContextMiddleware
from .packs.travelplanner import TravelPlannerPack
from .packs.travelplanner.loader import RefDataError
from .registry import PackNotFoundError, PackRegistry

logger = logging.getLogger("solver_service.solve")


class SolveRequest(BaseModel):
    queryId: str
    spec: dict
    maxCandidates: int = Field(default=3, ge=1)
    timeLimitMs: Optional[int] = Field(default=None, gt=0)


class VerifyRequest(BaseModel):
    queryId: str
    spec: dict
    plan: list


def create_app(settings=None):
    settings = settings or Settings.from_env()
    setup_logging()
    if not settings.auth_enabled and not settings.dev_allow_no_auth:
        raise RuntimeError(
            "SOLVER_SERVICE_API_KEY is not set; refusing to start. "
            "Set it, or set SOLVER_DEV_ALLOW_NO_AUTH=1 for local development only."
        )

    registry = PackRegistry()
    registry.register(TravelPlannerPack.from_settings(settings))

    app = FastAPI(title="lobehub-solver-service", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.registry = registry

    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(MaxBodySizeMiddleware, max_bytes=settings.max_request_bytes)

    require_auth = make_auth_dependency(settings)

    @app.get("/health")
    def health():
        return {"status": "ok", "version": __version__, "packs": registry.list()}

    @app.get("/v1/packs", dependencies=[Depends(require_auth)])
    def list_packs():
        return {"packs": registry.list()}

    def _get_pack(pack_id):
        try:
            return registry.get(pack_id)
        except PackNotFoundError:
            raise HTTPException(status_code=404, detail="unknown pack {!r}".format(pack_id))

    @app.post("/v1/packs/{pack_id}/solve", dependencies=[Depends(require_auth)])
    async def solve_pack(pack_id: str, body: SolveRequest, request: Request):
        pack = _get_pack(pack_id)
        time_limit_ms = settings.max_time_limit_ms
        if body.timeLimitMs is not None:
            time_limit_ms = min(body.timeLimitMs, settings.max_time_limit_ms)
        max_candidates = min(body.maxCandidates, settings.max_candidates)
        try:
            result = await run_in_threadpool(
                pack.solve, body.queryId, body.spec, max_candidates, time_limit_ms
            )
        except RefDataError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        logger.info(
            "solve",
            extra={
                "ctx": {
                    "request_id": request.scope["state"]["request_id"],
                    "pack": pack_id,
                    "op": "solve",
                    "status": result.get("status"),
                    "solve_ms": (result.get("solverMeta") or {}).get("solveMs"),
                }
            },
        )
        return result

    @app.post("/v1/packs/{pack_id}/verify", dependencies=[Depends(require_auth)])
    async def verify_pack(pack_id: str, body: VerifyRequest, request: Request):
        pack = _get_pack(pack_id)
        try:
            result = await run_in_threadpool(pack.verify, body.queryId, body.spec, body.plan)
        except RefDataError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        logger.info(
            "verify",
            extra={
                "ctx": {
                    "request_id": request.scope["state"]["request_id"],
                    "pack": pack_id,
                    "op": "verify",
                    "status": result.get("status") or ("pass" if result.get("pass") else "fail"),
                }
            },
        )
        return result

    return app
