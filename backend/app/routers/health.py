"""Health + Prometheus metrics."""
from __future__ import annotations

from fastapi import APIRouter, Response

from ..metrics import render_latest

router = APIRouter(tags=["health"])


@router.get("/api/health")
async def health() -> dict:
    """Public healthcheck — minimal on purpose. Aggregate fleet counts are
    available to authenticated admins via /api/analytics and /api/providers.
    """
    return {"status": "ok"}


@router.get("/metrics")
async def metrics() -> Response:
    body, content_type = render_latest()
    return Response(content=body, media_type=content_type)
