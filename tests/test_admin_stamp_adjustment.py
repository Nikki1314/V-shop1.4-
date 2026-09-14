"""
Manual stamp credits — ``AdminLoyaltyService.credit_stamps``.

The stamps take the ledger's ordinary path (an ``adjustment`` row under the
account lock); what these tests pin is everything around it: only positive
whole numbers up to the configured maximum, a reason, a UUID operation id that
makes a repeat the same credit, an author row for every credit, the customer's
card afterwards, no reward issued and no purchase counted, nothing sent, and
the schema and health check that keep the record honest.
"""

from __future__ import annotations

import ast
import pathlib
import re
import uuid
from datetime import timedelta
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import CheckConstraint, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.config import Settings
from app.models.admin_access import AdminAccessSession
from app.models.enums import AdminAccessKind, LoyaltyTransactionType, RewardStatus
from app.models.loyalty import LoyaltyAccount, LoyaltyTransaction
from app.models.loyalty_adjustment import LoyaltyStampAdjustment
from app.models.reward import UserReward
from app.security.admin import AdminGrant
from app.services.admin import AdminService
from app.services.admin.loyalty import (
    AdjustmentActor,
    AdjustmentError,
    AdminLoyaltyService,
    InvalidAdjustmentError,
    SelfAdjustmentError,
    StampAdjustmentPolicy,
    UnknownCustomerError,
    canonical_operation_id,
)
from app.services.admin_access import AdminAccessService
from app.services.loyalty import LoyaltyError, LoyaltyService
from app.services.stamp_card import StampCardService
from app.verify_deployment import loyalty_health
from tests.factories import make_user
from tests.production_bot import tree_settings
from tests.test_loyalty_scenarios import first_loyalty_write, locks_and_writes

ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVICE = ROOT / "app" / "services" / "admin" / "loyalty.py"
MIGRATION = ROOT / "alembic" / "versions" / "f3c7a1d9e2b5_loyalty_stamp_adjustments.py"
CUSTOMER, OPERATOR, HOLDER = 9601, 9602, 9603
CONFIGURED = AdminAccessKind.CONFIGURED
FRESH = object()  # "make a new operation id" — distinct from passing an empty one


def op() -> str:
    return str(uuid.uuid4())


async def people(session: AsyncSession) -> tuple[int, int]:
    customer = await make_user(session, telegram_id=CUSTOMER)
    operator = await make_user(session, telegram_id=OPERATOR)
    return customer.id, operator.id


async def credit(
    session: AsyncSession,
    target: int,
    actor_id: int,
    amount: int = 3,
    *,
    reason: str = "goodwill",
    operation_id: Any = FRESH,
    actor: AdjustmentActor | None = None,
    policy: StampAdjustmentPolicy | None = None,
) -> Any:
    return await AdminLoyaltyService(session, policy).credit_stamps(
        target_user_id=target,
        amount=amount,
        reason=reason,
        actor=actor or AdjustmentActor(actor_id, CONFIGURED),
        operation_id=op() if operation_id is FRESH else operation_id,
    )


async def rows(session: AsyncSession, model: type[Any], **where: Any) -> int:
    stmt = select(func.count()).select_from(model)
    for key, value in where.items():
        stmt = stmt.where(getattr(model, key) == value)
    return int(await session.scalar(stmt) or 0)


async def nothing_written(session: AsyncSession) -> bool:
    return (
        await rows(session, LoyaltyTransaction) == 0
        and await rows(session, LoyaltyStampAdjustment) == 0
    )


# ======================================================= the credit


async def test_a_credit_is_an_adjustment_row_with_its_author(session: AsyncSession) -> None:
    customer, operator = await people(session)
    operation = op()

    result = await credit(
        session, customer, operator, 3, reason="  late delivery  ", operation_id=operation
    )

    assert result.created and result.stamps == 3
    txn = result.transaction
    assert (txn.kind, txn.amount, txn.balance_after, txn.note) == (
        LoyaltyTransactionType.ADJUSTMENT,
        3,
        3,
        "late delivery",
    )
    assert (txn.order_id, txn.referral_id, txn.spin_id, txn.reward_id) == (None, None, None, None)
    author = result.adjustment
    assert author.transaction_id == txn.id and author.user_id == customer
    assert author.actor_user_id == operator and author.actor_kind == CONFIGURED
    assert author.access_session_id is None and author.operation_id == operation
    assert await LoyaltyService(session).balance(customer) == 3
    assert await LoyaltyService(session).ledger_balance(customer) == 3
    assert (result.card.stamps, result.card.can_claim) == (3, False)


