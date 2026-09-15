"""Domain-pack registry.

A *pack* is a self-contained domain module (solver + verifier + reference data
loader) exposing ``solve`` and ``verify`` over validated spec dicts. Registering
a second domain means adding a new module under ``solver_service.packs`` and
one ``registry.register(...)`` line in ``app.create_app`` — no route or
middleware changes.

Extension point — generic declarative endpoint (NOT implemented):
the parallel formulation experiment (typed spec vs SMT-LIB2/MiniZinc) decides
whether the service also exposes a domain-agnostic declarative endpoint
(e.g. ``POST /v1/solve`` executing a declarative model without running
arbitrary code). When it ships, it plugs in here as a registry sibling
(``registry.register_generic(...)`` + one route module), reusing the same
auth, size-limit, time-limit and logging middleware. Do not add it before the
experiment concludes; executing model-written code in this service is
explicitly out of scope.
"""


class PackNotFoundError(KeyError):
    pass


class PackRegistry(object):
    def __init__(self):
        self._packs = {}

    def register(self, pack):
        if pack.id in self._packs:
            raise ValueError("duplicate pack id {!r}".format(pack.id))
        self._packs[pack.id] = pack

    def get(self, pack_id):
        try:
            return self._packs[pack_id]
        except KeyError:
            raise PackNotFoundError(pack_id)

    def list(self):
        return [pack.info() for pack in self._packs.values()]
