"""
Hostile review of manual stamp credits — the attempts, as regression tests.

The wizard's own tests already cover a stranger, a copied button, a forged
operation id, a second tap, five at once and a restart. These go behind the
screens: the operator's stored target and amount tampered with directly, an
emergency session that expires or is revoked mid-wizard, a failure after the
ledger row is written, a duplicated amount message, digits from other scripts,
a hostile username, two operators on one customer at once. Every one of them
ends with the books balanced and nobody told.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from aiogram.fsm.storage.base import StorageKey
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.keyboards.admin_loyalty import (
    CALLBACK_LOYALTY_CANCEL,
    CALLBACK_LOYALTY_CONFIRM_PREFIX,
)
from app.models.admin_access import AdminAccessSession
from app.models.enums import AdminAccessMethod, LoyaltyTransactionType
from app.models.loyalty import LoyaltyTransaction
from app.models.loyalty_adjustment import LoyaltyStampAdjustment
from app.models.reward import UserReward
from app.models.user import User
from app.repositories.admin_access_session import AdminAccessSessionRepository
from app.repositories.loyalty_stamp_adjustment import LoyaltyStampAdjustmentRepository
from app.services.admin_access import AdminAccessService
from app.services.localization import LocalizationService
from app.services.loyalty import LoyaltyService
from app.verify_deployment import loyalty_health
from tests.factories import make_user
from tests.production_bot import ADMIN_ID, MANAGER_CHAT_ID, RunningBot, tree_settings
from tests.test_loyalty_journeys import no_errors, sessions  # noqa: F401  (fixture)

EN = LocalizationService("en")
CUSTOMER, HOLDER, STRANGER = 9_821, 9_822, 9_823
MAX = 5


def t(key: str, **kwargs: Any) -> str:
    return EN.t(key, **kwargs)


@pytest_asyncio.fixture
async def bot(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[RunningBot]:  # noqa: F811
    async with sessions() as session:
        await make_user(session, telegram_id=ADMIN_ID)
        customer = await make_user(session, telegram_id=CUSTOMER)
        customer.username = "target_user"
        holder = await make_user(session, telegram_id=HOLDER)
        await make_user(session, telegram_id=STRANGER)
        now = datetime.now(UTC)
        await AdminAccessSessionRepository(session).open(
            holder.id,
            auth_method=AdminAccessMethod.BREAK_GLASS,
            created_at=now,
            expires_at=now + timedelta(hours=1),
        )
        await session.commit()
    yield RunningBot(sessions, tree_settings(loyalty_admin_max_stamp_adjustment=MAX))


async def uid(bot: RunningBot, telegram_id: int) -> int:
    async with bot.sessions() as session:
        return int(
            await session.scalar(select(User.id).where(User.telegram_id == telegram_id)) or 0
        )


async def balance(bot: RunningBot, telegram_id: int) -> int:
    async with bot.sessions() as session:
        return await LoyaltyService(session).balance(await uid(bot, telegram_id))


async def books(bot: RunningBot) -> tuple[int, int, int]:
    """(adjustment ledger rows, author rows, rewards)."""
    async with bot.sessions() as session:

        async def count(model: type[Any], *where: Any) -> int:
            return int(
                await session.scalar(select(func.count()).select_from(model).where(*where)) or 0
            )

        return (
            await count(
                LoyaltyTransaction, LoyaltyTransaction.kind == LoyaltyTransactionType.ADJUSTMENT
            ),
            await count(LoyaltyStampAdjustment),
            await count(UserReward),
        )


async def to_confirmation(bot: RunningBot, operator: int, target: str, amount: str) -> str:
    await bot.send(operator, "/admin_adjust_stamps")
    await bot.send(operator, target)
    await bot.send(operator, amount)
    return bot.button(operator, CALLBACK_LOYALTY_CONFIRM_PREFIX)


async def tamper(bot: RunningBot, operator: int, **fields: Any) -> None:
    """Rewrite the operator's stored wizard data, as if the steps had said something else."""
    key = StorageKey(bot_id=bot.bot.id, chat_id=operator, user_id=operator)
    data = await bot.dispatcher.storage.get_data(key)
    data.update(fields)
    await bot.dispatcher.storage.set_data(key, data)