async def test_credits_add_up_and_show_on_the_card(session: AsyncSession) -> None:
    customer, operator = await people(session)
    await credit(session, customer, operator, 4)
    result = await credit(session, customer, operator, 2)
    assert result.transaction.balance_after == 6
    assert result.card.stamps == 6 and result.card.remaining == 4
    card = await AdminLoyaltyService(session).card(customer)
    assert (card.stamps, card.filled) == (6, 6)
    assert [
        (a.transaction_id, t.amount)
        for a, t in await AdminLoyaltyService(session).history(customer)
    ] == [
        (result.transaction.id, 2),
        (result.transaction.id - 1, 4),
    ]


async def test_a_break_glass_operator_is_recorded_with_their_session(session: AsyncSession) -> None:
    customer, _ = await people(session)
    holder = await make_user(session, telegram_id=HOLDER)
    opened = await AdminAccessService(session).open_break_glass(
        holder.id, ttl=timedelta(minutes=30)
    )
    grant = AdminGrant(
        telegram_id=HOLDER, kind=AdminAccessKind.BREAK_GLASS, session_id=opened.session.id
    )

    result = await credit(
        session, customer, holder.id, actor=AdjustmentActor.from_grant(holder.id, grant)
    )

    assert result.adjustment.actor_kind == AdminAccessKind.BREAK_GLASS
    assert result.adjustment.access_session_id == opened.session.id
    assert await rows(session, AdminAccessSession) == 1


# ======================================================= crossing the threshold: the existing rules


async def test_crossing_the_threshold_unlocks_a_claim_and_issues_nothing_by_itself(
    session: AsyncSession,
) -> None:
    customer, operator = await people(session)
    await LoyaltyService(session).adjust(customer, amount=8, note="two purchases")

    result = await credit(session, customer, operator, 5)

    card = result.card
    assert (card.stamps, card.can_claim, card.free_bottles_unlocked, card.extra_stamps) == (
        13,
        True,
        1,
        3,
    )
    assert await rows(session, UserReward) == 0  # no reward is minted by a credit

    # The customer claims it exactly as after a purchase — the same reward path.
    reward = await StampCardService(session).claim_free_bottle(customer, card_version=card.version)
    assert reward.status == RewardStatus.AVAILABLE
    assert await LoyaltyService(session).balance(customer) == 3
    after = await StampCardService(session).card(customer)
    assert (after.stamps, after.free_bottles_waiting, after.can_claim) == (3, 1, False)


async def test_the_configured_card_size_decides_when_a_claim_opens(session: AsyncSession) -> None:
    customer, operator = await people(session)
    settings = tree_settings(loyalty_stamps_required=5)
    service = AdminService(session, settings=settings).loyalty_admin
    result = await service.credit_stamps(
        target_user_id=customer,
        amount=5,
        reason="promo",
        actor=AdjustmentActor(operator, CONFIGURED),
        operation_id=op(),
    )
    assert result.card.stamps_required == 5 and result.card.can_claim


async def test_a_credit_is_not_a_purchase(session: AsyncSession) -> None:
    customer, operator = await people(session)
    await credit(session, customer, operator, 10)
    account = await session.scalar(select(LoyaltyAccount).where(LoyaltyAccount.user_id == customer))
    assert account is not None and account.qualifying_purchase_count == 0
    assert await LoyaltyService(session).purchase_count(customer) == 0


# ======================================================= idempotency


async def test_the_same_operation_id_books_once(session: AsyncSession) -> None:
    customer, operator = await people(session)
    operation = op()
    first = await credit(session, customer, operator, 3, operation_id=operation)

    again = await credit(session, customer, operator, 3, operation_id=operation)

    assert (first.created, again.created) == (True, False)
    assert again.transaction.id == first.transaction.id
    assert again.adjustment.id == first.adjustment.id
    assert await LoyaltyService(session).balance(customer) == 3
    assert await rows(session, LoyaltyTransaction) == 1
    assert await rows(session, LoyaltyStampAdjustment) == 1
    assert again.card.stamps == 3


