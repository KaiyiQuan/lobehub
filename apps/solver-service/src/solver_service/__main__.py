"""Container entrypoint: ``python -m solver_service``.

Validates configuration in the master process BEFORE forking uvicorn workers,
so a missing SOLVER_SERVICE_API_KEY exits the container immediately with a
clear error instead of leaving a master process up with dead workers.
"""

import os
import sys

import uvicorn

from .config import Settings


def main():
    settings = Settings.from_env()
    if not settings.auth_enabled and not settings.dev_allow_no_auth:
        sys.stderr.write(
            "solver-service: SOLVER_SERVICE_API_KEY is not set; refusing to start. "
            "Set it, or set SOLVER_DEV_ALLOW_NO_AUTH=1 for local development only.\n"
        )
        sys.exit(1)
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    workers = int(os.environ.get("SOLVER_WORKERS", "4"))
    uvicorn.run("solver_service.asgi:app", host=host, port=port, workers=workers)


if __name__ == "__main__":
    main()
