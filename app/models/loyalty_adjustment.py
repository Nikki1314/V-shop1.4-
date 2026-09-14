"""Who credited a customer's stamps by hand — the audit half of a manual adjustment.

The stamps themselves are an ordinary ``loyalty_transactions`` row of kind
``adjustment`` (positive amount, the reason as its note), written by
:class:`~app.services.loyalty.LoyaltyService` under the customer's account lock
like every other stamp movement. This table adds what the ledger row cannot
say: which operator did it, under which authority (a configured admin, or a
break-glass session it names), and the operation id that makes a repeated
request the same adjustment rather than a second one.

One row per ledger row, and one row per operation id — both unique. A manual
adjustment therefore cannot be booked twice, and every ``adjustment`` row the
service writes has exactly one author on record.
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.database.base import Base, TimestampMixin
from app.models.enums import AdminAccessKind
from app.models.types import enum_values

OPERATION_ID_LENGTH = 36  # a canonical UUID


class LoyaltyStampAdjustment(Base, TimestampMixin):
    """The author and the idempotency key of one manual stamp credit."""

    __tablename__ = "loyalty_stamp_adjustments"
    __table_args__ = (
        CheckConstraint(
            "(actor_kind = 'break_glass') = (access_session_id IS NOT NULL)",
            name="ck_loyalty_stamp_adjustments_session_matches_actor",
        ),
        CheckConstraint(
            "user_id <> actor_user_id",
            name="ck_loyalty_stamp_adjustments_not_self",
        ),
        UniqueConstraint("transaction_id", name="uq_loyalty_stamp_adjustments_transaction_id"),
        UniqueConstraint("operation_id", name="uq_loyalty_stamp_adjustments_operation_id"),
        # A customer's manual credits, newest first.
        Index("ix_loyalty_stamp_adjustments_user_id_id", "user_id", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    transaction_id: Mapped[int] = mapped_column(
        ForeignKey("loyalty_transactions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # The customer credited: the same user the ledger row belongs to.
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # The operator, as a registered user, and how they were authorized.
    actor_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    actor_kind: Mapped[AdminAccessKind] = mapped_column(
        Enum(
            AdminAccessKind,
            name="admin_access_kind",
            native_enum=False,
            length=32,
            values_callable=enum_values,
        ),
        nullable=False,
    )
    access_session_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_access_sessions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    operation_id: Mapped[str] = mapped_column(String(OPERATION_ID_LENGTH), nullable=False)

    # An audit row is a fact about the past: nothing on it changes.
    @validates(
        "transaction_id",
        "user_id",
        "actor_user_id",
        "actor_kind",
        "access_session_id",
        "operation_id",
    )
    def _set_once(self, key: str, value: object) -> object:
        current = self.__dict__.get(key)
        if current is not None and value != current:
            raise ValueError(f"LoyaltyStampAdjustment.{key} never changes once set")
        return value

    def __repr__(self) -> str:
        return (
            f"<LoyaltyStampAdjustment id={self.id} user_id={self.user_id} "
            f"actor_user_id={self.actor_user_id} kind={self.actor_kind} "
            f"transaction_id={self.transaction_id}>"
        )
