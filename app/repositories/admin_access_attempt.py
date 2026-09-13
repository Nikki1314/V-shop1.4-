"""AdminAccessAttempt repository: record attempts, count recent failures, list for audit."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.admin_access import AdminAccessAttempt
from app.models.enums import AdminAccessAttemptOutcome, AdminAccessMethod
from app.repositories.base import BaseRepository


class AdminAccessAttemptRepository(BaseRepository[AdminAccessAttempt]):
    model = AdminAccessAttempt

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def record(
        self,
        user_id: int,
        *,
        auth_method: AdminAccessMethod,
        outcome: AdminAccessAttemptOutcome,
        created_at: datetime,
    ) -> AdminAccessAttempt:
        return await self.create_and_add(
            user_id=user_id,
            auth_method=auth_method,
            outcome=outcome,
            created_at=created_at,
        )

    async def count_failures_since(self, user_id: int, *, since: datetime) -> int:
        """
        Failed attempts by ``user_id`` at or after ``since`` and after their last
        success — a successful login starts the count afresh.
        """
        last_success = (
            select(func.max(AdminAccessAttempt.created_at))
            .where(
                AdminAccessAttempt.user_id == user_id,
                AdminAccessAttempt.outcome == AdminAccessAttemptOutcome.SUCCEEDED,
            )
            .scalar_subquery()
        )
        value = await self.session.scalar(
            select(func.count())
            .select_from(AdminAccessAttempt)
            .where(
                AdminAccessAttempt.user_id == user_id,
                AdminAccessAttempt.outcome == AdminAccessAttemptOutcome.FAILED,
                AdminAccessAttempt.created_at >= since,
                func.coalesce(AdminAccessAttempt.created_at > last_success, True),
            )
        )
        return int(value or 0)

    async def latest_for_user(self, user_id: int) -> AdminAccessAttempt | None:
        result = await self.session.scalars(
            select(AdminAccessAttempt)
            .where(AdminAccessAttempt.user_id == user_id)
            .order_by(AdminAccessAttempt.id.desc())
            .limit(1)
        )
        return result.first()

    async def list_for_user(
        self, user_id: int, *, limit: int | None = None
    ) -> list[AdminAccessAttempt]:
        """Newest first."""
        stmt = (
            select(AdminAccessAttempt)
            .where(AdminAccessAttempt.user_id == user_id)
            .order_by(AdminAccessAttempt.id.desc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self.session.scalars(stmt)
        return list(result.all())
