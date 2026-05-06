"""First-run setup endpoints — public until completed, then disabled.

Covers the master-key paste flow, the combined first-admin form, and the
legacy ``/api/setup/initial`` admin-token bootstrap. Once an installation
has a real admin user these endpoints reject further calls.
"""
from __future__ import annotations

from typing import Optional, Self

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete as sa_delete, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import hash_password
from ..bootstrap import consume_bootstrap_token, verify_bootstrap_token
from ..crypto import (
    confirm_pending_master_key,
    hash_admin_token,
    is_master_key_ready,
    master_key_confirmation_required,
)
from ..db import get_session
from ..db.models import (
    AppConfigRow,
    ClientRow,
    ProviderStatsRow,
    RateEventRow,
    UsageEventRow,
    UserProviderRow,
)
from ..logging_config import get_logger
from ..repositories import ConfigRepository, UserRepository
from ..repositories.config_repo import DEFAULT_PROVIDERS
from ..settings import get_settings
from ._common import acquire_setup_lock, is_placeholder

router = APIRouter(prefix="/api/setup", tags=["setup"])
log = get_logger("freeai.setup")


class SetupStatusResponse(BaseModel):
    needs_initial_setup: bool
    needs_master_key_confirm: bool
    provider_names: list[str]


class InitialSetupBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    admin_token: str = Field(..., min_length=12, max_length=512)
    admin_token_confirm: str = Field(..., min_length=12, max_length=512)
    provider_keys: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _tokens_match(self) -> Self:
        if self.admin_token != self.admin_token_confirm:
            raise ValueError("admin tokens do not match")
        return self


class MasterKeyConfirmBody(BaseModel):
    master_key: str = Field(..., min_length=1, max_length=512)


