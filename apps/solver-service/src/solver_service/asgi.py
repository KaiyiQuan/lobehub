"""ASGI entrypoint: ``uvicorn solver_service.asgi:app``.

Each uvicorn worker imports this module, so every worker process builds its own
app instance with ortools and the pack reference data preloaded before it
starts accepting connections.
"""

from .app import create_app

app = create_app()
