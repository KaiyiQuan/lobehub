"""TravelPlanner domain pack.

Wraps the CP-SAT solver (``solve.py``), the independent verifier
(``verify.py``) and the ref-info loader (``loader.py``) behind the pack
interface used by the service registry. Reference data is loaded once per
worker at startup (``loader.RefDataStore``).

INVARIANT: this pack never reads the gold CSV fields (org/dest/days/
people_number/budget/local_constraint/level). All query parameters arrive via
the ConstraintSpec argument; only ref-info JSONL rows and the committed
background city/state file are read.
"""

from . import loader
from . import solve as solve_module
from . import spec as spec_module
from . import verify as verify_module


class TravelPlannerPack(object):
    id = "travelplanner"
    version = "1.0.0"
    kind = "domain"

    def __init__(self, store):
        self._store = store

    @classmethod
    def from_settings(cls, settings):
        store = loader.RefDataStore(settings.tp_data_dir, splits=settings.tp_splits)
        return cls(store)

    def info(self):
        return {
            "id": self.id,
            "version": self.version,
            "kind": self.kind,
            "splits": self._store.splits,
            "queries": self._store.query_count(),
        }

    def solve(self, query_id, spec, max_candidates=3, time_limit_ms=None):
        ref = self._store.get(query_id)  # raises loader.RefDataError -> 400 in the route
        return solve_module.solve(ref, spec, max_candidates=max_candidates, time_limit_ms=time_limit_ms)

    def verify(self, query_id, spec, plan):
        spec_errors = spec_module.validate_spec(spec)
        if spec_errors:
            return {"status": "error", "error": "invalid spec: " + "; ".join(spec_errors)}
        ref = self._store.get(query_id)  # raises loader.RefDataError -> 400 in the route
        return verify_module.verify_plan(plan, spec, ref)