def only_the_operator_heard(bot: RunningBot, operator: int) -> bool:
    chats = {getattr(m, "chat_id", None) for m, _ in bot.telegram.calls}
    return chats <= {operator, None}


# ============================================ the steps can be bypassed; the service cannot


async def test_a_tampered_target_is_refused_at_the_tap(bot: RunningBot) -> None:
    data = await to_confirmation(bot, ADMIN_ID, str(CUSTOMER), "2")
    operator_id = await uid(bot, ADMIN_ID)

    await tamper(bot, ADMIN_ID, target_telegram_id=ADMIN_ID, target_user_id=operator_id)
    await bot.press(ADMIN_ID, data)
    assert bot.texts(ADMIN_ID)[-1] == t("admin.loyalty_refused")  # SelfAdjustmentError, server-side

    data = await to_confirmation(bot, ADMIN_ID, str(CUSTOMER), "2")
    await tamper(bot, ADMIN_ID, target_telegram_id=424_242, target_user_id=424_242)
    await bot.press(ADMIN_ID, data)
    assert bot.texts(ADMIN_ID)[-1] == t("admin.loyalty_refused")  # nobody has that id

    assert await books(bot) == (0, 0, 0)
    assert await balance(bot, ADMIN_ID) == 0 and await balance(bot, CUSTOMER) == 0
    assert only_the_operator_heard(bot, ADMIN_ID)


@pytest.mark.parametrize(
    "amount", [MAX + 1, 1_000_000, 0, -5, True], ids=["over", "huge", "zero", "negative", "bool"]
)
async def test_a_tampered_amount_is_refused_at_the_tap(bot: RunningBot, amount: Any) -> None:
    data = await to_confirmation(bot, ADMIN_ID, str(CUSTOMER), "2")

    await tamper(bot, ADMIN_ID, amount=amount)
    await bot.press(ADMIN_ID, data)

    assert bot.texts(ADMIN_ID)[-1] == t("admin.loyalty_refused") or bot.alerts(ADMIN_ID)[-1] == (
        t("error.invalid_callback"),
        True,
    )
    assert await books(bot) == (0, 0, 0)
    assert await balance(bot, CUSTOMER) == 0


async def test_a_tampered_amount_of_the_wrong_type_is_a_stale_tap(bot: RunningBot) -> None:
    data = await to_confirmation(bot, ADMIN_ID, str(CUSTOMER), "2")
    await tamper(bot, ADMIN_ID, amount="3")
    await bot.press(ADMIN_ID, data)
    assert bot.alerts(ADMIN_ID)[-1] == (t("error.invalid_callback"), True)
    assert await books(bot) == (0, 0, 0)


# ======================================================= the emergency session must still be live


async def test_an_emergency_session_that_expires_mid_wizard_stops_it(bot: RunningBot) -> None:
    data = await to_confirmation(bot, HOLDER, str(CUSTOMER), "2")
    async with bot.sessions() as session:
        await session.execute(
            update(AdminAccessSession).values(
                created_at=datetime.now(UTC) - timedelta(hours=3),
                expires_at=datetime.now(UTC) - timedelta(hours=2),
            )
        )
        await session.commit()

    calls = await bot.press(HOLDER, data)

    assert calls == []  # dropped by the admin router's gates: not even an answer
    assert await books(bot) == (0, 0, 0)
    assert await bot.send(HOLDER, "/admin_adjust_stamps") == []


async def test_an_emergency_session_revoked_mid_wizard_stops_it(bot: RunningBot) -> None:
    data = await to_confirmation(bot, HOLDER, str(CUSTOMER), "2")
    async with bot.sessions() as session:
        access = (await session.scalars(select(AdminAccessSession))).one()
        assert await AdminAccessService(session).revoke(access)
        await session.commit()

    assert await bot.press(HOLDER, data) == []
    assert await books(bot) == (0, 0, 0)


