"""AdminAccessSession repository: open, find active, revoke, list for audit."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.admin_access import AdminAccessSession
from app.models.enums import AdminAccessMethod
from app.models.user import User
from app.repositories.base import BaseRepository


class AdminAccessSessionRepository(BaseRepository[AdminAccessSession]):
    model = AdminAccessSession

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def open(
        self,
        user_id: int,
        *,
        auth_method: AdminAccessMethod,
        created_at: datetime,
        expires_at: datetime,
    ) -> AdminAccessSession:
        """Insert one session. Both timestamps come from the caller's clock."""
        return await self.create_and_add(
            user_id=user_id,
            auth_method=auth_method,
            created_at=created_at,
            expires_at=expires_at,
        )

    # --- the authorization question -------------------------------------------

    @staticmethod
    def _active(now: datetime) -> tuple[ColumnElement[bool], ...]:
        return (
            AdminAccessSession.revoked_at.is_(None),
            AdminAccessSession.expires_at > now,
        )

    async def get_active_for_telegram_id(
        self, telegram_id: int, *, now: datetime
    ) -> AdminAccessSession | None:
        """The session that grants ``telegram_id`` admin rights at ``now``, if any."""
        stmt = (
            select(AdminAccessSession)
            .join(User, User.id == AdminAccessSession.user_id)
            .where(User.telegram_id == telegram_id, *self._active(now))
            .order_by(AdminAccessSession.expires_at.desc(), AdminAccessSession.id.desc())
            .limit(1)
        )
        result = await self.session.scalars(stmt)
        return result.first()

    async def list_active_for_user(
        self, user_id: int, *, now: datetime
    ) -> list[AdminAccessSession]:
        stmt = (
            select(AdminAccessSession)
            .where(AdminAccessSession.user_id == user_id, *self._active(now))
            .order_by(AdminAccessSession.id)
        )
        result = await self.session.scalars(stmt)
        return list(result.all())

    async def count_active(self, *, now: datetime) -> int:
        value = await self.session.scalar(
            select(func.count()).select_from(AdminAccessSession).where(*self._active(now))
        )
        return int(value or 0)

    # --- audit ------------------------------------------------------------------

    async def list_for_user(self, user_id: int) -> list[AdminAccessSession]:
        """Every session the user ever held, newest first — revoked and expired included."""
        stmt = (
            select(AdminAccessSession)
            .where(AdminAccessSession.user_id == user_id)
            .order_by(AdminAccessSession.id.desc())
        )
        result = await self.session.scalars(stmt)
        return list(result.all())

    # --- revocation ---------------------------------------------------------------

    async def revoke(self, access: AdminAccessSession, *, at: datetime) -> bool:
        """Set ``revoked_at`` unless already set. ``True`` when this call revoked it."""
        if access.revoked_at is not None:
            return False
        await self.update(access, revoked_at=at)
        return True
