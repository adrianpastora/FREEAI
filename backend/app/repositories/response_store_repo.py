"""Postgres-backed state for the Responses API.

Stores, per user, the conversation behind each response id (so clients can
chain with ``previous_response_id``) and individual items (so
``item_reference`` input items resolve). Survives restarts and works across
replicas, unlike the in-process store it replaces. Everything expires after
``TTL_S`` and is removed by the periodic purge.
"""
from __future__ import annotations

import json
import time
from typing import Iterable, Optional

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import ResponseStoreRow
from ..schemas import ChatMessage

TTL_S = 24 * 3600
# A history full of inline images can get huge; past this we skip storing
# it (the client then gets the explicit "send the full conversation" 400).
MAX_PAYLOAD_BYTES = 5 * 1024 * 1024
_MAX_KEY_LEN = 128


class ResponseStoreRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def _upsert(self, user_id: int, kind: str, key: str, payload) -> bool:
        if not key or len(key) > _MAX_KEY_LEN:
            return False
        if len(json.dumps(payload)) > MAX_PAYLOAD_BYTES:
            return False
        stmt = pg_insert(ResponseStoreRow).values(
            user_id=user_id, kind=kind, key=key, payload=payload,
            expires_at=time.time() + TTL_S,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[ResponseStoreRow.user_id, ResponseStoreRow.kind, ResponseStoreRow.key],
            set_={"payload": stmt.excluded.payload, "expires_at": stmt.excluded.expires_at},
        )
        await self.session.execute(stmt)
        return True

    async def put_history(self, user_id: int, response_id: str, history: list[ChatMessage]) -> bool:
        payload = [m.model_dump(exclude_none=True) for m in history]
        return await self._upsert(user_id, "history", response_id, payload)

    async def get_history(self, user_id: int, response_id: str) -> Optional[list[ChatMessage]]:
        row = await self.session.get(ResponseStoreRow, (user_id, "history", response_id))
        if row is None or row.expires_at <= time.time():
            return None
        return [ChatMessage(**m) for m in row.payload]

    async def put_items(self, user_id: int, items: Iterable[dict]) -> int:
        stored = 0
        for item in items:
            if not isinstance(item, dict) or item.get("type") == "item_reference":
                continue
            if await self._upsert(user_id, "item", item.get("id") or "", item):
                stored += 1
        return stored

    async def get_items(self, user_id: int, item_ids: Iterable[str]) -> dict[str, dict]:
        ids = [i for i in set(item_ids) if i]
        if not ids:
            return {}
        rows = (await self.session.execute(
            select(ResponseStoreRow).where(
                ResponseStoreRow.user_id == user_id,
                ResponseStoreRow.kind == "item",
                ResponseStoreRow.key.in_(ids),
                ResponseStoreRow.expires_at > time.time(),
            )
        )).scalars().all()
        return {r.key: r.payload for r in rows}

    async def purge_expired(self) -> int:
        result = await self.session.execute(
            delete(ResponseStoreRow).where(ResponseStoreRow.expires_at <= time.time())
        )
        return result.rowcount or 0
