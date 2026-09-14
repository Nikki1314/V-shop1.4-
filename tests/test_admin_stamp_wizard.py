"""
🪪 Loyalty — crediting stamps from the admin panel, through the production bot.

The wizard asks who, shows the customer and their stamps, asks how many, shows
a confirmation and books on the tap. These tests drive it as an operator would,
in every language, and as an attacker would: a second tap, five at once, a
button from an earlier screen, a forged operation id, another operator's copied
button, a screen that outlived a restart, a stranger. Nothing but the one credit
is ever booked, and nobody but the operator ever hears about it.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from aiogram.types import Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.keyboards.admin import admin_menu_keyboard
from app.keyboards.admin_loyalty import (
    CALLBACK_LOYALTY_CANCEL,
    CALLBACK_LOYALTY_CONFIRM_PREFIX,
    CALLBACK_LOYALTY_CREDIT,
)
from app.models.enums import (
    AdminAccessKind,
    AdminAccessMethod,
    LanguageCode,
    LoyaltyTransactionType,
)
from app.models.loyalty import LoyaltyTransaction
from app.models.loyalty_adjustment import LoyaltyStampAdjustment
from app.models.reward import UserReward
from app.repositories.admin_access_session import AdminAccessSessionRepository
from app.repositories.user import UserRepository
from app.services.admin.loyalty import AdminLoyaltyService, InvalidAdjustmentError
from app.services.localization import LocalizationService
from app.services.loyalty import LoyaltyService
from tests.factories import make_user
from tests.production_bot import ADMIN_ID, MANAGER_CHAT_ID, RunningBot, tree_settings
from tests.test_loyalty_journeys import no_errors, sessions  # noqa: F401  (fixture)

EN = LocalizationService("en")
CUSTOMER, HOLDER, STRANGER, TWIN = 9_811, 9_812, 9_813, 9_814
MAX = 5


def t(key: str, **kwargs: Any) -> str:
    return EN.t(key, **kwargs)


@pytest_asyncio.fixture
async def bot(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[RunningBot]:  # noqa: F811
    async with sessions() as session:
        await make_user(session, telegram_id=ADMIN_ID)
        customer = await make_user(session, telegram_id=CUSTOMER)
        customer.username = "Vape_Fan"
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


async def balance(bot: RunningBot, telegram_id: int) -> int:
    async with bot.sessions() as session:
        user = await UserRepository(session).get_by_telegram_id(telegram_id)
        assert user is not None
        return await LoyaltyService(session).balance(user.id)


async def credits(bot: RunningBot) -> list[tuple[int, str, int | None]]:
    """(amount, actor kind, session id) of every manual credit, oldest first."""
    async with bot.sessions() as session:
        rows = await session.execute(
            select(LoyaltyTransaction.amount, LoyaltyStampAdjustment)
            .join(
                LoyaltyStampAdjustment,
                LoyaltyStampAdjustment.transaction_id == LoyaltyTransaction.id,
            )
            .order_by(LoyaltyStampAdjustment.id)
        )
        return [(amount, a.actor_kind.value, a.access_session_id) for amount, a in rows.all()]


async def ledger_rows(bot: RunningBot) -> int:
    async with bot.sessions() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(LoyaltyTransaction)
                .where(LoyaltyTransaction.kind == LoyaltyTransactionType.ADJUSTMENT)
            )
            or 0
        )


async def to_confirmation(bot: RunningBot, operator: int, target: str, amount: str) -> Message:
    await bot.send(operator, "/admin_adjust_stamps")
    await bot.send(operator, target)
    await bot.send(operator, amount)
    return bot.showing(operator, CALLBACK_LOYALTY_CANCEL)


def confirm_data(bot: RunningBot, operator: int) -> str:
    return bot.button(operator, CALLBACK_LOYALTY_CONFIRM_PREFIX)


def nobody_else_heard(bot: RunningBot, *chats: int) -> bool:
    return all(bot.texts(chat) == [] for chat in chats)


# ======================================================= the flow


async def test_the_operator_credits_stamps_in_four_steps(bot: RunningBot) -> None:
    await bot.send(ADMIN_ID, "/admin_adjust_stamps")
    assert bot.texts(ADMIN_ID) == [t("admin.loyalty_ask_target")]

    await bot.send(ADMIN_ID, str(CUSTOMER))
    card = t(
        "admin.loyalty_target_card", username="@Vape_Fan", telegram_id=CUSTOMER, balance=0, max=MAX
    )
    assert bot.texts(ADMIN_ID)[-1] == card

    await bot.send(ADMIN_ID, "3")
    confirm = t(
        "admin.loyalty_confirm",
        amount=3,
        username="@Vape_Fan",
        telegram_id=CUSTOMER,
        balance=0,
        after=3,
    )
    assert bot.texts(ADMIN_ID)[-1] == confirm
    data = confirm_data(bot, ADMIN_ID)
    assert re.fullmatch(re.escape(CALLBACK_LOYALTY_CONFIRM_PREFIX) + r"[0-9a-f-]{36}", data)
    assert str(CUSTOMER) not in data

    await bot.press(ADMIN_ID, data)

    done = t("admin.loyalty_done", amount=3, username="@Vape_Fan", telegram_id=CUSTOMER, balance=3)
    assert bot.texts(ADMIN_ID)[-1] == done
    last = [m for m, _ in bot.telegram.calls if getattr(m, "text", None) == done][0]
    assert last.reply_markup == admin_menu_keyboard(EN)
    assert await balance(bot, CUSTOMER) == 3
    assert await credits(bot) == [(3, AdminAccessKind.CONFIGURED.value, None)]
    assert nobody_else_heard(bot, CUSTOMER, MANAGER_CHAT_ID)
    assert no_errors(bot, ADMIN_ID)
    # The wizard is over: the menu works again.
    await bot.send(ADMIN_ID, t("admin.menu_products"))
    assert bot.texts(ADMIN_ID)[-2] == t("admin.section_products")


async def test_the_menu_button_leads_to_the_same_wizard(bot: RunningBot) -> None:
    await bot.send(ADMIN_ID, "/admin")
    await bot.send(ADMIN_ID, t("admin.menu_loyalty"))
    assert bot.texts(ADMIN_ID)[-1] == t("admin.section_loyalty")
    await bot.press(ADMIN_ID, CALLBACK_LOYALTY_CREDIT)
    assert bot.texts(ADMIN_ID)[-1] == t("admin.loyalty_ask_target")
    await bot.send(ADMIN_ID, "@vape_fan")
    await bot.send(ADMIN_ID, "1")
    await bot.press(ADMIN_ID, confirm_data(bot, ADMIN_ID))
    assert await balance(bot, CUSTOMER) == 1


async def test_a_break_glass_operator_is_recorded_with_their_session(bot: RunningBot) -> None:
    await to_confirmation(bot, HOLDER, str(CUSTOMER), "2")
    await bot.press(HOLDER, confirm_data(bot, HOLDER))

    assert await balance(bot, CUSTOMER) == 2
    ((amount, kind, session_id),) = await credits(bot)
    assert (amount, kind) == (2, AdminAccessKind.BREAK_GLASS.value) and session_id is not None
    assert no_errors(bot, HOLDER)


async def test_crossing_the_threshold_tells_the_operator_the_customer_can_claim(
    bot: RunningBot,
) -> None:
    async with bot.sessions() as session:
        user = await UserRepository(session).get_by_telegram_id(CUSTOMER)
        assert user is not None
        await LoyaltyService(session).adjust(user.id, amount=8, note="earlier")
        await session.commit()
    await to_confirmation(bot, ADMIN_ID, str(CUSTOMER), "5")
    await bot.press(ADMIN_ID, confirm_data(bot, ADMIN_ID))

    done = t("admin.loyalty_done", amount=5, username="@Vape_Fan", telegram_id=CUSTOMER, balance=13)
    assert bot.texts(ADMIN_ID)[-1] == done + "\n" + t("admin.loyalty_done_claim")
    async with bot.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(UserReward)) == 0


@pytest.mark.parametrize("language", list(LanguageCode))
async def test_every_text_is_in_the_operators_language(
    sessions: async_sessionmaker[AsyncSession],  # noqa: F811
    language: LanguageCode,
) -> None:
    async with sessions() as session:
        await make_user(session, telegram_id=ADMIN_ID, language=language)
        customer = await make_user(session, telegram_id=CUSTOMER)
        customer.username = None
        await session.commit()
    bot = RunningBot(sessions, tree_settings(loyalty_admin_max_stamp_adjustment=MAX))
    own = LocalizationService(language.value)
    no_name = own.t("admin.loyalty_no_username")

    await bot.send(ADMIN_ID, "/admin_adjust_stamps")
    await bot.send(ADMIN_ID, "nope!")
    await bot.send(ADMIN_ID, "@nobody_here")
    await bot.send(ADMIN_ID, str(CUSTOMER))
    await bot.send(ADMIN_ID, "0")
    await bot.send(ADMIN_ID, "2")
    await bot.press(ADMIN_ID, confirm_data(bot, ADMIN_ID))

    assert bot.texts(ADMIN_ID) == [
        own.t("admin.loyalty_ask_target"),
        own.t("admin.loyalty_target_invalid"),
        own.t("admin.loyalty_target_not_found"),
        own.t(
            "admin.loyalty_target_card", username=no_name, telegram_id=CUSTOMER, balance=0, max=MAX
        ),
        own.t("admin.loyalty_amount_invalid", max=MAX),
        own.t(
            "admin.loyalty_confirm",
            amount=2,
            username=no_name,
            telegram_id=CUSTOMER,
            balance=0,
            after=2,
        ),
        own.t("admin.loyalty_done", amount=2, username=no_name, telegram_id=CUSTOMER, balance=2),
    ]
    assert no_errors(bot, ADMIN_ID)


# ======================================================= refusals at each step


async def test_the_customer_step_refuses_and_stays(bot: RunningBot) -> None:
    async with bot.sessions() as session:
        twin = await make_user(session, telegram_id=TWIN)
        twin.username = "vape_fan"  # the handle changed hands; the old row is stale
        await session.commit()
    await bot.send(ADMIN_ID, "/admin_adjust_stamps")

    await bot.send(ADMIN_ID, "not an id")
    await bot.send(ADMIN_ID, "@ghost_user")
    await bot.send(ADMIN_ID, "@vape_fan")
    await bot.send(ADMIN_ID, str(ADMIN_ID))

    assert bot.texts(ADMIN_ID)[1:] == [
        t("admin.loyalty_target_invalid"),
        t("admin.loyalty_target_not_found"),
        t("admin.loyalty_target_ambiguous"),
        t("admin.loyalty_target_self"),
    ]
    await bot.send(ADMIN_ID, str(TWIN))  # still at the customer step: by id it works
    assert bot.texts(ADMIN_ID)[-1].startswith("👤")


@pytest.mark.parametrize(
    "amount", ["0", "-1", "abc", "1.5", "2 3", "", str(MAX + 1), "999999999999"]
)
async def test_the_amount_step_refuses_and_stays(bot: RunningBot, amount: str) -> None:
    await bot.send(ADMIN_ID, "/admin_adjust_stamps")
    await bot.send(ADMIN_ID, str(CUSTOMER))

    await bot.send(ADMIN_ID, amount or " ")

    assert bot.texts(ADMIN_ID)[-1] == t("admin.loyalty_amount_invalid", max=MAX)
    assert not bot.shows(ADMIN_ID, CALLBACK_LOYALTY_CANCEL)
    await bot.send(ADMIN_ID, str(MAX))  # the bound itself is fine
    assert bot.shows(ADMIN_ID, CALLBACK_LOYALTY_CANCEL)
    assert await ledger_rows(bot) == 0


async def test_cancel_works_at_every_step_and_credits_nothing(bot: RunningBot) -> None:
    cancel = t("common.cancel")
    await bot.send(ADMIN_ID, "/admin_adjust_stamps")
    await bot.send(ADMIN_ID, cancel)
    assert bot.texts(ADMIN_ID)[-1] == t("admin.loyalty_cancelled")

    await bot.send(ADMIN_ID, "/admin_adjust_stamps")
    await bot.send(ADMIN_ID, str(CUSTOMER))
    await bot.send(ADMIN_ID, cancel)
    assert bot.texts(ADMIN_ID)[-1] == t("admin.loyalty_cancelled")

    await to_confirmation(bot, ADMIN_ID, str(CUSTOMER), "2")
    await bot.press(ADMIN_ID, CALLBACK_LOYALTY_CANCEL)
    assert bot.texts(ADMIN_ID)[-1] == t("admin.loyalty_cancelled")
    assert not bot.shows(ADMIN_ID, CALLBACK_LOYALTY_CANCEL)  # the keyboard was taken away

    assert await ledger_rows(bot) == 0
    await bot.send(ADMIN_ID, "2")  # no wizard is open: a number is just a message
    assert await ledger_rows(bot) == 0


async def test_menu_taps_mid_wizard_are_held_off(bot: RunningBot) -> None:
    await bot.send(ADMIN_ID, "/admin_adjust_stamps")
    await bot.send(ADMIN_ID, t("admin.menu_orders"))
    assert bot.texts(ADMIN_ID)[-1] == t("admin.wizard_in_progress")
    await bot.send(ADMIN_ID, t("admin.menu_loyalty"))
    assert bot.texts(ADMIN_ID)[-1] == t("admin.wizard_in_progress")


async def test_a_message_at_the_confirmation_step_points_at_the_buttons(bot: RunningBot) -> None:
    await to_confirmation(bot, ADMIN_ID, str(CUSTOMER), "2")
    await bot.send(ADMIN_ID, "yes please")
    assert bot.texts(ADMIN_ID)[-1] == t("admin.loyalty_confirm_waiting")
    assert await ledger_rows(bot) == 0


# ======================================================= the tap, attacked


async def test_a_second_tap_credits_nothing_more(bot: RunningBot) -> None:
    screen = await to_confirmation(bot, ADMIN_ID, str(CUSTOMER), "2")
    data = confirm_data(bot, ADMIN_ID)
    await bot.press(ADMIN_ID, data, on=screen)

    await bot.press(ADMIN_ID, data, on=screen)

    assert await balance(bot, CUSTOMER) == 2 and await ledger_rows(bot) == 1
    assert bot.alerts(ADMIN_ID)[-1] == (t("error.invalid_callback"), True)


async def test_five_taps_at_once_credit_once(bot: RunningBot) -> None:
    screen = await to_confirmation(bot, ADMIN_ID, str(CUSTOMER), "2")
    data = confirm_data(bot, ADMIN_ID)

    await asyncio.gather(*(bot.feed(bot.tap(ADMIN_ID, data, on=screen)) for _ in range(5)))

    assert await balance(bot, CUSTOMER) == 2 and await ledger_rows(bot) == 1
    assert (
        bot.texts(ADMIN_ID).count(
            t("admin.loyalty_done", amount=2, username="@Vape_Fan", telegram_id=CUSTOMER, balance=2)
        )
        == 1
    )
    assert no_errors(bot, ADMIN_ID)


async def test_an_earlier_screens_button_is_stale(bot: RunningBot) -> None:
    """Going back and choosing another amount invalidates the first confirmation."""
    first_screen = await to_confirmation(bot, ADMIN_ID, str(CUSTOMER), "1")
    first = confirm_data(bot, ADMIN_ID)
    second_screen = await to_confirmation(bot, ADMIN_ID, str(CUSTOMER), "4")
    second = confirm_data(bot, ADMIN_ID)
    assert first != second

    await bot.press(ADMIN_ID, first, on=first_screen)
    assert bot.alerts(ADMIN_ID)[-1] == (t("error.invalid_callback"), True)
    assert await ledger_rows(bot) == 0

    # And the wizard was ended by the stale tap: the second button is now stale too.
    await bot.press(ADMIN_ID, second, on=second_screen)
    assert await ledger_rows(bot) == 0


async def test_a_forged_operation_id_credits_nothing(bot: RunningBot) -> None:
    screen = await to_confirmation(bot, ADMIN_ID, str(CUSTOMER), "2")
    forged = f"{CALLBACK_LOYALTY_CONFIRM_PREFIX}{uuid.uuid4()}"

    await bot.press(ADMIN_ID, forged, on=screen)

    assert bot.alerts(ADMIN_ID)[-1] == (t("error.invalid_callback"), True)
    assert await ledger_rows(bot) == 0


async def test_the_button_names_neither_the_customer_nor_the_amount(bot: RunningBot) -> None:
    """Whatever a client edits in the callback, the customer and amount come from the FSM."""
    await to_confirmation(bot, ADMIN_ID, str(CUSTOMER), "2")
    data = confirm_data(bot, ADMIN_ID)
    payload = data.removeprefix(CALLBACK_LOYALTY_CONFIRM_PREFIX)
    assert str(uuid.UUID(payload)) == payload
    assert str(CUSTOMER) not in data and str(STRANGER) not in data


async def test_another_operators_copied_button_credits_nothing(bot: RunningBot) -> None:
    screen = await to_confirmation(bot, ADMIN_ID, str(CUSTOMER), "2")
    data = confirm_data(bot, ADMIN_ID)

    await bot.press(HOLDER, data, on=screen)  # a real admin, but not the one with the wizard

    assert bot.alerts(HOLDER)[-1] == (t("error.invalid_callback"), True)
    assert await ledger_rows(bot) == 0
    # The owner's own tap still works afterwards.
    await bot.press(ADMIN_ID, data, on=screen)
    assert await ledger_rows(bot) == 1


async def test_a_stranger_gets_nothing_at_all(bot: RunningBot) -> None:
    screen = await to_confirmation(bot, ADMIN_ID, str(CUSTOMER), "2")
    data = confirm_data(bot, ADMIN_ID)

    assert await bot.send(STRANGER, "/admin_adjust_stamps") == []
    assert await bot.press(STRANGER, data, on=screen) == []
    assert await bot.press(STRANGER, CALLBACK_LOYALTY_CREDIT, on=screen) == []
    assert await ledger_rows(bot) == 0


async def test_a_restart_makes_the_screen_stale(bot: RunningBot) -> None:
    screen = await to_confirmation(bot, ADMIN_ID, str(CUSTOMER), "2")
    data = confirm_data(bot, ADMIN_ID)

    bot.restart()
    await bot.press(ADMIN_ID, data, on=screen)

    assert bot.alerts(ADMIN_ID)[-1] == (t("error.invalid_callback"), True)
    assert await ledger_rows(bot) == 0


async def test_a_refusal_at_the_tap_rolls_back_and_ends_the_wizard(
    bot: RunningBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    screen = await to_confirmation(bot, ADMIN_ID, str(CUSTOMER), "2")

    async def refuse(self: AdminLoyaltyService, **kwargs: Any) -> Any:
        raise InvalidAdjustmentError("changed underneath")

    monkeypatch.setattr(AdminLoyaltyService, "credit_stamps", refuse)
    await bot.press(ADMIN_ID, confirm_data(bot, ADMIN_ID), on=screen)

    assert bot.texts(ADMIN_ID)[-1] == t("admin.loyalty_refused")
    assert await ledger_rows(bot) == 0
    assert no_errors(bot, ADMIN_ID)
    monkeypatch.undo()
    await bot.send(ADMIN_ID, "2")  # the wizard is over
    assert await ledger_rows(bot) == 0


# ======================================================= silence


async def test_the_customer_and_the_manager_chat_hear_nothing(bot: RunningBot) -> None:
    await to_confirmation(bot, ADMIN_ID, "@Vape_Fan", str(MAX))
    await bot.press(ADMIN_ID, confirm_data(bot, ADMIN_ID))
    assert await balance(bot, CUSTOMER) == MAX
    chats = {getattr(m, "chat_id", None) for m, _ in bot.telegram.calls}
    assert chats <= {ADMIN_ID, None}


# ======================================================= one caller


def test_the_wizard_is_the_only_caller_of_the_credit() -> None:
    """The service books; exactly one screen asks it to — this one."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    callers = [
        path.relative_to(root).as_posix()
        for path in sorted((root / "app").rglob("*.py"))
        if ".credit_stamps(" in path.read_text(encoding="utf-8")
    ]
    assert callers == ["app/handlers/admin/loyalty.py"]