async def test_an_operation_id_cannot_be_reused_for_a_different_credit(
    session: AsyncSession,
) -> None:
    customer, operator = await people(session)
    other = await make_user(session, telegram_id=HOLDER)
    operation = op()
    await credit(session, customer, operator, 3, operation_id=operation)

    with pytest.raises(InvalidAdjustmentError):
        await credit(session, customer, operator, 4, operation_id=operation)  # another amount
    with pytest.raises(InvalidAdjustmentError):
        await credit(session, other.id, operator, 3, operation_id=operation)  # another customer

    assert await LoyaltyService(session).balance(customer) == 3
    assert await LoyaltyService(session).balance(other.id) == 0
    assert await rows(session, LoyaltyTransaction) == 1


@pytest.mark.parametrize(
    "value",
    ["", "not-a-uuid", "12345", "  ", uuid.uuid4().hex + "x", None, 42],
)
async def test_an_operation_id_must_be_a_uuid(session: AsyncSession, value: Any) -> None:
    customer, operator = await people(session)
    with pytest.raises(InvalidAdjustmentError):
        await credit(session, customer, operator, operation_id=value)
    assert await nothing_written(session)


def test_operation_ids_are_stored_canonically() -> None:
    raw = uuid.uuid4()
    assert canonical_operation_id(raw) == str(raw)
    assert canonical_operation_id(f"  {raw.hex.upper()}  ") == str(raw)
    assert canonical_operation_id("{" + str(raw) + "}") == str(raw)


# ======================================================= refusals, all before the first write


@pytest.mark.parametrize(
    "amount",
    [0, -1, -10, 11, 1000, True, 2.0, "3", None],
    ids=["zero", "minus one", "minus ten", "over max", "far over", "bool", "float", "text", "none"],
)
async def test_only_whole_numbers_from_one_to_the_maximum_are_credited(
    session: AsyncSession, amount: Any
) -> None:
    customer, operator = await people(session)
    with pytest.raises(InvalidAdjustmentError):
        await credit(session, customer, operator, amount)
    assert await nothing_written(session)


@pytest.mark.parametrize("amount", [1, 10])
async def test_the_bounds_themselves_are_credited(session: AsyncSession, amount: int) -> None:
    customer, operator = await people(session)
    assert (await credit(session, customer, operator, amount)).stamps == amount


async def test_the_maximum_is_the_configured_one(session: AsyncSession) -> None:
    customer, operator = await people(session)
    policy = StampAdjustmentPolicy.from_settings(
        tree_settings(loyalty_admin_max_stamp_adjustment=25)
    )
    assert (await credit(session, customer, operator, 25, policy=policy)).stamps == 25
    with pytest.raises(InvalidAdjustmentError):
        await credit(session, customer, operator, 26, policy=policy)
    assert StampAdjustmentPolicy.defaults().max_stamps == 10
    assert AdminService(session, settings=tree_settings()).loyalty_admin.policy.max_stamps == 10


@pytest.mark.parametrize("value", [0, -1, 101])
def test_an_out_of_range_maximum_stops_the_process(value: int) -> None:
    with pytest.raises(ValidationError):
        tree_settings(loyalty_admin_max_stamp_adjustment=value)
    with pytest.raises((TypeError, ValueError)):
        StampAdjustmentPolicy(max_stamps=0)
    assert Settings.model_fields["loyalty_admin_max_stamp_adjustment"].default == 10


@pytest.mark.parametrize(
    "reason",
    ["", "   ", "\n", "x" * 256, None],
    ids=["empty", "spaces", "newline", "too long", "none"],
)
async def test_a_reason_is_required_and_bounded(session: AsyncSession, reason: Any) -> None:
    customer, operator = await people(session)
    with pytest.raises(InvalidAdjustmentError):
        await credit(session, customer, operator, reason=reason)
    assert await nothing_written(session)
    assert (
        await credit(session, customer, operator, reason="x" * 255)
    ).transaction.note == "x" * 255


