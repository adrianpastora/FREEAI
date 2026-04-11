"""Smart orchestrator — Sprint 3 version.

What's new vs Sprint 2:
  • STRATEGY_TAGS is no longer hardcoded — strategies live in the `strategies`
    table, loaded per-request. Users can add custom ones from the UI.
  • Auto-strategy uses the new language-aware detector in app.auto_strategy.
  • Every dispatched completion (success OR failure) writes a row to
    usage_events via UsageRepository. That's what feeds the analytics panel
    and Grafana.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import AsyncIterator, Optional

import httpx

from .auto_strategy import AutoSignal, detect_auto_strategy
from .logging_config import get_logger
from .metrics import (
    orchestrator_fallbacks_total,
    provider_call_duration_seconds,
    provider_calls_total,
)
from .providers import (
    PROVIDER_REGISTRY,
    BaseProvider,
    ErrorKind,
    ProviderError,
    ProviderResponse,
    StreamChunk,
)
from .repositories import (
    ConfigRepository,
    ProviderConfigDTO,
    RateRepository,
    ReservationToken,
    StrategyRepository,
    UsageEvent,
    UsageRepository,
)
from .schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Choice,
    ChoiceMessage,
    Strategy,
    Usage,
)

log = get_logger("freeai.orchestrator")


@dataclass
class _Candidate:
    name: str
    provider: BaseProvider
    score: float
    config: ProviderConfigDTO


@dataclass
class _AttemptResult:
    response: Optional[ProviderResponse]
    error: Optional[ProviderError]
    latency_ms: int


class _StrategyCache:
    """In-process TTL cache for strategy lookups (change rarely, read every request)."""
    _TTL = 5.0

    def __init__(self):
        self._data: dict[str, tuple[float, Optional[list[str]]]] = {}

    async def get(self, name: str, repo: StrategyRepository) -> Optional[list[str]]:
        entry = self._data.get(name)
        now = time.monotonic()
        if entry and (now - entry[0]) < self._TTL:
            return entry[1]
        strat = await repo.get(name)
        tags = strat.tags if strat else None
        self._data[name] = (now, tags)
        return tags

    def invalidate(self, name: Optional[str] = None):
        if name:
            self._data.pop(name, None)
        else:
            self._data.clear()


class Orchestrator:
    """Stateless aside from the shared httpx client. Receives repositories per-call."""

    _RETRY_BACKOFF_S = 0.4
    _MAX_RETRIES = 1

    def __init__(self):
        self._client = httpx.AsyncClient(
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
        )
        self._strategy_cache = _StrategyCache()

    async def aclose(self) -> None:
        await self._client.aclose()

    def invalidate_strategy_cache(self, name: Optional[str] = None) -> None:
        self._strategy_cache.invalidate(name)

    # ──────────────── candidate selection ────────────────

    def _build_provider(self, dto: ProviderConfigDTO) -> Optional[BaseProvider]:
        cls = PROVIDER_REGISTRY.get(dto.name)
        if not cls:
            return None
        return cls(api_key=dto.api_key, default_model=dto.default_model)

    def _score(self, dto: ProviderConfigDTO, snap, wanted_tags: list[str]) -> float:
        if not snap.healthy:
            return -1.0
        score = 0.0
        for i, tag in enumerate(wanted_tags):
            if tag in dto.tags:
                score += 5.0 / (i + 1)
        score += dto.weight
        if dto.rpd_limit:
            remaining = max(0, dto.rpd_limit - snap.requests_today) / dto.rpd_limit
            score += remaining * 1.5
        else:
            score += 1.5
        last_ms = snap.last_latency_ms
        if last_ms is not None:
            if last_ms < 800:
                score += 0.8
            elif last_ms < 2000:
                score += 0.3
            else:
                score -= 0.3
        return score

    async def _resolve_strategy(
        self,
        req: ChatCompletionRequest,
        strategy_repo: StrategyRepository,
    ) -> tuple[Strategy, list[str], Optional[AutoSignal]]:
        """Returns (effective strategy name, its tag list, auto-detect signal if any).

        Raises ProviderError(CLIENT_ERROR) if the requested strategy doesn't
        exist. Uses an in-process TTL cache (5s) so strategies that rarely change
        don't require a DB lookup on every request.
        """
        if req.strategy == "auto":
            signal = detect_auto_strategy(req.messages)
            effective = signal.strategy
        else:
            signal = None
            effective = req.strategy
        tags = await self._strategy_cache.get(effective, strategy_repo)
        if tags is None:
            raise ProviderError(
                "orchestrator",
                f"unknown strategy '{effective}' — create it first via POST /api/strategies",
                kind=ErrorKind.CLIENT_ERROR,
            )
        return effective, tags, signal

    async def _rank(
        self,
        config_repo: ConfigRepository,
        rate_repo: RateRepository,
        wanted_tags: list[str],
        preferred: Optional[str],
    ) -> list[_Candidate]:
        providers = await config_repo.list_providers()
        eligible = [dto for dto in providers if dto.enabled and dto.api_key]
        if not eligible:
            return []
        snapshots = await rate_repo.snapshot_all([dto.name for dto in eligible])
        candidates: list[_Candidate] = []
        for dto in eligible:
            snap = snapshots.get(dto.name)
            if not snap or not snap.healthy:
                continue
            provider = self._build_provider(dto)
            if not provider:
                continue
            score = self._score(dto, snap, wanted_tags)
            if preferred and dto.name == preferred:
                score += 100
            candidates.append(_Candidate(name=dto.name, provider=provider, score=score, config=dto))
        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates

    # ──────────────── single-attempt helper ────────────────

    async def _attempt(self, provider: BaseProvider, req: ChatCompletionRequest) -> ProviderResponse:
        return await provider.complete(
            req.messages,
            model=req.model,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
            client=self._client,
        )

    async def _try_with_retry(self, cand: _Candidate, req: ChatCompletionRequest) -> _AttemptResult:
        started = time.perf_counter()
        last_error: Optional[ProviderError] = None
        for attempt in range(self._MAX_RETRIES + 1):
            try:
                resp = await self._attempt(cand.provider, req)
                latency_ms = int((time.perf_counter() - started) * 1000)
                return _AttemptResult(response=resp, error=None, latency_ms=latency_ms)
            except ProviderError as e:
                last_error = e
                if e.is_transient and attempt < self._MAX_RETRIES:
                    log.info("retrying transient error", provider=cand.name, kind=e.kind.value)
                    await asyncio.sleep(self._RETRY_BACKOFF_S * (2 ** attempt))
                    continue
                break
            except Exception as e:  # noqa: BLE001
                last_error = ProviderError(cand.name, str(e), kind=ErrorKind.UNKNOWN)
                break
        latency_ms = int((time.perf_counter() - started) * 1000)
        return _AttemptResult(response=None, error=last_error, latency_ms=latency_ms)

    async def _commit_attempt(
        self,
        rate_repo: RateRepository,
        usage_repo: UsageRepository,
        reservation: ReservationToken,
        result: _AttemptResult,
        strategy: str,
        fallback_position: int,
        client_hash: Optional[str],
    ) -> None:
        provider_call_duration_seconds.labels(provider=reservation.provider).observe(
            result.latency_ms / 1000.0
        )
        outcome: str
        if result.response is not None:
            outcome = "success"
            provider_calls_total.labels(provider=reservation.provider, outcome="success").inc()
            await rate_repo.commit(reservation, result.latency_ms, ok=True)
            await usage_repo.record(
                UsageEvent(
                    provider=reservation.provider,
                    model=result.response.model,
                    strategy=strategy,
                    outcome=outcome,
                    latency_ms=result.latency_ms,
                    prompt_tokens=result.response.prompt_tokens,
                    completion_tokens=result.response.completion_tokens,
                    fallback_position=fallback_position,
                    client_hash=client_hash,
                )
            )
            return

        err = result.error
        assert err is not None
        outcome = err.kind.value
        provider_calls_total.labels(provider=reservation.provider, outcome=outcome).inc()
        quarantine = err.retry_after if err.kind == ErrorKind.RATE_LIMITED else None
        if err.kind == ErrorKind.AUTH:
            quarantine = 24 * 3600
        await rate_repo.commit(
            reservation,
            result.latency_ms,
            ok=False,
            error=err.message,
            error_kind=err.kind.value,
            quarantine_seconds=quarantine,
        )
        await usage_repo.record(
            UsageEvent(
                provider=reservation.provider,
                model=None,
                strategy=strategy,
                outcome=outcome,
                latency_ms=result.latency_ms,
                fallback_position=fallback_position,
                client_hash=client_hash,
            )
        )

    # ──────────────── public: chat completion ────────────────

    async def chat(
        self,
        req: ChatCompletionRequest,
        config_repo: ConfigRepository,
        rate_repo: RateRepository,
        usage_repo: UsageRepository,
        strategy_repo: StrategyRepository,
        client_hash: Optional[str] = None,
    ) -> ChatCompletionResponse:
        app_cfg = await config_repo.get_app_config()
        strategy, wanted_tags, signal = await self._resolve_strategy(req, strategy_repo)
        if signal:
            log.info(
                "auto_strategy",
                language=signal.language,
                picked=signal.strategy,
                total_chars=signal.total_chars,
                has_code=signal.has_code,
                has_reasoning=signal.has_reasoning,
                has_vision=signal.has_vision,
            )

        candidates = await self._rank(config_repo, rate_repo, wanted_tags, req.preferred_provider)
        if not candidates:
            raise ProviderError(
                "orchestrator",
                "no provider configured/available — add an API key in settings",
                kind=ErrorKind.CLIENT_ERROR,
            )

        fallback_chain: list[str] = []
        last_error: Optional[ProviderError] = None
        use_fallback = req.fallback and app_cfg.enable_fallback
        attempts = candidates if use_fallback else candidates[:1]
        started_total = time.perf_counter()

        for cand in attempts:
            reservation = await rate_repo.try_reserve(
                cand.name, cand.config.rpm_limit, cand.config.rpd_limit
            )
            if reservation is None:
                log.info("provider over capacity, skipping", provider=cand.name)
                continue

            if fallback_chain:
                orchestrator_fallbacks_total.labels(
                    from_provider=fallback_chain[-1], to_provider=cand.name
                ).inc()
            fallback_chain.append(cand.name)

            log.info(
                "dispatching",
                provider=cand.name,
                strategy=strategy,
                attempt=len(fallback_chain),
            )
            result = await self._try_with_retry(cand, req)
            await self._commit_attempt(
                rate_repo, usage_repo, reservation, result,
                strategy=strategy,
                fallback_position=len(fallback_chain),
                client_hash=client_hash,
            )

            if result.response is not None:
                resp = result.response
                latency_ms = int((time.perf_counter() - started_total) * 1000)
                log.info(
                    "completed",
                    provider=resp.provider,
                    strategy=strategy,
                    latency_ms=latency_ms,
                    tokens=resp.total_tokens,
                    chain=fallback_chain,
                )
                return ChatCompletionResponse(
                    id=f"freeai-{uuid.uuid4().hex[:12]}",
                    created=int(time.time()),
                    model=resp.model,
                    provider=resp.provider,
                    strategy_used=strategy,
                    choices=[Choice(message=ChoiceMessage(content=resp.content))],
                    usage=Usage(
                        prompt_tokens=resp.prompt_tokens,
                        completion_tokens=resp.completion_tokens,
                        total_tokens=resp.total_tokens,
                    ),
                    latency_ms=latency_ms,
                    fallback_chain=fallback_chain,
                )
            last_error = result.error
            log.warning(
                "provider failed",
                provider=cand.name,
                kind=last_error.kind.value if last_error else "unknown",
                message=last_error.message if last_error else "?",
            )
            if last_error and last_error.kind == ErrorKind.CLIENT_ERROR:
                break

        raise ProviderError(
            "orchestrator",
            f"all providers failed; last: {last_error.message if last_error else 'unknown'}",
            kind=last_error.kind if last_error else ErrorKind.UNKNOWN,
        )

    # ──────────────── public: streaming ────────────────

    async def stream(
        self,
        req: ChatCompletionRequest,
        config_repo: ConfigRepository,
        rate_repo: RateRepository,
        usage_repo: UsageRepository,
        strategy_repo: StrategyRepository,
        client_hash: Optional[str] = None,
    ) -> AsyncIterator[dict]:
        app_cfg = await config_repo.get_app_config()
        strategy, wanted_tags, _ = await self._resolve_strategy(req, strategy_repo)
        candidates = await self._rank(config_repo, rate_repo, wanted_tags, req.preferred_provider)
        if not candidates:
            raise ProviderError(
                "orchestrator",
                "no provider configured/available — add an API key in settings",
                kind=ErrorKind.CLIENT_ERROR,
            )

        use_fallback = req.fallback and app_cfg.enable_fallback
        attempts = candidates if use_fallback else candidates[:1]
        completion_id = f"freeai-{uuid.uuid4().hex[:12]}"
        created = int(time.time())
        last_error: Optional[ProviderError] = None
        fallback_position = 0

        for cand in attempts:
            if not cand.provider.supports_streaming:
                continue
            reservation = await rate_repo.try_reserve(
                cand.name, cand.config.rpm_limit, cand.config.rpd_limit
            )
            if reservation is None:
                continue
            fallback_position += 1

            started = time.perf_counter()
            first_chunk_sent = False
            ttfb_ms: Optional[int] = None
            model_seen: Optional[str] = None
            prompt_tokens = 0
            completion_tokens = 0
            try:
                stream_iter = cand.provider.stream(
                    req.messages,
                    model=req.model,
                    temperature=req.temperature,
                    max_tokens=req.max_tokens,
                    client=self._client,
                )
                async for chunk in stream_iter:
                    if not first_chunk_sent:
                        first_chunk_sent = True
                        ttfb_ms = int((time.perf_counter() - started) * 1000)
                    model_seen = chunk.model
                    if chunk.prompt_tokens:
                        prompt_tokens = chunk.prompt_tokens
                    if chunk.completion_tokens:
                        completion_tokens = chunk.completion_tokens
                    yield self._format_sse_chunk(chunk, completion_id, created, strategy)
                latency_ms = int((time.perf_counter() - started) * 1000)
                provider_call_duration_seconds.labels(provider=cand.name).observe(latency_ms / 1000.0)
                provider_calls_total.labels(provider=cand.name, outcome="success").inc()
                await rate_repo.commit(reservation, latency_ms, ok=True)
                await usage_repo.record(
                    UsageEvent(
                        provider=cand.name,
                        model=model_seen,
                        strategy=strategy,
                        outcome="success",
                        latency_ms=latency_ms,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        ttfb_ms=ttfb_ms,
                        fallback_position=fallback_position,
                        client_hash=client_hash,
                    )
                )
                yield self._format_sse_done(completion_id, created, cand.name, strategy)
                return
            except ProviderError as e:
                latency_ms = int((time.perf_counter() - started) * 1000)
                provider_calls_total.labels(provider=cand.name, outcome=e.kind.value).inc()
                quarantine = e.retry_after if e.kind == ErrorKind.RATE_LIMITED else None
                if e.kind == ErrorKind.AUTH:
                    quarantine = 24 * 3600
                await rate_repo.commit(
                    reservation, latency_ms, ok=False,
                    error=e.message, error_kind=e.kind.value,
                    quarantine_seconds=quarantine,
                )
                await usage_repo.record(
                    UsageEvent(
                        provider=cand.name,
                        model=None,
                        strategy=strategy,
                        outcome=e.kind.value,
                        latency_ms=latency_ms,
                        fallback_position=fallback_position,
                        client_hash=client_hash,
                    )
                )
                last_error = e
                if first_chunk_sent:
                    raise
                if e.kind == ErrorKind.CLIENT_ERROR:
                    break
                continue

        raise ProviderError(
            "orchestrator",
            f"all providers failed; last: {last_error.message if last_error else 'unknown'}",
            kind=last_error.kind if last_error else ErrorKind.UNKNOWN,
        )

    @staticmethod
    def _format_sse_chunk(chunk: StreamChunk, cid: str, created: int, strategy: str) -> dict:
        return {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": chunk.model,
            "provider": chunk.provider,
            "strategy_used": strategy,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": chunk.delta},
                    "finish_reason": chunk.finish_reason,
                }
            ],
        }

    @staticmethod
    def _format_sse_done(cid: str, created: int, provider: str, strategy: str) -> dict:
        return {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "provider": provider,
            "strategy_used": strategy,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
