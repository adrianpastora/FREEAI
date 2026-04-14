"""Repositories for users and refresh tokens."""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import RefreshTokenRow, UserRow


@dataclass(frozen=True)
class UserDTO:
    id: int
    username: str
    password_hash: str
    role: str
    max_clients: int
    created_at: float


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(
        self,
        username: str,
        password_hash: str,
        role: str = "user",
        max_clients: int = 5,
    ) -> UserDTO:
        row = UserRow(
            username=username,
            password_hash=password_hash,
            role=role,
            max_clients=max_clients,
            created_at=time.time(),
            updated_at=time.time(),
        )
        self._s.add(row)
        await self._s.flush()
        return self._to_dto(row)

    async def find_by_username(self, username: str) -> Optional[UserDTO]:
        stmt = select(UserRow).where(UserRow.username == username)
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        return self._to_dto(row) if row else None

    async def find_by_id(self, user_id: int) -> Optional[UserDTO]:
        row = await self._s.get(UserRow, user_id)
        return self._to_dto(row) if row else None

    async def list_all(self) -> list[UserDTO]:
        stmt = select(UserRow).order_by(UserRow.id)
        rows = (await self._s.execute(stmt)).scalars().all()
        return [self._to_dto(r) for r in rows]

    async def find_first_admin(self) -> Optional[UserDTO]:
        stmt = select(UserRow).where(UserRow.role == "admin").order_by(UserRow.id).limit(1)
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        return self._to_dto(row) if row else None

    async def count(self) -> int:
        stmt = select(func.count()).select_from(UserRow)
        return (await self._s.execute(stmt)).scalar_one()

    async def delete(self, user_id: int) -> bool:
        stmt = delete(UserRow).where(UserRow.id == user_id)
        result = await self._s.execute(stmt)
        return result.rowcount > 0

    async def update_password(self, user_id: int, password_hash: str) -> bool:
        row = await self._s.get(UserRow, user_id)
        if not row:
            return False
        row.password_hash = password_hash
        row.updated_at = time.time()
        return True

    async def update_role(self, user_id: int, role: str) -> bool:
        row = await self._s.get(UserRow, user_id)
        if not row:
            return False
        row.role = role
        row.updated_at = time.time()
        return True

    @staticmethod
    def _to_dto(row: UserRow) -> UserDTO:
        return UserDTO(
            id=row.id,
            username=row.username,
            password_hash=row.password_hash,
            role=row.role,
            max_clients=row.max_clients,
            created_at=row.created_at,
        )


class RefreshTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def store(self, user_id: int, token_hash: str, expires_at: float) -> None:
        row = RefreshTokenRow(
            user_id=user_id,
            token_hash=token_hash,
            expires_at=expires_at,
            created_at=time.time(),
        )
        self._s.add(row)

    async def find_by_hash(self, token_hash: str) -> Optional[RefreshTokenRow]:
        stmt = select(RefreshTokenRow).where(
            RefreshTokenRow.token_hash == token_hash
        )
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def delete_by_hash(self, token_hash: str) -> bool:
        stmt = delete(RefreshTokenRow).where(
            RefreshTokenRow.token_hash == token_hash
        )
        result = await self._s.execute(stmt)
        return result.rowcount > 0

    async def delete_all_for_user(self, user_id: int) -> int:
        stmt = delete(RefreshTokenRow).where(RefreshTokenRow.user_id == user_id)
        result = await self._s.execute(stmt)
        return result.rowcount

    async def delete_expired(self) -> int:
        stmt = delete(RefreshTokenRow).where(
            RefreshTokenRow.expires_at < time.time()
        )
        result = await self._s.execute(stmt)
        return result.rowcount

    @staticmethod
    def hash_token(raw: str) -> str:
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