# ======================================================= atomicity and retry


async def test_a_failure_after_the_ledger_row_rolls_everything_back_and_the_retry_books_once(
    bot: RunningBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = await to_confirmation(bot, ADMIN_ID, str(CUSTOMER), "3")
    original = LoyaltyStampAdjustmentRepository.record
    failures = {"left": 1}

    async def flaky(self: LoyaltyStampAdjustmentRepository, **fields: Any) -> Any:
        if failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("the process died between the two rows")
        return await original(self, **fields)

    monkeypatch.setattr(LoyaltyStampAdjustmentRepository, "record", flaky)

    await bot.press(ADMIN_ID, data)  # the ledger row was written, then the author row failed

    assert await books(bot) == (0, 0, 0)  # nothing partial survived the rollback
    assert await balance(bot, CUSTOMER) == 0
    async with bot.sessions() as session:
        assert await LoyaltyService(session).ledger_balance(await uid(bot, CUSTOMER)) == 0

    await bot.press(ADMIN_ID, data)  # confirm_once let the operator retry after the failure

    assert await books(bot) == (1, 1, 0)
    assert await balance(bot, CUSTOMER) == 3
    assert bot.texts(ADMIN_ID)[-1] == t(
        "admin.loyalty_done", amount=3, username="@target_user", telegram_id=CUSTOMER, balance=3
    )
    assert only_the_operator_heard(bot, ADMIN_ID)


# ======================================================= duplicated and concurrent updates


async def test_a_duplicated_amount_message_leads_to_one_credit(bot: RunningBot) -> None:
    await bot.send(ADMIN_ID, "/admin_adjust_stamps")
    await bot.send(ADMIN_ID, str(CUSTOMER))
    amount = bot.message(ADMIN_ID, "2")

    await asyncio.gather(bot.feed(amount), bot.feed(amount))  # delivered twice, at once

    buttons = {
        b.callback_data
        for m in bot.screens(ADMIN_ID).values()
        if m.reply_markup
        for row in m.reply_markup.inline_keyboard
        for b in row
        if (b.callback_data or "").startswith(CALLBACK_LOYALTY_CONFIRM_PREFIX)
    }
    assert len(buttons) == 2  # two screens were drawn; only the later one is live
    live = bot.button(ADMIN_ID, CALLBACK_LOYALTY_CONFIRM_PREFIX)
    await bot.press(ADMIN_ID, live)
    assert (await books(bot))[:2] == (1, 1)
    (other,) = buttons - {live}
    await bot.press(ADMIN_ID, other, on=bot.showing(ADMIN_ID, other))
    assert bot.alerts(ADMIN_ID)[-1] == (t("error.invalid_callback"), True)
    assert (await books(bot))[:2] == (1, 1)
    assert await balance(bot, CUSTOMER) == 2


async def test_two_operators_on_one_customer_both_book_and_the_ledger_balances(
    bot: RunningBot,
) -> None:
    """
    Two wizards open on one customer, confirmed one after the other. The same two
    taps *at once* are raced on PostgreSQL in ``tests/test_loyalty_journeys_postgres.py``;
    SQLite's single shared connection cannot hold two transactions at a time.
    """
    first = await to_confirmation(bot, ADMIN_ID, str(CUSTOMER), "2")
    second = await to_confirmation(bot, HOLDER, str(CUSTOMER), "3")

    await bot.press(ADMIN_ID, first)
    await bot.press(HOLDER, second)

    assert await balance(bot, CUSTOMER) == 5
    async with bot.sessions() as session:
        rows = await LoyaltyService(session).history(await uid(bot, CUSTOMER))
        assert sorted(row.amount for row in rows) == [2, 3]
        assert max(row.balance_after for row in rows) == 5
        health = await loyalty_health(session)
        assert set(health["integrity"].values()) == {0}, health["integrity"]
        assert health["audit"] == {"adjustments_without_their_author": 0}
    assert await books(bot) == (2, 2, 0)
    assert no_errors(bot, ADMIN_ID, HOLDER)


# ======================================================= input from other scripts


@pytest.mark.parametrize(
    "raw",
    ["١٠٠٠", "99999999999", "1e3", "+5", "0x10", "²", "1,5", "5.0", "٣"],
)
async def test_digits_of_any_script_never_exceed_the_limit(bot: RunningBot, raw: str) -> None:
    await bot.send(ADMIN_ID, "/admin_adjust_stamps")
    await bot.send(ADMIN_ID, str(CUSTOMER))

    await bot.send(ADMIN_ID, raw)

    last = bot.texts(ADMIN_ID)[-1]
    if last == t("admin.loyalty_amount_invalid", max=MAX):
        return  # refused outright
    shown = re.search(r"<b>(\d+)</b>\?", last)
    assert shown is not None and 1 <= int(shown.group(1)) <= MAX, last  # an ordinary small number
    await bot.press(ADMIN_ID, CALLBACK_LOYALTY_CANCEL)
    assert await books(bot) == (0, 0, 0)


async def test_a_command_at_the_amount_step_is_not_an_amount(bot: RunningBot) -> None:
    await bot.send(ADMIN_ID, "/admin_adjust_stamps")
    await bot.send(ADMIN_ID, str(CUSTOMER))
    await bot.send(ADMIN_ID, "/admin")
    assert bot.texts(ADMIN_ID)[-1] == t("admin.loyalty_amount_invalid", max=MAX)
    assert await books(bot) == (0, 0, 0)


# ======================================================= what the screens show


async def test_a_hostile_username_is_escaped_on_every_screen(bot: RunningBot) -> None:
    async with bot.sessions() as session:
        await session.execute(
            update(User).where(User.telegram_id == CUSTOMER).values(username="<b>x</b>&y")
        )
        await session.commit()

    await to_confirmation(bot, ADMIN_ID, str(CUSTOMER), "1")
    await bot.press(ADMIN_ID, bot.button(ADMIN_ID, CALLBACK_LOYALTY_CONFIRM_PREFIX))

    shown = "\n".join(bot.texts(ADMIN_ID))
    assert "@&lt;b&gt;x&lt;/b&gt;&amp;y" in shown
    assert "<b>x</b>" not in shown
    assert no_errors(bot, ADMIN_ID)


def test_the_credit_buttons_carry_no_customer_and_no_amount() -> None:
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parent.parent / "app" / "keyboards" / "admin_loyalty.py"
    ).read_text(encoding="utf-8")
    constants = re.findall(r'^CALLBACK_LOYALTY_\w+ = "([^"]+)"$', source, re.M)
    assert constants == ["admin:loy:credit", "admin:loy:ok:", "admin:loy:cancel"]
    # The confirm builder takes the operation id and nothing about the credit itself.
    import inspect

    from app.keyboards.admin_loyalty import loyalty_confirm_keyboard

    assert list(inspect.signature(loyalty_confirm_keyboard).parameters) == ["i18n", "operation_id"]


# ======================================================= nobody else, ever


async def test_across_every_attempt_nobody_but_the_operators_heard_anything(
    bot: RunningBot,
) -> None:
    data = await to_confirmation(bot, ADMIN_ID, str(CUSTOMER), "2")
    screen = bot.showing(ADMIN_ID, data)
    await bot.press(STRANGER, data, on=screen)
    await bot.press(HOLDER, data, on=screen)
    await bot.press(ADMIN_ID, data, on=screen)
    await bot.press(ADMIN_ID, data, on=screen)

    chats = {getattr(m, "chat_id", None) for m, _ in bot.telegram.calls}
    assert chats <= {ADMIN_ID, HOLDER, None}
    assert CUSTOMER not in chats and MANAGER_CHAT_ID not in chats
    assert await books(bot) == (1, 1, 0)
