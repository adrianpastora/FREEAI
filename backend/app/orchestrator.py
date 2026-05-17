"""Provider orchestration — ranking, dispatch, retry, and fallback.

Responsible for turning a ``ChatCompletionRequest`` into an upstream call:
resolves the strategy (user-defined, stored in ``strategies``), snapshots
rate-limit + health for each candidate, scores them, dispatches in order,
and falls back when a provider errors, rate-limits, or returns an empty or
filtered response. Each attempt (success or failure) lands in
``usage_events`` so the analytics panel and Grafana dashboards stay honest.
"""
from __future__ import annotations

import asyncio
import random
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
    AppConfigDTO,
    ConfigRepository,
    PricingRepository,
    ProviderConfigDTO,
    RateRepository,
    ReservationToken,
    StrategyRepository,
    UsageEvent,
    UsageRepository,
)
from .repositories.rate_repo import ProviderSnapshot
from .repositories.user_provider_repo import UserProviderRepository
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
    # Multiplicative jitter applied to the exponential backoff so that
    # concurrent requests that fail at the same instant don't retry in
    # lock-step against an already-struggling provider.
    _RETRY_JITTER_RANGE = (0.5, 1.5)
    # Cap on how long a single retry will wait when honoring a provider's
    # Retry-After header inline. Anything longer than this is the job of
    # quarantine + fallback, not a blocking sleep on the request path.
    _RETRY_AFTER_MAX_S = 5.0
    # Fallback when AppConfigDTO.provider_max_retries is somehow missing.
    _DEFAULT_MAX_RETRIES = 1

    # httpx connection pool. Sized for multi-user concurrent traffic across
    # ~7 providers; each provider opens its own pool entries so the total
    # ceiling is reached when many users hit many providers at once. The
    # pool timeout is deliberately generous: when the pool is saturated we
    # want callers to queue briefly rather than fail with PoolTimeout, since
    # the orchestrator's own per-provider timeouts already bound how long
    # any single request can occupy a slot.
    _HTTP_MAX_CONNECTIONS = 200
    _HTTP_MAX_KEEPALIVE = 50
    _HTTP_POOL_TIMEOUT_S = 30.0

    def __init__(self):
        self._client = httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=self._HTTP_MAX_CONNECTIONS,
                max_keepalive_connections=self._HTTP_MAX_KEEPALIVE,
            ),
            # Per-phase timeouts; each provider adapter further constrains
            # connect/read via _phase_timeout() at call sites that need it.
            timeout=httpx.Timeout(
                connect=10.0, read=120.0, write=30.0, pool=self._HTTP_POOL_TIMEOUT_S,
            ),
        )
        self._strategy_cache = _StrategyCache()
        # Per-(user, provider) in-flight request counters for concurrency-aware
        # scoring. Scoped per-user so one tenant's traffic can't skew another's
        # routing decisions. Bounded at _IN_FLIGHT_MAX_KEYS; entries with a
        # zero count are popped in _dec_in_flight so the map stays lean.
        self._in_flight: dict[tuple[int, str], int] = {}
        # In-memory rate counters — avoids COUNT over rate_events on hot path
        self._counter_store = RateCounterStore()

    _IN_FLIGHT_MAX_KEYS = 10_000

    @property
    def http_client(self) -> httpx.AsyncClient:
        """Shared httpx connection pool — exposed so other dispatchers
        (embeddings, transcription) can reuse it instead of opening their own."""
        return self._client

    @staticmethod
    def _circuit_breaker_kwargs(app_cfg: Optional[AppConfigDTO]) -> dict:
        """Pull breaker tunables from AppConfig with safe fallbacks."""
        if app_cfg is None:
            return {}
        kwargs = {}
        for attr in (
            "circuit_breaker_threshold",
            "circuit_breaker_window_s",
            "circuit_breaker_base_cooldown_s",
            "circuit_breaker_max_cooldown_s",
        ):
            val = getattr(app_cfg, attr, None)
            if val is not None:
                kwargs[attr] = val
        return kwargs

    def _inc_in_flight(self, user_id: int, name: str) -> None:
        key = (user_id, name)
        # Cap the map to guard against unbounded growth from many users/providers.
        # The penalty is a soft hint; losing a slot occasionally is harmless.
        if key not in self._in_flight and len(self._in_flight) >= self._IN_FLIGHT_MAX_KEYS:
            return
        self._in_flight[key] = self._in_flight.get(key, 0) + 1

    def _dec_in_flight(self, user_id: int, name: str) -> None:
        key = (user_id, name)
        n = self._in_flight.get(key, 1) - 1
        if n <= 0:
            self._in_flight.pop(key, None)
        else:
            self._in_flight[key] = n

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
        snap: ProviderSnapshot,
        definition: Optional[Definition],
        user_id: int,
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
        # Penalize providers with concurrent in-flight requests for this user.
        in_flight = self._in_flight.get((user_id, dto.name), 0)
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
        user_id: int,
        user_provider_repo: UserProviderRepository,
        rate_repo: RateRepository,
        definition: Optional[Definition],
        preferred: Optional[str],
    ) -> list[_Candidate]:
        user_providers = await user_provider_repo.list_for_user(user_id)
        eligible = [dto for dto in user_providers if dto.enabled and dto.api_key]
        if not eligible:
            return []
        # Project per-user merged DTOs to the catalog-shaped DTO the ranker expects.
        provider_dtos = [dto.to_provider_config() for dto in eligible]
        snapshots = await rate_repo.snapshot_all(
            user_id,
            [dto.name for dto in provider_dtos],
            counter_store=self._counter_store,
        )

        candidates: list[_Candidate] = []
        for dto in provider_dtos:
            snap = snapshots.get(dto.name)
            if not snap or not snap.healthy:
                continue
            provider = self._build_provider(dto)
            if not provider:
                continue
            score = self._score(dto, snap, definition, user_id)
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

    @classmethod
    def _retry_delay(cls, err: ProviderError, attempt: int) -> float:
        """Decide how long to sleep before the next retry.

        Honors a provider-supplied ``Retry-After`` (capped at
        :attr:`_RETRY_AFTER_MAX_S`) when present — the upstream knows its
        own recovery cadence better than our backoff curve. Otherwise falls
        back to ``base * 2**attempt`` with multiplicative jitter so that
        simultaneous failures don't retry in lock-step.
        """
        if err.retry_after is not None and err.retry_after > 0:
            return min(float(err.retry_after), cls._RETRY_AFTER_MAX_S)
        jitter = random.uniform(*cls._RETRY_JITTER_RANGE)
        return cls._RETRY_BACKOFF_S * (2 ** attempt) * jitter

    async def _try_with_retry(
        self,
        cand: _Candidate,
        req: ChatCompletionRequest,
        provider_model: Optional[str],
        max_retries: int,
    ) -> _AttemptResult:
        started = time.perf_counter()
        last_error: Optional[ProviderError] = None
        attempts_budget = max(0, max_retries)
        for attempt in range(attempts_budget + 1):
            try:
                resp = await self._attempt(cand.provider, req, provider_model)
                latency_ms = int((time.perf_counter() - started) * 1000)
                return _AttemptResult(response=resp, error=None, latency_ms=latency_ms)
            except ProviderError as e:
                last_error = e
                if e.is_transient and attempt < attempts_budget:
                    delay = self._retry_delay(e, attempt)
                    log.info(
                        "retrying transient error",
                        provider=cand.name, kind=e.kind.value,
                        attempt=attempt + 1, max_retries=attempts_budget,
                        delay_s=round(delay, 3),
                        honored_retry_after=e.retry_after is not None,
                    )
                    await asyncio.sleep(delay)
                    continue
                break
            except Exception as e:  # noqa: BLE001
                last_error = ProviderError(cand.name, str(e), kind=ErrorKind.UNKNOWN)
                break
        latency_ms = int((time.perf_counter() - started) * 1000)
        return _AttemptResult(response=None, error=last_error, latency_ms=latency_ms)

    @staticmethod
    async def _resolve_cost(
        pricing_repo: Optional[PricingRepository],
        *,
        provider: str,
        model: Optional[str],
        prompt_tokens: int,
        completion_tokens: int,
    ) -> Optional[float]:
        """Look up the frozen USD cost for one dispatch.

        Returns ``None`` (not 0.0) when no price row is on file — kept
        distinct in analytics so coverage gaps are visible. Errors in the
        pricing lookup are swallowed and treated as "no price": cost
        accounting must never block recording a real dispatch.
        """
        if pricing_repo is None or model is None:
            return None
        try:
            return await pricing_repo.compute_cost_usd(
                provider, model, prompt_tokens, completion_tokens,
            )
        except Exception:  # noqa: BLE001
            log.warning(
                "pricing_lookup_failed",
                provider=provider, model=model, exc_info=True,
            )
            return None

    async def _commit_attempt(
        self,
        rate_repo: RateRepository,
        usage_repo: UsageRepository,
        reservation: ReservationToken,
        result: _AttemptResult,
        strategy: str,
        fallback_position: int,
        client_hash: Optional[str],
        user_id: Optional[int] = None,
        app_cfg: Optional[AppConfigDTO] = None,
        pricing_repo: Optional[PricingRepository] = None,
    ) -> None:
        """Record the outcome of a provider attempt. Never raises — a DB
        error during commit must not mask a successful provider response."""
        provider_call_duration_seconds.labels(provider=reservation.provider).observe(
            result.latency_ms / 1000.0
        )
        try:
            await self._commit_attempt_inner(
                rate_repo, usage_repo, reservation, result,
                strategy, fallback_position, client_hash, user_id,
                app_cfg=app_cfg,
                pricing_repo=pricing_repo,
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
        user_id: Optional[int] = None,
        app_cfg: Optional[AppConfigDTO] = None,
        pricing_repo: Optional[PricingRepository] = None,
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
            cost_usd = await self._resolve_cost(
                pricing_repo,
                provider=reservation.provider,
                model=result.response.model,
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
                    user_id=user_id,
                    cost_usd=cost_usd,
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
        cb_kwargs = self._circuit_breaker_kwargs(app_cfg)
        await rate_repo.commit(
            reservation,
            result.latency_ms,
            ok=False,
            error=err.message,
            error_kind=err.kind.value,
            quarantine_seconds=quarantine,
            **cb_kwargs,
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
                user_id=user_id,
            )
        )

    # ──────────────── public: chat completion ────────────────

    async def chat(
        self,
        req: ChatCompletionRequest,
        user_id: int,
        user_provider_repo: UserProviderRepository,
        config_repo: ConfigRepository,
        rate_repo: RateRepository,
        usage_repo: UsageRepository,
        strategy_repo: StrategyRepository,
        client_hash: Optional[str] = None,
        pricing_repo: Optional[PricingRepository] = None,
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

        candidates = await self._rank(user_id, user_provider_repo, rate_repo, definition, req.preferred_provider)
        if not candidates:
            raise ProviderError(
                "orchestrator",
                "no provider configured/available — add an API key in settings",
                kind=ErrorKind.CLIENT_ERROR,
            )

        # Filter to vision-capable providers when the request contains images
        request_has_images = any(m.has_images for m in req.messages)
        if request_has_images:
            vision_candidates = [c for c in candidates if c.provider.supports_vision]
            if not vision_candidates:
                raise ProviderError(
                    "orchestrator",
                    "this request contains images but no vision-capable provider is "
                    "available. Configure a provider with vision support (Gemini, "
                    "OpenRouter) and ensure it has an API key and the 'vision' tag.",
                    kind=ErrorKind.CLIENT_ERROR,
                )
            candidates = vision_candidates

        fallback_chain: list[str] = []
        last_error: Optional[ProviderError] = None
        use_fallback = req.fallback and app_cfg.enable_fallback
        attempts = candidates if use_fallback else candidates[:1]
        started_total = time.perf_counter()

        for cand in attempts:
            reservation = await rate_repo.try_reserve(
                user_id, cand.name, cand.config.rpm_limit, cand.config.rpd_limit
            )
            if reservation is None:
                log.info("provider over capacity, skipping", provider=cand.name)
                continue
            self._counter_store.record(user_id, cand.name)

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
            self._inc_in_flight(user_id, cand.name)
            max_retries = (
                cand.config.max_retries
                if cand.config.max_retries is not None
                else getattr(app_cfg, "provider_max_retries", self._DEFAULT_MAX_RETRIES)
            )
            reservation_settled = False
            result: Optional[_AttemptResult] = None
            try:
                result = await self._try_with_retry(cand, req, provider_model, max_retries)
                await self._commit_attempt(
                    rate_repo, usage_repo, reservation, result,
                    strategy=strategy,
                    fallback_position=len(fallback_chain),
                    client_hash=client_hash,
                    user_id=user_id,
                    app_cfg=app_cfg,
                    pricing_repo=pricing_repo,
                )
                reservation_settled = True
            finally:
                self._dec_in_flight(user_id, cand.name)
                # Roll back the rate_events row if we never recorded the
                # outcome — a cancelled request otherwise inflates the RPM
                # counter against the client forever.
                if not reservation_settled:
                    try:
                        await rate_repo.rollback(reservation)
                    except Exception:  # noqa: BLE001
                        log.warning(
                            "reservation rollback failed",
                            provider=cand.name, exc_info=True,
                        )

            if result is None:
                # _try_with_retry always returns an _AttemptResult; this only
                # happens if the request was cancelled mid-flight.
                break
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
        user_id: int,
        user_provider_repo: UserProviderRepository,
        config_repo: ConfigRepository,
        rate_repo: RateRepository,
        usage_repo: UsageRepository,
        strategy_repo: StrategyRepository,
        client_hash: Optional[str] = None,
        pricing_repo: Optional[PricingRepository] = None,
    ) -> AsyncIterator[dict]:
        app_cfg = await config_repo.get_app_config()
        strategy, definition, _, virtual_model_id, provider_model = await self._resolve_strategy(req, strategy_repo)
        candidates = await self._rank(user_id, user_provider_repo, rate_repo, definition, req.preferred_provider)
        if not candidates:
            raise ProviderError(
                "orchestrator",
                "no provider configured/available — add an API key in settings",
                kind=ErrorKind.CLIENT_ERROR,
            )

        # Filter to vision-capable providers when the request contains images
        request_has_images = any(m.has_images for m in req.messages)
        if request_has_images:
            vision_candidates = [c for c in candidates if c.provider.supports_vision]
            if not vision_candidates:
                raise ProviderError(
                    "orchestrator",
                    "this request contains images but no vision-capable provider is "
                    "available. Configure a provider with vision support (Gemini, "
                    "OpenRouter) and ensure it has an API key and the 'vision' tag.",
                    kind=ErrorKind.CLIENT_ERROR,
                )
            candidates = vision_candidates

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
                user_id, cand.name, cand.config.rpm_limit, cand.config.rpd_limit
            )
            if reservation is None:
                continue
            self._counter_store.record(user_id, cand.name)
            fallback_position += 1
            self._inc_in_flight(user_id, cand.name)

            started = time.perf_counter()
            first_chunk_sent = False
            ttfb_ms: Optional[int] = None
            model_seen: Optional[str] = None
            prompt_tokens = 0
            completion_tokens = 0
            reservation_settled = False
            stream_iter = None
            idle_timeout = getattr(app_cfg, "stream_idle_timeout_s", 45.0)
            try:
                try:
                    stream_iter = cand.provider.stream(
                        req.messages,
                        model=provider_model,
                        temperature=req.temperature,
                        max_tokens=req.max_tokens,
                        client=self._client,
                    )
                    # Manual iteration so we can enforce a per-chunk idle timeout.
                    # A provider that goes silent after accepting the request
                    # would otherwise hang this coroutine indefinitely.
                    while True:
                        try:
                            chunk = await asyncio.wait_for(
                                stream_iter.__anext__(), timeout=idle_timeout
                            )
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError as te:
                            # Close the upstream generator; it owns the httpx
                            # stream context and must release it.
                            try:
                                await stream_iter.aclose()
                            except Exception:  # noqa: BLE001
                                pass
                            raise ProviderError(
                                cand.name,
                                f"stream idle for {idle_timeout}s",
                                kind=ErrorKind.NETWORK,
                            ) from te
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
                    reservation_settled = True
                    cost_usd = await self._resolve_cost(
                        pricing_repo,
                        provider=cand.name,
                        model=model_seen,
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
                            user_id=user_id,
                            cost_usd=cost_usd,
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
                        **self._circuit_breaker_kwargs(app_cfg),
                    )
                    reservation_settled = True
                    await usage_repo.record(
                        UsageEvent(
                            provider=cand.name,
                            model=None,
                            strategy=strategy,
                            outcome=e.kind.value,
                            latency_ms=latency_ms,
                            fallback_position=fallback_position,
                            client_hash=client_hash,
                            user_id=user_id,
                        )
                    )
                    last_error = e
                    if first_chunk_sent:
                        raise
                    if e.kind == ErrorKind.CLIENT_ERROR:
                        break
                    continue
            finally:
                # Always release in-flight counter — otherwise a cancelled
                # client or unexpected exception would leak the slot forever
                # and skew scoring until restart.
                self._dec_in_flight(user_id, cand.name)
                # If the request was cancelled / raised before we could record
                # the outcome, roll back the reservation so the rate counters
                # don't carry a "ghost" in-flight call.
                if not reservation_settled:
                    try:
                        await rate_repo.rollback(reservation)
                    except Exception:  # noqa: BLE001
                        log.warning(
                            "reservation rollback failed",
                            provider=cand.name, exc_info=True,
                        )
                # Make sure the upstream httpx stream is released even on
                # client disconnect — otherwise the connection stays checked
                # out of the httpx pool.
                if stream_iter is not None:
                    try:
                        await stream_iter.aclose()
                    except Exception:  # noqa: BLE001
                        pass

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
