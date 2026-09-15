"""Service configuration, sourced entirely from environment variables.

- ``SOLVER_SERVICE_API_KEY``: bearer token required on every endpoint except
  ``GET /health``. The service refuses to start without it unless the explicit
  dev flag is set.
- ``SOLVER_DEV_ALLOW_NO_AUTH``: set to ``1`` to run without auth (local dev
  only; ignored as soon as an API key is present).
- ``SOLVER_MAX_REQUEST_BYTES``: request body size limit (default 1 MiB; 413).
- ``SOLVER_MAX_TIME_LIMIT_MS``: server-side cap for the per-request solver
  time limit (default 30000).
- ``SOLVER_MAX_CANDIDATES``: cap for the per-request candidate count
  (default 10).
- ``SOLVER_TP_DATA_DIR``: TravelPlanner pack data directory (default
  ``<repo>/data``).
- ``SOLVER_TP_SPLITS``: comma-separated splits to preload per worker
  (default ``train,validation,test``). Loading fewer splits cuts worker
  memory roughly proportionally to the on-disk file sizes.
"""

import os

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class Settings(object):
    def __init__(
        self,
        api_key=None,
        dev_allow_no_auth=False,
        max_request_bytes=1 << 20,
        max_time_limit_ms=30000,
        max_candidates=10,
        tp_data_dir=os.path.join(_REPO_ROOT, "data"),
        tp_splits=("train", "validation", "test"),
    ):
        self.api_key = api_key
        self.dev_allow_no_auth = dev_allow_no_auth
        self.max_request_bytes = max_request_bytes
        self.max_time_limit_ms = max_time_limit_ms
        self.max_candidates = max_candidates
        self.tp_data_dir = tp_data_dir
        self.tp_splits = tp_splits

    @property
    def auth_enabled(self):
        return bool(self.api_key)

    @classmethod
    def from_env(cls):
        splits = tuple(
            s.strip()
            for s in os.environ.get("SOLVER_TP_SPLITS", "train,validation,test").split(",")
            if s.strip()
        )
        return cls(
            api_key=os.environ.get("SOLVER_SERVICE_API_KEY") or None,
            dev_allow_no_auth=os.environ.get("SOLVER_DEV_ALLOW_NO_AUTH") == "1",
            max_request_bytes=int(os.environ.get("SOLVER_MAX_REQUEST_BYTES", str(1 << 20))),
            max_time_limit_ms=int(os.environ.get("SOLVER_MAX_TIME_LIMIT_MS", "30000")),
            max_candidates=int(os.environ.get("SOLVER_MAX_CANDIDATES", "10")),
            tp_data_dir=os.environ.get("SOLVER_TP_DATA_DIR") or os.path.join(_REPO_ROOT, "data"),
            tp_splits=splits,
        )
