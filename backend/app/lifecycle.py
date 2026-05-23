"""App lifecycle: startup, migrations, periodic purge, shutdown.

Kept separate from ``main.py`` so the entrypoint stays focused on wiring
(FastAPI app, middlewares, routers) and the bootstrap-flow side effects
(printed master-key + bootstrap-token banners, default seeding, background
tasks) live here where they can be read top-to-bottom without scrolling
past route registrations.
"""
from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI

from .bootstrap import ensure_bootstrap_token, read_bootstrap_token
from .crypto import (
    auto_confirm_pending_master_key,
    ensure_pending_master_key,
    master_key_confirmation_required,
    read_pending_master_key_plaintext,
)
from .db import create_engine_and_sessionmaker, dispose_engine
from .db.models import AppConfigRow
from .logging_config import get_logger
from .metrics import purge_rows_total
from .orchestrator import Orchestrator
from .providers import PROVIDER_REGISTRY
from .repositories import (
    ClientRateRepository,
    ClientRepository,
    ConfigRepository,
    RateRepository,
    StrategyRepository,
    UsageRepository,
    UserRepository,
)
from .routers._common import is_placeholder
from .settings import get_settings

log = get_logger("freeai.lifecycle")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    engine, sessionmaker = create_engine_and_sessionmaker()
    app.state.engine = engine
    app.state.sessionmaker = sessionmaker
    app.state.orchestrator = Orchestrator()

    if settings.auto_migrate:
        await _run_migrations(settings.database_url)

    async with sessionmaker() as session:
        config_repo = ConfigRepository(session)
        strategy_repo = StrategyRepository(session)
        client_repo = ClientRepository(session)

        added = await config_repo.seed_defaults_if_empty()
        if added:
            log.info("seeded_default_providers", count=added)
        strat_added = await strategy_repo.seed_builtins_if_missing()
        if strat_added:
            log.info("seeded_builtin_strategies", count=strat_added)
        await session.commit()

        if not await client_repo.has_any():
            log.warning(
                "bootstrap_mode",
                message="no API clients configured — /v1/* is open. "
                        "Create a client via POST /api/clients before exposing.",
            )

        user_repo = UserRepository(session)
        user_count = await user_repo.count()
        placeholder = await user_repo.find_by_username("admin")
        real_users = user_count - (1 if is_placeholder(placeholder) else 0)
        cfg_row = await session.get(AppConfigRow, 1)
        has_admin_token = (
            bool(cfg_row and cfg_row.admin_token_hash)
            or bool(settings.admin_token)
            or settings.admin_token_path.exists()
        )
        needs_bootstrap = real_users == 0 and not has_admin_token

        mk_plain = ensure_pending_master_key()

        # Default mode: silently promote the pending key so the first request
        # to the panel sees an active master key — no banner, no copy-paste.
        # Paranoid mode keeps the legacy "operator pastes the key" flow.
        if mk_plain and not settings.require_bootstrap_header:
            if auto_confirm_pending_master_key():
                log.info(
                    "master_key_auto_confirmed",
                    hint="default-mode startup; set FREEAI_REQUIRE_BOOTSTRAP_HEADER=true "
                         "to require manual confirmation instead",
                )
                mk_plain = None  # don't fall through to the paranoid banner

        if mk_plain:
            print(
                "\n"
                "============================================================\n"
                "  FreeAI encryption master key (paste in the web UI once):\n"
                f"    {mk_plain}\n"
                "  FREEAI_REQUIRE_BOOTSTRAP_HEADER is set — paste this key,\n"
                "  the bootstrap token, and admin credentials in the FIRST\n"
                "  SETUP form. Pending at data/.master_key.pending until\n"
                "  confirmed.\n"
                "============================================================\n",
                flush=True,
            )
        elif master_key_confirmation_required():
            pk = read_pending_master_key_plaintext()
            if pk:
                # Server restarted with a leftover pending key. Try to
                # auto-promote in default mode; only fall through to the
                # paranoid banner if the operator opted into manual mode.
                if not settings.require_bootstrap_header and auto_confirm_pending_master_key():
                    log.info("master_key_auto_confirmed_on_restart")
                else:
                    print(
                        "\n"
                        "============================================================\n"
                        "  FreeAI master key still awaiting UI confirmation — paste:\n"
                        f"    {pk}\n"
                        "  (Server was restarted before you confirmed the key.)\n"
                        "============================================================\n",
                        flush=True,
                    )

        new_token = ensure_bootstrap_token(needed=needs_bootstrap)
        # Always reprint the banner while a token is still pending — covers
        # both cold starts and restarts, and both default-mode (loopback UI
        # may not be reachable, e.g. browser on another LAN host) and
        # paranoid-mode operators who tail the log to copy by hand.
        token_to_print = new_token or (
            read_bootstrap_token() if needs_bootstrap else None
        )

        if token_to_print:
            print(
                "\n"
                "============================================================\n"
                "  FreeAI bootstrap token (one-time, do not share):\n"
                f"    {token_to_print}\n"
                "  Paste it in the setup form's X-Bootstrap-Token field when\n"
                "  creating the first admin (or send the header on POST\n"
                "  /api/setup/first-admin, /api/setup/initial, /api/auth/register).\n"
                "  Stored at data/.bootstrap_token until consumed.\n"
                "============================================================\n",
                flush=True,
            )

    log.info("freeai_ready", providers=len(PROVIDER_REGISTRY))
    purge_task = asyncio.create_task(_periodic_purge(sessionmaker))
    try:
        yield
    finally:
        purge_task.cancel()
        try:
            await purge_task
        except asyncio.CancelledError:
            pass
        await app.state.orchestrator.aclose()
        await dispose_engine(engine)