async def test_an_operator_cannot_credit_themselves(session: AsyncSession) -> None:
    customer, operator = await people(session)
    with pytest.raises(SelfAdjustmentError):
        await credit(session, operator, operator)
    assert await nothing_written(session)
    assert await LoyaltyService(session).balance(operator) == 0
    del customer


async def test_the_customer_must_be_registered_and_the_operator_is_the_callers_problem(
    session: AsyncSession,
) -> None:
    customer, operator = await people(session)
    with pytest.raises(UnknownCustomerError):
        await credit(session, 424242, operator)
    with pytest.raises(LookupError):
        await credit(session, customer, 424242)
    assert await nothing_written(session)


def test_an_actor_names_a_session_exactly_when_break_glass() -> None:
    with pytest.raises(InvalidAdjustmentError):
        AdjustmentActor(1, AdminAccessKind.BREAK_GLASS)
    with pytest.raises(InvalidAdjustmentError):
        AdjustmentActor(1, CONFIGURED, access_session_id=5)
    assert (
        AdjustmentActor.from_grant(1, AdminGrant(telegram_id=9, kind=CONFIGURED)).kind == CONFIGURED
    )


def test_every_refusal_is_a_loyalty_error_a_handler_can_answer() -> None:
    for error in (InvalidAdjustmentError, SelfAdjustmentError, UnknownCustomerError):
        assert issubclass(error, AdjustmentError) and issubclass(error, LoyaltyError)
    assert issubclass(LoyaltyError, ValueError)


# ======================================================= locks, transactions, silence


