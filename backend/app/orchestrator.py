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
from .rate_counters import RateCounterStore
from . import strategy_dsl
from .virtual_models import DEFAULT_VIRTUAL_MODEL, is_virtual_model, resolve_virtual_model
from .strategy_dsl import Definition, parse_definition
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
    """In-process TTL cache for strategy lookups (change rarely, read every request).

    Stores parsed `Definition` objects so we don't re-run the DSL parser
    on every request. A cache miss for a strategy that doesn't exist
    yields `_MISSING` (a sentinel) which the resolver translates into a
    CLIENT_ERROR; we can't use None for that because None is the
    legitimate definition for `auto`.
    """
    _TTL = 5.0
    _MISSING = object()

    def __init__(self):
        self._data: dict[str, tuple[float, object]] = {}

    async def get(self, name: str, repo: StrategyRepository) -> object:
        entry = self._data.get(name)
        now = time.monotonic()
        if entry and (now - entry[0]) < self._TTL:
            return entry[1]
        strat = await repo.get(name)
        if strat is None:
            value: object = self._MISSING
        elif strat.definition is None:
            value = None  # `auto` — no DSL rules, handled by detect_auto_strategy
        else:
            value = parse_definition(strat.definition)
        self._data[name] = (now, value)
        return value

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
        # Per-provider in-flight request counters for concurrency-aware scoring.
        # Penalizes providers with many concurrent requests to spread load.
        self._in_flight: dict[str, int] = {}
        # In-memory rate counters — avoids COUNT over rate_events on hot path
        self._counter_store = RateCounterStore()

    def _dec_in_flight(self, name: str) -> None:
        n = self._in_flight.get(name, 1) - 1
        if n <= 0:
            self._in_flight.pop(name, None)
        else:
            self._in_flight[name] = n

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

    def _score(
        self,
        dto: ProviderConfigDTO,
        snap,
        definition: Optional[Definition],
    ) -> Optional[float]:
        """Compute a provider's total score for a given strategy definition.

        Returns:
            None  if the provider is excluded — either unhealthy or
                  rejected by a `require` clause in the DSL.
            float otherwise: baseline + DSL prefer contribution - in_flight penalty.
        """
        if not snap.healthy:
            return None
        ctx = strategy_dsl.context_from_provider(
            name=dto.name,
            enabled=dto.enabled,
            weight=dto.weight,
            tags=dto.tags,
            last_latency_ms=snap.last_latency_ms,
            latency_ema_ms=snap.latency_ema_ms,
            requests_today=snap.requests_today,
            requests_this_minute=snap.requests_this_minute,
            rpd_limit=dto.rpd_limit,
            rpm_limit=dto.rpm_limit,
            tpd_limit=dto.tpd_limit,
            tokens_today=snap.tokens_today,
            total_failures=snap.total_failures,
        )
        baseline = strategy_dsl.baseline_score(ctx)
        if definition is None:
            score = baseline
        else:
            dsl_score = strategy_dsl.score(definition, ctx)
            if dsl_score is None:
                return None
            score = baseline + dsl_score
        # Penalize providers with concurrent in-flight requests
        in_flight = self._in_flight.get(dto.name, 0)
        if in_flight > 0:
            score -= 0.5 * in_flight
        return score

    async def _resolve_strategy(
        self,
        req: ChatCompletionRequest,
        strategy_repo: StrategyRepository,
    ) -> tuple[Strategy, Optional[Definition], Optional[AutoSignal], Optional[str], Optional[str]]:
        """Returns (strategy, definition, auto_signal, virtual_model_id, provider_model).

        ``virtual_model_id``: the virtual model name if one was used, else None.
        ``provider_model``: the model name to actually send to the provider.
            - None when virtual (provider uses its own default_model).
            - The original req.model when it's a real model passthrough.

        This method never mutates ``req``.
        """
        virtual_model_id: Optional[str] = None
        provider_model: Optional[str] = req.model

        if is_virtual_model(req.model):
            vm = resolve_virtual_model(req.model)
            virtual_model_id = vm.id
            strategy_name = vm.strategy
            provider_model = None  # provider picks its default
        elif req.model is None and req.strategy == "auto":
            virtual_model_id = DEFAULT_VIRTUAL_MODEL
            strategy_name = "auto"
            provider_model = None
        else:
            strategy_name = req.strategy

        if strategy_name == "auto":
            signal = detect_auto_strategy(req.messages)
            effective = signal.strategy
        else:
            signal = None
            effective = strategy_name

        cached = await self._strategy_cache.get(effective, strategy_repo)
        if cached is _StrategyCache._MISSING:
            raise ProviderError(
                "orchestrator",
                f"unknown strategy '{effective}' — create it first via POST /api/strategies",
                kind=ErrorKind.CLIENT_ERROR,
            )
        return effective, cached, signal, virtual_model_id, provider_model  # type: ignore[return-value]

    async def _rank(
        self,
        config_repo: ConfigRepository,
        rate_repo: RateRepository,
        definition: Optional[Definition],
        preferred: Optional[str],
        usage_repo: Optional[UsageRepository] = None,
    ) -> list[_Candidate]:
        providers = await config_repo.list_providers()
        eligible = [dto for dto in providers if dto.enabled and dto.api_key]
        if not eligible:
            return []
        snapshots = await rate_repo.snapshot_all(
            [dto.name for dto in eligible],
            counter_store=self._counter_store,
        )

        candidates: list[_Candidate] = []
        for dto in eligible:
            snap = snapshots.get(dto.name)
            if not snap or not snap.healthy:
                continue
            provider = self._build_provider(dto)
            if not provider:
                continue
            score = self._score(dto, snap, definition)
            if score is None:
                continue
            if preferred and dto.name == preferred:
                score += 100
            candidates.append(_Candidate(name=dto.name, provider=provider, score=score, config=dto))
        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates

    # ──────────────── single-attempt helper ────────────────

    async def _attempt(
        self, provider: BaseProvider, req: ChatCompletionRequest, provider_model: Optional[str],
    ) -> ProviderResponse:
        return await provider.complete(
            req.messages,
            model=provider_model,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
            client=self._client,
        )

    async def _try_with_retry(
        self, cand: _Candidate, req: ChatCompletionRequest, provider_model: Optional[str],
    ) -> _AttemptResult:
        started = time.perf_counter()
        last_error: Optional[ProviderError] = None
        for attempt in range(self._MAX_RETRIES + 1):
            try:
                resp = await self._attempt(cand.provider, req, provider_model)
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
        """Record the outcome of a provider attempt. Never raises — a DB
        error during commit must not mask a successful provider response."""
        provider_call_duration_seconds.labels(provider=reservation.provider).observe(
            result.latency_ms / 1000.0
        )
        try:
            await self._commit_attempt_inner(
                rate_repo, usage_repo, reservation, result,
                strategy, fallback_position, client_hash,
            )
        except Exception:  # noqa: BLE001
            log.error(
                "commit_attempt_failed",
                provider=reservation.provider,
                exc_info=True,
            )

    async def _commit_attempt_inner(
        self,
        rate_repo: RateRepository,
        usage_repo: UsageRepository,
        reservation: ReservationToken,
        result: _AttemptResult,
        strategy: str,
        fallback_position: int,
        client_hash: Optional[str],
    ) -> None:
        outcome: str
        if result.response is not None:
            outcome = "success"
            provider_calls_total.labels(provider=reservation.provider, outcome="success").inc()
            await rate_repo.commit(
                reservation, result.latency_ms, ok=True,
                prompt_tokens=result.response.prompt_tokens,
                completion_tokens=result.response.completion_tokens,
            )
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
        strategy, definition, signal, virtual_model_id, provider_model = await self._resolve_strategy(req, strategy_repo)
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

        candidates = await self._rank(config_repo, rate_repo, definition, req.preferred_provider, usage_repo)
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
            self._counter_store.record(cand.name)

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
            self._in_flight[cand.name] = self._in_flight.get(cand.name, 0) + 1
            try:
                result = await self._try_with_retry(cand, req, provider_model)
            finally:
                self._dec_in_flight(cand.name)
            await self._commit_attempt(
                rate_repo, usage_repo, reservation, result,
                strategy=strategy,
                fallback_position=len(fallback_chain),
                client_hash=client_hash,
            )

            if result.response is not None:
                resp = result.response
                latency_ms = int((time.perf_counter() - started_total) * 1000)
                # When a virtual model was requested, show it in the response
                # so the client sees e.g. "freeai-fast" instead of the
                # provider-specific model name.
                response_model = virtual_model_id if virtual_model_id else resp.model
                log.info(
                    "completed",
                    provider=resp.provider,
                    strategy=strategy,
                    virtual_model=virtual_model_id,
                    real_model=resp.model,
                    latency_ms=latency_ms,
                    tokens=resp.total_tokens,
                    chain=fallback_chain,
                )
                return ChatCompletionResponse(
                    id=f"freeai-{uuid.uuid4().hex[:12]}",
                    created=int(time.time()),
                    model=response_model,
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
                    real_model=resp.model if virtual_model_id else None,
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
        strategy, definition, _, virtual_model_id, provider_model = await self._resolve_strategy(req, strategy_repo)
        candidates = await self._rank(config_repo, rate_repo, definition, req.preferred_provider, usage_repo)
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
            self._counter_store.record(cand.name)
            fallback_position += 1
            self._in_flight[cand.name] = self._in_flight.get(cand.name, 0) + 1

            started = time.perf_counter()
            first_chunk_sent = False
            ttfb_ms: Optional[int] = None
            model_seen: Optional[str] = None
            prompt_tokens = 0
            completion_tokens = 0
            try:
                stream_iter = cand.provider.stream(
                    req.messages,
                    model=provider_model,
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
                    # Override model name in SSE chunks when using virtual models
                    if virtual_model_id:
                        chunk = StreamChunk(
                            delta=chunk.delta,
                            provider=chunk.provider,
                            model=virtual_model_id,
                            finish_reason=chunk.finish_reason,
                            prompt_tokens=chunk.prompt_tokens,
                            completion_tokens=chunk.completion_tokens,
                        )
                    yield self._format_sse_chunk(chunk, completion_id, created, strategy)
                latency_ms = int((time.perf_counter() - started) * 1000)
                provider_call_duration_seconds.labels(provider=cand.name).observe(latency_ms / 1000.0)
                provider_calls_total.labels(provider=cand.name, outcome="success").inc()
                await rate_repo.commit(
                    reservation, latency_ms, ok=True,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )
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
                self._dec_in_flight(cand.name)
                yield self._format_sse_done(completion_id, created, cand.name, strategy)
                return
            except ProviderError as e:
                self._dec_in_flight(cand.name)
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