async def _run_migrations(database_url: str) -> None:
    log.info("running_migrations")
    proc = await asyncio.subprocess.create_subprocess_exec(
        sys.executable, "-m", "alembic", "upgrade", "head",
        cwd=str(Path(__file__).parent.parent),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if stdout:
        # Keep the full output at debug only — verbose schema details don't
        # belong in production logs. If the run fails we print it below.
        for line in stdout.decode().splitlines():
            log.debug("alembic_stdout", line=line)
    if proc.returncode != 0:
        err = stderr.decode(errors="replace")
        log.error(
            "migration_failed",
            returncode=proc.returncode,
            stdout=stdout.decode(errors="replace")[-2000:],
            stderr=err[-2000:],
        )
        raise RuntimeError(f"alembic upgrade failed (exit {proc.returncode})")
    log.info("migrations_done")


async def _periodic_purge(sessionmaker) -> None:
    """Background loop that trims event tables every hour and rolls up dailies.

    rate_events: keep 2 days (only rpm/rpd windows matter).
    client_rate_events: keep 2 days (only per-minute window matters).
    usage_events: keep 90 days (feeds the real-time analytics dashboard).
    usage_daily_rollup: keep 730 days (feeds historical analytics).

    Rollup order: compute today + yesterday rollups BEFORE purging usage_events,
    so a late-arrival row never falls off the 90d edge without being counted.
    """
    while True:
        await asyncio.sleep(3600)
        try:
            async with sessionmaker() as session:
                usage_repo = UsageRepository(session)
                # Roll up yesterday (closes out late arrivals) then today
                # (still-accumulating, but kept fresh for historical views).
                today_utc = datetime.now(timezone.utc).date()
                yesterday_utc = today_utc - timedelta(days=1)
                rolled_yday = await usage_repo.rollup_day(yesterday_utc)
                rolled_today = await usage_repo.rollup_day(today_utc)

                purged = {
                    "rate_events": await RateRepository(session).purge_old_events(86400 * 2),
                    "client_rate_events": await ClientRateRepository(session).purge_older_than(86400 * 2),
                    "usage_events": await usage_repo.purge_older_than(86400 * 90),
                    "usage_daily_rollup": await usage_repo.purge_rollups_older_than(730),
                }
                await session.commit()
                if any(purged.values()):
                    for table, rows in purged.items():
                        purge_rows_total.labels(table=table).inc(rows)
                log.info(
                    "periodic_purge",
                    **purged,
                    rollup_rows_yesterday=rolled_yday,
                    rollup_rows_today=rolled_today,
                )
        except Exception as exc:  # noqa: BLE001
            log.error("periodic_purge_failed", error=str(exc))