async def test_the_account_is_locked_before_the_first_write(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    customer, operator = await people(session)
    with locks_and_writes(engine, session) as log:
        await credit(session, customer, operator, 2)
    assert "lock loyalty_accounts" in log, log
    assert log.index("lock loyalty_accounts") < first_loyalty_write(log), log
    assert log.index("insert loyalty_transactions") < log.index("insert loyalty_stamp_adjustments")


async def test_a_replay_takes_the_lock_and_writes_nothing(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    customer, operator = await people(session)
    operation = op()
    await credit(session, customer, operator, 2, operation_id=operation)
    with locks_and_writes(engine, session) as log:
        await credit(session, customer, operator, 2, operation_id=operation)
    assert "lock loyalty_accounts" in log
    assert not [entry for entry in log if entry.startswith(("insert", "update", "delete"))], log


async def test_a_credit_leaves_the_books_consistent(session: AsyncSession) -> None:
    customer, operator = await people(session)
    await credit(session, customer, operator, 4)
    await credit(session, customer, operator, 6)
    health = await loyalty_health(session)
    assert set(health["integrity"].values()) == {0}, health["integrity"]
    assert health["audit"] == {"adjustments_without_their_author": 0}


async def test_the_health_check_names_an_adjustment_without_its_author(
    session: AsyncSession,
) -> None:
    customer, _ = await people(session)
    await LoyaltyService(session).adjust(customer, amount=2, note="behind the service's back")
    health = await loyalty_health(session)
    assert health["audit"]["adjustments_without_their_author"] == 1
    assert health["integrity"]["authors_disagreeing_with_their_adjustment"] == 0


def test_the_service_can_send_nothing() -> None:
    """No bot, no notification service, no Telegram type is even importable here."""
    source = SERVICE.read_text(encoding="utf-8")
    module = ast.parse(source)
    imported = {
        node.module or "" for node in ast.walk(module) if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(module)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not any("aiogram" in name or "notification" in name for name in imported), imported
    assert "Bot" not in source and "send_message" not in source


def test_the_ledger_write_is_the_existing_one() -> None:
    """The credit goes through LoyaltyService.adjust — never a balance UPDATE of its own."""
    source = SERVICE.read_text(encoding="utf-8")
    assert source.count(".adjust(") == 1
    assert "stamp_balance" not in source and "update(" not in source


# ======================================================= what the schema itself refuses


async def _author_row(session: AsyncSession, **overrides: Any) -> LoyaltyStampAdjustment:
    customer, operator = await people(session)
    txn = await LoyaltyService(session).adjust(customer, amount=1, note="seed")
    fields: dict[str, Any] = {
        "transaction_id": txn.id,
        "user_id": customer,
        "actor_user_id": operator,
        "actor_kind": CONFIGURED,
        "access_session_id": None,
        "operation_id": op(),
    }
    return LoyaltyStampAdjustment(**(fields | overrides))


async def _rejected(session: AsyncSession, row: LoyaltyStampAdjustment) -> bool:
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        return True
    return False


async def test_the_database_refuses_an_author_who_is_the_customer(session: AsyncSession) -> None:
    row = await _author_row(session)
    self_row = LoyaltyStampAdjustment(
        transaction_id=row.transaction_id,
        user_id=row.user_id,
        actor_user_id=row.user_id,
        actor_kind=CONFIGURED,
        access_session_id=None,
        operation_id=op(),
    )
    assert await _rejected(session, self_row)


async def test_the_database_refuses_a_session_that_does_not_match_the_actor_kind(
    session: AsyncSession,
) -> None:
    assert await _rejected(
        session, await _author_row(session, actor_kind=AdminAccessKind.BREAK_GLASS)
    )
    holder = await make_user(session, telegram_id=HOLDER)
    opened = await AdminAccessService(session).open_break_glass(holder.id, ttl=timedelta(minutes=5))
    customer = (await session.scalar(select(LoyaltyAccount))).user_id  # type: ignore[union-attr]
    txn = await LoyaltyService(session).adjust(customer, amount=1, note="seed 2")
    configured_with_session = LoyaltyStampAdjustment(
        transaction_id=txn.id,
        user_id=customer,
        actor_user_id=holder.id,
        actor_kind=CONFIGURED,
        access_session_id=opened.session.id,
        operation_id=op(),
    )
    assert await _rejected(session, configured_with_session)


async def test_the_database_refuses_two_authors_for_one_row_or_one_operation(
    session: AsyncSession,
) -> None:
    first = await _author_row(session)
    session.add(first)
    await session.flush()
    twin = LoyaltyStampAdjustment(
        transaction_id=first.transaction_id,
        user_id=first.user_id,
        actor_user_id=first.actor_user_id,
        actor_kind=CONFIGURED,
        access_session_id=None,
        operation_id=op(),
    )
    assert await _rejected(session, twin)
    txn = await LoyaltyService(session).adjust(first.user_id, amount=1, note="seed 3")
    same_operation = LoyaltyStampAdjustment(
        transaction_id=txn.id,
        user_id=first.user_id,
        actor_user_id=first.actor_user_id,
        actor_kind=CONFIGURED,
        access_session_id=None,
        operation_id=first.operation_id,
    )
    assert await _rejected(session, same_operation)


async def test_an_author_row_never_changes(session: AsyncSession) -> None:
    row = await _author_row(session)
    session.add(row)
    await session.flush()
    with pytest.raises(ValueError, match="never changes"):
        row.actor_user_id = row.actor_user_id + 1
    with pytest.raises(ValueError, match="never changes"):
        row.operation_id = op()


def test_the_migration_creates_exactly_the_models_constraints() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    table = LoyaltyStampAdjustment.__table__
    for constraint in table.constraints:
        if isinstance(constraint, CheckConstraint):
            assert str(constraint.sqltext) in source, constraint.name
            assert constraint.name in source
    for name in (
        "uq_loyalty_stamp_adjustments_transaction_id",
        "uq_loyalty_stamp_adjustments_operation_id",
        "ix_loyalty_stamp_adjustments_user_id_id",
        "ix_loyalty_stamp_adjustments_actor_user_id_id",
    ):
        assert name in source
    assert 'down_revision: str | None = "e8b2c4d6f1a3"' in source
    for value in AdminAccessKind:
        assert f'"{value.value}"' in source
    downgrade = next(
        n
        for n in ast.parse(source).body
        if isinstance(n, ast.FunctionDef) and n.name == "downgrade"
    )
    first = downgrade.body[0]
    assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)
    assert isinstance(first.value.func, ast.Name)
    assert first.value.func.id == "_refuse_to_forget_who_credited_stamps"


def test_the_setting_and_the_service_are_documented() -> None:
    docs = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "docs").glob("*.md"))
    assert "`LOYALTY_ADMIN_MAX_STAMP_ADJUSTMENT`" in docs
    assert "`loyalty_stamp_adjustments`" in docs
    assert re.search(r"credit_stamps", docs)
