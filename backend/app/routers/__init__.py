"""HTTP routers — one module per resource group.

Each module exports a ``router`` (``APIRouter``) that ``main.py`` mounts
via ``app.include_router(...)``. The split keeps endpoint groups readable
in isolation without touching the orchestration / provider layers.

This file intentionally has no eager re-exports — that would force every
caller of ``app.routers`` to load all 14 router modules and their transitive
imports. ``main.py`` imports the submodules it needs by name.
"""
