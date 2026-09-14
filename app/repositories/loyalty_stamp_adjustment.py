"""LoyaltyStampAdjustment repository: record, find by operation, list for audit."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import AdminAccessKind
from app.models.loyalty import LoyaltyTransaction
from app.models.loyalty_adjustment import LoyaltyStampAdjustment
from app.repositories.base import BaseRepository


class LoyaltyStampAdjustmentRepository(BaseRepository[LoyaltyStampAdjustment]):
    model = LoyaltyStampAdjustment

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def record(
        self,
        *,
        transaction_id: int,
        user_id: int,
        actor_user_id: int,
        actor_kind: AdminAccessKind,
        access_session_id: int | None,
        operation_id: str,
    ) -> LoyaltyStampAdjustment:
        return await self.create_and_add(
            transaction_id=transaction_id,
            user_id=user_id,
            actor_user_id=actor_user_id,
            actor_kind=actor_kind,
            access_session_id=access_session_id,
            operation_id=operation_id,
        )

    async def get_by_operation_id(self, operation_id: str) -> LoyaltyStampAdjustment | None:
        result = await self.session.scalars(
            select(LoyaltyStampAdjustment).where(
                LoyaltyStampAdjustment.operation_id == operation_id
            )
        )
        return result.first()

    async def get_for_transaction(self, transaction_id: int) -> LoyaltyStampAdjustment | None:
        result = await self.session.scalars(
            select(LoyaltyStampAdjustment).where(
                LoyaltyStampAdjustment.transaction_id == transaction_id
            )
        )
        return result.first()

    async def list_for_user(
        self, user_id: int, *, limit: int | None = None
    ) -> list[tuple[LoyaltyStampAdjustment, LoyaltyTransaction]]:
        """A customer's manual credits with their ledger rows, newest first."""
        stmt = (
            select(LoyaltyStampAdjustment, LoyaltyTransaction)
            .join(
                LoyaltyTransaction, LoyaltyTransaction.id == LoyaltyStampAdjustment.transaction_id
            )
            .where(LoyaltyStampAdjustment.user_id == user_id)
            .order_by(LoyaltyStampAdjustment.id.desc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self.session.execute(stmt)
        return [(adjustment, transaction) for adjustment, transaction in result.all()]