class FirstAdminSetupBody(BaseModel):
    """One-shot first admin: optional pending master key + JWT user."""

    master_key: Optional[str] = Field(default=None, max_length=512)
    username: str = Field(..., min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(..., min_length=8, max_length=512)
    password_confirm: str = Field(..., min_length=8, max_length=512)

    @model_validator(mode="after")
    def _passwords_match(self) -> Self:
        if self.password != self.password_confirm:
            raise ValueError("passwords do not match")
        return self


async def _needs_initial_setup(session: AsyncSession) -> bool:
    settings = get_settings()
    if settings.admin_token:
        return False
    if settings.admin_token_path.exists():
        return False
    row = await session.get(AppConfigRow, 1)
    if row and row.admin_token_hash:
        return False

    if not is_master_key_ready():
        return False

    user_repo = UserRepository(session)
    user_count = await user_repo.count()
    placeholder = await user_repo.find_by_username("admin")
    real_users = user_count - (1 if is_placeholder(placeholder) else 0)
    if real_users == 0 and not settings.legacy_initial_setup:
        return False

    cfg = ConfigRepository(session)
    return await cfg.count_providers_with_stored_keys() == 0


@router.get("/status", response_model=SetupStatusResponse)
async def setup_status(session: AsyncSession = Depends(get_session)) -> SetupStatusResponse:
    return SetupStatusResponse(
        needs_initial_setup=await _needs_initial_setup(session),
        needs_master_key_confirm=master_key_confirmation_required(),
        provider_names=sorted(DEFAULT_PROVIDERS.keys()),
    )


@router.post("/confirm-master-key")
async def setup_confirm_master_key(
    body: MasterKeyConfirmBody,
    session: AsyncSession = Depends(get_session),
    x_bootstrap_token: Optional[str] = Header(default=None, alias="X-Bootstrap-Token"),
) -> dict:
    """Promote ``data/.master_key.pending`` to ``.master_key`` after UI paste.

    Requires the bootstrap token so a drive-by cannot confirm encryption
    without both secrets from the operator's logs.
    """
    await acquire_setup_lock(session)
    if not master_key_confirmation_required():
        raise HTTPException(
            status_code=403,
            detail="master key is already active or there is no pending key",
        )
    if not verify_bootstrap_token(x_bootstrap_token):
        raise HTTPException(
            status_code=401,
            detail=(
                "missing or invalid X-Bootstrap-Token header — use the value printed "
                "with the FreeAI bootstrap token banner."
            ),
        )
    if not confirm_pending_master_key(body.master_key.strip()):
        raise HTTPException(
            status_code=400,
            detail="master key does not match the pending server-generated value",
        )
    return {
        "ok": True,
        "detail": (
            "Clave maestra activada. Si existía un archivo .env local y era "
            "escribible, se añadió FREEAI_MASTER_KEY; comprueba y haz backup."
        ),
    }


@router.post("/first-admin", status_code=201)
async def setup_first_admin(
    body: FirstAdminSetupBody,
    session: AsyncSession = Depends(get_session),
    x_bootstrap_token: Optional[str] = Header(default=None, alias="X-Bootstrap-Token"),
) -> dict:
    """Confirm pending master key (if any) and create the first admin in one step."""
    # Imported here to avoid a circular import at module load: auth router
    # also imports from this module's helpers and we don't want a cycle.
    from .auth import issue_tokens

    await acquire_setup_lock(session)

    user_repo = UserRepository(session)
    count = await user_repo.count()
    placeholder = await user_repo.find_by_username("admin")
    placeholder_present = is_placeholder(placeholder)
    real_count = count - (1 if placeholder_present else 0)

    if real_count != 0:
        raise HTTPException(403, "first-admin setup is only available on an empty installation")
    if not verify_bootstrap_token(x_bootstrap_token):
        raise HTTPException(
            status_code=401,
            detail=(
                "missing or invalid X-Bootstrap-Token header — use the value printed "
                "with the FreeAI bootstrap token banner."
            ),
        )

    if master_key_confirmation_required():
        mk = (body.master_key or "").strip()
        if not mk or not confirm_pending_master_key(mk):
            raise HTTPException(
                status_code=400,
                detail="invalid or missing master encryption key — copy the value from the server logs",
            )
    elif not is_master_key_ready():
        raise HTTPException(
            status_code=503,
            detail="master encryption key is not active — set FREEAI_MASTER_KEY or complete pending setup",
        )

    existing = await user_repo.find_by_username(body.username)
    if existing and not (existing == placeholder and placeholder_present):
        raise HTTPException(409, f"username '{body.username}' is already taken")

    pwd_hash = hash_password(body.password)
    user_dto = await user_repo.create(body.username, pwd_hash, role="admin")

    if placeholder_present and placeholder:
        for tbl, col in [
            (UserProviderRow, UserProviderRow.user_id),
            (ClientRow, ClientRow.user_id),
        ]:
            await session.execute(
                sa_update(tbl).where(col == placeholder.id).values(user_id=user_dto.id)
            )
        await session.execute(
            sa_update(RateEventRow).where(RateEventRow.user_id == placeholder.id).values(user_id=user_dto.id)
        )
        await session.execute(
            sa_update(UsageEventRow).where(UsageEventRow.user_id == placeholder.id).values(user_id=user_dto.id)
        )
        await session.execute(
            sa_delete(ProviderStatsRow).where(ProviderStatsRow.user_id == placeholder.id)
        )
        await user_repo.delete(placeholder.id)
        await session.flush()

    consume_bootstrap_token()
    log.info("first_admin_setup_completed", username=body.username)
    return await issue_tokens(user_dto.id, user_dto.username, user_dto.role, session)


@router.post("/initial", status_code=201)
async def setup_initial(
    body: InitialSetupBody,
    session: AsyncSession = Depends(get_session),
    x_bootstrap_token: Optional[str] = Header(default=None, alias="X-Bootstrap-Token"),
) -> dict:
    # Serialize the full setup flow — two concurrent callers would otherwise
    # both pass the _needs_initial_setup check and race on set_admin_token_hash.
    await acquire_setup_lock(session)
    if not await _needs_initial_setup(session):
        raise HTTPException(
            status_code=403,
            detail=(
                "initial setup is not available — already completed, or set "
                "FREEAI_ADMIN_TOKEN / data/admin_token"
            ),
        )
    if not verify_bootstrap_token(x_bootstrap_token):
        raise HTTPException(
            status_code=401,
            detail=(
                "missing or invalid X-Bootstrap-Token header. The one-time "
                "bootstrap token was printed to the server logs on startup "
                "(data/.bootstrap_token)."
            ),
        )
    cfg = ConfigRepository(session)
    await cfg.seed_defaults_if_empty()
    await cfg.set_admin_token_hash(hash_admin_token(body.admin_token))
    for name, key in body.provider_keys.items():
        kn = (name or "").strip().lower()
        if kn not in DEFAULT_PROVIDERS:
            continue
        v = (key or "").strip()
        if not v:
            continue
        await cfg.patch_provider(kn, api_key=v)
    consume_bootstrap_token()
    log.info("initial_setup_completed")
    return {
        "ok": True,
        "detail": (
            "Token de administrador guardado (hash en base de datos). "
            "Las claves de proveedor se almacenan cifradas como siempre. "
            "No volveremos a mostrar el token — cópialo ahora."
        ),
    }
