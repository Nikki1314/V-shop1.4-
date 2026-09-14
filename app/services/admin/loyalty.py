"""Manual stamp credits by an operator — the service, not the screen.

An operator credits a customer some stamps (a compensation, a promotion, a
correction). The stamps go through the one path every stamp takes:
:meth:`LoyaltyService.adjust` writes the ``adjustment`` ledger row and moves the
cached balance under the customer's account lock. Beside it this service
records who did it and under which authority (:class:`LoyaltyStampAdjustment`),
keyed by an operation id so the same request booked twice — a double tap, a
redelivered update, a retry after a timeout — is one adjustment.

What the credit does *not* do, on purpose:

* it never issues a reward. Crossing the card's threshold means exactly what it
  means after a purchase: the card shows a free bottle can be claimed, and the
  customer claims it (:meth:`StampCardService.claim_free_bottle`) — the same
  rule, the same code;
* it never counts as a purchase: the purchase counter, milestone spins and
  referral qualification are untouched;
* it never sends anything. No customer message, no manager alert. The customer
  sees the new balance on their card; the operator sees the result on screen;
  the audit row is the record.

Every refusal is decided before the first write and is a :class:`LoyaltyError`,
so a handler can answer it and let the middleware commit nothing. Nothing here
commits.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.enums import AdminAccessKind
from app.models.loyalty import LoyaltyTransaction
from app.models.loyalty_adjustment import LoyaltyStampAdjustment
from app.repositories.loyalty_stamp_adjustment import LoyaltyStampAdjustmentRepository
from app.repositories.loyalty_transaction import LoyaltyTransactionRepository
from app.repositories.user import UserRepository
from app.services.loyalty import NOTE_MAX_LENGTH, LoyaltyError, LoyaltyService
from app.services.stamp_card import StampCard, StampCardPolicy, StampCardService

if TYPE_CHECKING:
    from app.security.admin import AdminGrant

# The note the admin panel puts on the ledger row; the author row carries the rest.
PANEL_CREDIT_NOTE = "manual_credit:admin_panel"


@dataclass(frozen=True, slots=True)
class StampAdjustmentPolicy:
    """The most stamps one manual credit may add: ``LOYALTY_ADMIN_MAX_STAMP_ADJUSTMENT``."""

    max_stamps: int

    def __post_init__(self) -> None:
        if isinstance(self.max_stamps, bool) or not isinstance(self.max_stamps, int):
            raise TypeError("max_stamps must be an integer")
        if self.max_stamps < 1:
            raise ValueError("max_stamps must be at least 1")

    @classmethod
    def from_settings(cls, settings: Settings) -> StampAdjustmentPolicy:
        return cls(max_stamps=settings.loyalty_admin_max_stamp_adjustment)

    @classmethod
    def defaults(cls) -> StampAdjustmentPolicy:
        return cls(max_stamps=Settings.model_fields["loyalty_admin_max_stamp_adjustment"].default)


class AdjustmentError(LoyaltyError):
    """A manual credit refused before anything was written."""


class InvalidAdjustmentError(AdjustmentError):
    """The amount, reason, operation id or actor does not fit."""


class SelfAdjustmentError(AdjustmentError):
    """An operator may not credit their own account."""


class UnknownCustomerError(AdjustmentError):
    """The target is not a registered customer."""


@dataclass(frozen=True, slots=True)
class AdjustmentActor:
    """The operator: a registered user, and the authority they act under."""

    user_id: int
    kind: AdminAccessKind
    access_session_id: int | None = None

    def __post_init__(self) -> None:
        if (self.kind == AdminAccessKind.BREAK_GLASS) != (self.access_session_id is not None):
            raise InvalidAdjustmentError(
                "a break-glass actor names their session; a configured admin has none"
            )

    @classmethod
    def from_grant(cls, user_id: int, grant: AdminGrant) -> AdjustmentActor:
        """The actor a handler builds from its ``admin_grant`` and the operator's user row."""
        return cls(user_id=user_id, kind=grant.kind, access_session_id=grant.session_id)


@dataclass(frozen=True, slots=True)
class StampAdjustment:
    """What one credit request came to, and the card as it stands afterwards."""

    adjustment: LoyaltyStampAdjustment
    transaction: LoyaltyTransaction
    card: StampCard
    created: bool  # False: this operation id was booked before; nothing was added now

    @property
    def stamps(self) -> int:
        return self.transaction.amount


def canonical_operation_id(value: object) -> str:
    """A UUID in its canonical text form; anything else is refused."""
    if isinstance(value, uuid.UUID):
        return str(value)
    if not isinstance(value, str):
        raise InvalidAdjustmentError("operation id must be a UUID")
    try:
        return str(uuid.UUID(value.strip()))
    except ValueError as exc:
        raise InvalidAdjustmentError("operation id must be a UUID") from exc


