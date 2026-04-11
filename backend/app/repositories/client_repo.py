"""Client repository — inbound API key registry."""
from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import ClientRow


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class ClientDTO:
    name: str
    key_hash: str
    rpm_limit: int
    enabled: bool
    created_at: float


class ClientRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_all(self) -> list[ClientDTO]:
        result = await self.session.execute(select(ClientRow).order_by(ClientRow.created_at))
        return [self._to_dto(r) for r in result.scalars().all()]

    async def has_any(self) -> bool:
        result = await self.session.execute(select(ClientRow.key_hash).limit(1))
        return result.first() is not None

    async def find_by_raw_key(self, raw_key: str) -> Optional[ClientDTO]:
        h = _hash_key(raw_key)
        row = await self.session.get(ClientRow, h)
        return self._to_dto(row) if row else None

    async def create(self, name: str, rpm_limit: int = 60) -> tuple[ClientDTO, str]:
        raw = "fai_" + secrets.token_urlsafe(28)
        row = ClientRow(
            key_hash=_hash_key(raw),
            name=name,
            rpm_limit=rpm_limit,
            enabled=True,
            created_at=time.time(),
        )
        self.session.add(row)
        await self.session.flush()
        return self._to_dto(row), raw

    async def revoke(self, key_hash: str) -> bool:
        result = await self.session.execute(
            delete(ClientRow).where(ClientRow.key_hash == key_hash)
        )
        return result.rowcount > 0

    @staticmethod
    def _to_dto(row: ClientRow) -> ClientDTO:
        return ClientDTO(
            name=row.name,
            key_hash=row.key_hash,
            rpm_limit=row.rpm_limit,
            enabled=row.enabled,
            created_at=row.created_at,
        )
