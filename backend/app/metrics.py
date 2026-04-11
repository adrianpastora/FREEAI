"""Prometheus metrics. Exposed at /metrics."""
from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

# HTTP-level
http_requests_total = Counter(
    "freeai_http_requests_total",
    "HTTP requests received by FreeAI",
    ["method", "path", "status"],
)
http_request_duration_seconds = Histogram(
    "freeai_http_request_duration_seconds",
    "HTTP request latency",
    ["method", "path"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
)

# Provider-level
provider_calls_total = Counter(
    "freeai_provider_calls_total",
    "Calls dispatched to a provider",
    ["provider", "outcome"],  # outcome: success | server_error | rate_limited | auth | network | client_error | parsing | unknown
)
provider_call_duration_seconds = Histogram(
    "freeai_provider_call_duration_seconds",
    "Provider call latency",
    ["provider"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)

# Orchestrator-level
orchestrator_fallbacks_total = Counter(
    "freeai_orchestrator_fallbacks_total",
    "Number of times the orchestrator had to fall back to a different provider",
    ["from_provider", "to_provider"],
)


purge_rows_total = Counter(
    "freeai_purged_rows_total",
    "Rows purged by the periodic cleanup job",
    ["table"],
)


def render_latest() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