class AdminLoyaltyService:
    def __init__(
        self,
        session: AsyncSession,
        policy: StampAdjustmentPolicy | None = None,
        *,
        stamp_policy: StampCardPolicy | None = None,
    ) -> None:
        self.session = session
        self.policy = policy or StampAdjustmentPolicy.defaults()
        self.users = UserRepository(session)
        self.loyalty = LoyaltyService(session)
        self.transactions = LoyaltyTransactionRepository(session)
        self.adjustments = LoyaltyStampAdjustmentRepository(session)
        self.stamp_card = StampCardService(session, stamp_policy)

    # --- reading ------------------------------------------------------------------

    async def card(self, user_id: int) -> StampCard:
        """The customer's card as it stands: what a confirmation screen shows. Read-only."""
        return await self.stamp_card.card(user_id)

    async def history(
        self, user_id: int, *, limit: int | None = 20
    ) -> list[tuple[LoyaltyStampAdjustment, LoyaltyTransaction]]:
        """The customer's manual credits, newest first, each with its ledger row."""
        return await self.adjustments.list_for_user(user_id, limit=limit)

    async def find(self, operation_id: str) -> LoyaltyStampAdjustment | None:
        return await self.adjustments.get_by_operation_id(canonical_operation_id(operation_id))

    # --- the credit ---------------------------------------------------------------

    async def credit_stamps(
        self,
        *,
        target_user_id: int,
        amount: int,
        reason: str,
        actor: AdjustmentActor,
        operation_id: str,
    ) -> StampAdjustment:
        """
        Add ``amount`` stamps to ``target_user_id``'s card, once per ``operation_id``.

        Refuses, before any write: an amount that is not a whole number from 1 to
        the configured maximum, a blank or overlong reason, an operation id that
        is not a UUID, an operator crediting themselves, an unregistered
        customer — all :class:`AdjustmentError`. An unregistered *operator* is a
        caller bug (:class:`LookupError`): handlers register their sender first.

        Under the customer's account lock the operation id is looked up: a known
        one returns the adjustment already booked (``created=False``) and adds
        nothing; a new one writes the ledger row and its audit row in one flush.
        The unique constraints on the operation id and the ledger row back that
        up for anything the lock does not cover.
        """
        stamps = self._checked_amount(amount)
        note = self._checked_reason(reason)
        operation = canonical_operation_id(operation_id)
        if actor.user_id == target_user_id:
            raise SelfAdjustmentError("an operator cannot credit their own account")
        if await self.users.get_by_id(target_user_id) is None:
            raise UnknownCustomerError(f"User {target_user_id} is not a registered customer")
        if await self.users.get_by_id(actor.user_id) is None:
            raise LookupError(f"Actor {actor.user_id} does not exist")

        await self.loyalty.lock_account(target_user_id)

        existing = await self.adjustments.get_by_operation_id(operation)
        if existing is not None:
            transaction = await self.transactions.get_by_id(existing.transaction_id)
            if (
                transaction is None
                or existing.user_id != target_user_id
                or transaction.amount != stamps
            ):
                raise InvalidAdjustmentError(
                    "this operation id was already used for a different adjustment"
                )
            return StampAdjustment(
                adjustment=existing,
                transaction=transaction,
                card=await self.stamp_card.card(target_user_id),
                created=False,
            )

        transaction = await self.loyalty.adjust(target_user_id, amount=stamps, note=note)
        adjustment = await self.adjustments.record(
            transaction_id=transaction.id,
            user_id=target_user_id,
            actor_user_id=actor.user_id,
            actor_kind=actor.kind,
            access_session_id=actor.access_session_id,
            operation_id=operation,
        )
        return StampAdjustment(
            adjustment=adjustment,
            transaction=transaction,
            card=await self.stamp_card.card(target_user_id),
            created=True,
        )

    # --- validation ----------------------------------------------------------------

    def _checked_amount(self, amount: object) -> int:
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise InvalidAdjustmentError("stamps must be a whole number")
        if not 1 <= amount <= self.policy.max_stamps:
            raise InvalidAdjustmentError(
                f"stamps must be between 1 and {self.policy.max_stamps}, not {amount}"
            )
        return amount

    @staticmethod
    def _checked_reason(reason: object) -> str:
        if not isinstance(reason, str):
            raise InvalidAdjustmentError("a reason is required")
        text = reason.strip()
        if not text:
            raise InvalidAdjustmentError("a reason is required")
        if len(text) > NOTE_MAX_LENGTH:
            raise InvalidAdjustmentError(f"the reason must be at most {NOTE_MAX_LENGTH} characters")
        return text
