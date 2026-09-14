"""
Both administrative features together, end to end, through the production bot.

An operator who is *not* in ``ADMIN_IDS`` breaks the glass, opens the existing
panel, credits a customer stamps, and later loses access when the session
expires — while a configured admin keeps working, the customer keeps shopping,
and nobody is told anything they should not be. The same journey is then put
through a process restart, a database reconnect, repeated commands, duplicated
callbacks and a burst of concurrent operations, and the shop's other paths —
catalog, cart, checkout, orders and their alerts, statistics, the stamp card,
the roulette, the referral link, four languages — are exercised around it.

The database here is a file, not memory: disposing the engine closes every
connection, which is what a database restart looks like from the bot's side.
"""

from __future__ import annotations

import asyncio
import pathlib
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.middlewares.database
from app import lifecycle
from app.database.base import Base
from app.handlers.user import roulette as roulette_screen
from app.keyboards.admin import admin_menu_keyboard
from app.keyboards.admin_loyalty import CALLBACK_LOYALTY_CANCEL, CALLBACK_LOYALTY_CONFIRM_PREFIX
from app.keyboards.admin_statistics import CALLBACK_STATS_REFRESH
from app.keyboards.roulette import CALLBACK_ROULETTE_SPIN_PREFIX
from app.keyboards.stamp_card import CALLBACK_STAMP_OPEN
from app.models.admin_access import AdminAccessAttempt, AdminAccessSession
from app.models.enums import AdminAccessKind, LanguageCode, LoyaltyTransactionType, OrderStatus
from app.models.loyalty import LoyaltyTransaction
from app.models.loyalty_adjustment import LoyaltyStampAdjustment
from app.models.order import Order
from app.models.user import User
from app.services.localization import LocalizationService
from app.services.loyalty import LoyaltyService
from app.utils.cache import invalidate_categories_cache
from app.utils.passwords import MIN_LOG_N, hash_password
from app.verify_deployment import loyalty_health
from tests.factories import make_user
from tests.production_bot import ADMIN_ID, MANAGER_CHAT_ID, RunningBot, tree_settings
from tests.test_loyalty_journeys import admin_moves, buy, check_out, no_errors, open_shop
from tests.test_loyalty_journeys import to_confirmation as checkout_to_confirmation

EN, DE, RU = (LocalizationService(code) for code in ("en", "de", "ru"))
PASSWORD = "operator on call tonight"
HASH = hash_password(PASSWORD, log_n=MIN_LOG_N)
OPERATOR, CUSTOMER, REFERRER, STRANGER = 9_931, 9_932, 9_933, 9_934
TTL_MINUTES = 30
TO_COMPLETED = (OrderStatus.ACCEPTED, OrderStatus.SHIPPED, OrderStatus.COMPLETED)


def settings() -> Any:
    return tree_settings(
        emergency_admin_password_hash=HASH,
        emergency_admin_session_ttl_minutes=TTL_MINUTES,
        emergency_admin_max_failed_attempts=3,
        loyalty_admin_max_stamp_adjustment=10,
    )


class World:
    """A shop on a file-backed database, its bot, and the people in it."""

    def __init__(self, engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.engine = engine
        self.sessions = sessions
        self.bottle = 0
        self.bot: RunningBot

    async def reconnect(self) -> None:
        """Close every database connection — the database went away and came back."""
        await self.engine.dispose()


@pytest_asyncio.fixture
async def world(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[World]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'shop.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(app.middlewares.database, "get_session_factory", lambda: sessions)
    monkeypatch.setattr(lifecycle, "get_session_factory", lambda: sessions)
    monkeypatch.setattr(roulette_screen, "FRAME_DELAY", 0)
    invalidate_categories_cache()
    world = World(engine, sessions)
    world.bottle = await open_shop(sessions)
    async with sessions() as session:
        await make_user(session, telegram_id=ADMIN_ID)
        operator = await make_user(session, telegram_id=OPERATOR, language=LanguageCode.DE)
        operator.username = "night_operator"
        customer = await make_user(session, telegram_id=CUSTOMER, language=LanguageCode.RU)
        customer.username = "Loyal_Customer"
        await make_user(session, telegram_id=REFERRER)
        await make_user(session, telegram_id=STRANGER)
        await session.commit()
    cfg = settings()
    await lifecycle.activate_loyalty(cfg)  # welcome spins for everyone
    world.bot = RunningBot(sessions, cfg)
    try:
        yield world
    finally:
        invalidate_categories_cache()
        await engine.dispose()


# ======================================================= reading the database


async def user_id(world: World, telegram_id: int) -> int:
    async with world.sessions() as session:
        return int(
            await session.scalar(select(User.id).where(User.telegram_id == telegram_id)) or 0
        )


async def balance(world: World, telegram_id: int) -> int:
    async with world.sessions() as session:
        return await LoyaltyService(session).balance(await user_id(world, telegram_id))


async def ledger(world: World, telegram_id: int) -> list[tuple[str, int, int]]:
    """(kind, amount, balance_after), oldest first."""
    uid = await user_id(world, telegram_id)
    async with world.sessions() as session:
        rows = await session.scalars(
            select(LoyaltyTransaction)
            .where(LoyaltyTransaction.user_id == uid)
            .order_by(LoyaltyTransaction.id)
        )
        return [(row.kind.value, row.amount, row.balance_after) for row in rows]


async def authors(world: World) -> list[tuple[str, int | None]]:
    async with world.sessions() as session:
        rows = await session.scalars(
            select(LoyaltyStampAdjustment).order_by(LoyaltyStampAdjustment.id)
        )
        return [(row.actor_kind.value, row.access_session_id) for row in rows]


async def access_sessions(world: World, telegram_id: int) -> list[AdminAccessSession]:
    uid = await user_id(world, telegram_id)
    async with world.sessions() as session:
        return list(
            await session.scalars(
                select(AdminAccessSession)
                .where(AdminAccessSession.user_id == uid)
                .order_by(AdminAccessSession.id)
            )
        )


async def attempts(world: World, telegram_id: int) -> list[str]:
    uid = await user_id(world, telegram_id)
    async with world.sessions() as session:
        rows = await session.scalars(
            select(AdminAccessAttempt)
            .where(AdminAccessAttempt.user_id == uid)
            .order_by(AdminAccessAttempt.id)
        )
        return [row.outcome.value for row in rows]


async def expire_sessions_of(world: World, telegram_id: int) -> None:
    """Time passes: every session of this user is now past its expiry."""
    uid = await user_id(world, telegram_id)
    async with world.sessions() as session:
        await session.execute(
            update(AdminAccessSession)
            .where(AdminAccessSession.user_id == uid)
            .values(
                created_at=datetime.now(UTC) - timedelta(hours=2),
                expires_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        await session.commit()


async def books_balance(world: World) -> None:
    async with world.sessions() as session:
        health = await loyalty_health(session)
    assert set(health["integrity"].values()) == {0}, health["integrity"]
    assert health["audit"] == {"adjustments_without_their_author": 0}


# ======================================================= driving the bot


async def log_in(bot: RunningBot, telegram_id: int, password: str = PASSWORD) -> None:
    await bot.send(telegram_id, "/emergency_admin")
    await bot.send(telegram_id, password)


async def credit_to_confirmation(bot: RunningBot, operator: int, target: str, amount: int) -> str:
    await bot.send(operator, "/admin_adjust_stamps")
    await bot.send(operator, target)
    await bot.send(operator, str(amount))
    return bot.button(operator, CALLBACK_LOYALTY_CONFIRM_PREFIX)


async def credit(bot: RunningBot, operator: int, target: str, amount: int) -> None:
    await bot.press(operator, await credit_to_confirmation(bot, operator, target, amount))


def chats(bot: RunningBot, since: int = 0) -> set[int]:
    return {
        int(m.chat_id)
        for m, _ in bot.telegram.calls[since:]
        if getattr(m, "chat_id", None) is not None
    }


# ======================================================= 1–18: the complete journey


async def test_the_complete_journey(world: World) -> None:
    bot = world.bot

    # 1. Not a configured admin: /admin is refused like any customer's.
    assert OPERATOR not in bot.settings.admin_ids
    await bot.send(OPERATOR, "/admin")
    assert bot.texts(OPERATOR) == [DE.t("admin.access_denied")]

    # 2–4. Break the glass, in the operator's own language: one session, thirty minutes.
    await log_in(bot, OPERATOR)
    assert bot.texts(OPERATOR)[1:] == [
        DE.t("admin.emergency_ask_password"),
        DE.t("admin.emergency_granted", minutes=TTL_MINUTES),
        DE.t("admin.panel_ready"),
    ]
    (access,) = await access_sessions(world, OPERATOR)
    assert access.is_active(datetime.now(UTC))
    assert access.expires_at - access.created_at == timedelta(minutes=TTL_MINUTES)
    assert await attempts(world, OPERATOR) == ["succeeded"]
    assert bot.settings.admin_ids == [ADMIN_ID]  # still not a configured admin

    # 5. The existing panel, with the existing sections.
    await bot.send(OPERATOR, "/admin")
    panel = next(
        m for m, _ in bot.telegram.calls if getattr(m, "text", None) == DE.t("admin.panel_ready")
    )
    assert panel.reply_markup == admin_menu_keyboard(DE)
    await bot.send(OPERATOR, DE.t("admin.menu_statistics"))
    await bot.press(OPERATOR, CALLBACK_STATS_REFRESH)
    assert bot.alerts(OPERATOR)[-1] == (DE.t("admin.stats_refreshed"), False)

    # 6–8. The credit wizard: the customer by handle, their card with the balance.
    await bot.send(OPERATOR, "/admin_adjust_stamps")
    assert bot.texts(OPERATOR)[-1] == DE.t("admin.loyalty_ask_target")
    await bot.send(OPERATOR, "@loyal_customer")
    assert bot.texts(OPERATOR)[-1] == DE.t(
        "admin.loyalty_target_card",
        username="@Loyal_Customer",
        telegram_id=CUSTOMER,
        balance=0,
        max=10,
    )

    # 9–10. The amount, then nothing happens until the tap.
    await bot.send(OPERATOR, "4")
    assert bot.texts(OPERATOR)[-1] == DE.t(
        "admin.loyalty_confirm",
        amount=4,
        username="@Loyal_Customer",
        telegram_id=CUSTOMER,
        balance=0,
        after=4,
    )
    await bot.send(OPERATOR, "ja")
    assert bot.texts(OPERATOR)[-1] == DE.t("admin.loyalty_confirm_waiting")
    assert await ledger(world, CUSTOMER) == []
    calls_before_tap = len(bot.telegram.calls)

    # 11–12. One tap: one ledger row of kind adjustment, one author row naming the session.
    await bot.press(OPERATOR, bot.button(OPERATOR, CALLBACK_LOYALTY_CONFIRM_PREFIX))
    assert bot.texts(OPERATOR)[-1] == DE.t(
        "admin.loyalty_done", amount=4, username="@Loyal_Customer", telegram_id=CUSTOMER, balance=4
    )
    assert await ledger(world, CUSTOMER) == [(LoyaltyTransactionType.ADJUSTMENT.value, 4, 4)]
    assert await authors(world) == [(AdminAccessKind.BREAK_GLASS.value, access.id)]
    assert await balance(world, CUSTOMER) == 4
    await books_balance(world)

    # 14–15. Nobody was told: not the customer, not the manager chat, not the configured admin.
    assert chats(bot, calls_before_tap) == {OPERATOR}
    assert (
        bot.texts(CUSTOMER) == [] and bot.texts(MANAGER_CHAT_ID) == [] and bot.texts(ADMIN_ID) == []
    )

    # 13. The customer sees the stamps on their card, in Russian, when they look.
    await bot.send(CUSTOMER, RU.t("menu.stamp_card"))
    assert bot.shows(CUSTOMER, CALLBACK_STAMP_OPEN)
    card = bot.texts(CUSTOMER)[-1]
    assert "4/10" in card and card.startswith(RU.t("stamp_card.title"))
    assert len(bot.texts(CUSTOMER)) == 1

    # 16–17. The session expires: the panel, the wizard and its buttons are gone.
    await expire_sessions_of(world, OPERATOR)
    await bot.send(OPERATOR, "/admin")
    assert bot.texts(OPERATOR)[-1] == DE.t("admin.access_denied")
    assert await bot.send(OPERATOR, "/admin_adjust_stamps") == []
    assert await bot.send(OPERATOR, DE.t("admin.menu_statistics")) == []
    assert await bot.press(OPERATOR, CALLBACK_STATS_REFRESH) == []

    # 18. The configured admin never noticed: panel, wizard, credit.
    await bot.send(ADMIN_ID, "/admin")
    assert bot.texts(ADMIN_ID)[-1] == EN.t("admin.panel_ready")
    await credit(bot, ADMIN_ID, str(CUSTOMER), 1)
    assert await balance(world, CUSTOMER) == 5
    assert await authors(world) == [
        (AdminAccessKind.BREAK_GLASS.value, access.id),
        (AdminAccessKind.CONFIGURED.value, None),
    ]

    # And the operator can break the glass again: a new session, the old one on record.
    await log_in(bot, OPERATOR)
    assert bot.texts(OPERATOR)[-1] == DE.t("admin.panel_ready")
    sessions_now = await access_sessions(world, OPERATOR)
    assert [s.is_active(datetime.now(UTC)) for s in sessions_now] == [False, True]
    await books_balance(world)
    assert no_errors(bot, OPERATOR, CUSTOMER, ADMIN_ID)


# ======================================================= restarts and reconnects


async def test_a_process_restart_and_a_database_reconnect_lose_nothing_that_matters(
    world: World,
) -> None:
    bot = world.bot
    await log_in(bot, OPERATOR)
    await credit(bot, OPERATOR, str(CUSTOMER), 2)

    bot.restart()  # the process: FSM gone, database untouched
    await world.reconnect()  # the database: every connection closed

    await bot.send(OPERATOR, "/admin")
    assert bot.texts(OPERATOR)[-1] == DE.t("admin.panel_ready")  # the session is in the database
    await credit(bot, OPERATOR, "@loyal_customer", 3)
    assert await balance(world, CUSTOMER) == 5

    await world.reconnect()
    await bot.send(CUSTOMER, RU.t("menu.stamp_card"))
    assert "5/10" in bot.texts(CUSTOMER)[-1]

    # A stranger's lockout survives both as well.
    for guess in ("a", "b", "c"):
        await log_in(bot, STRANGER, guess)
    bot.restart()
    await world.reconnect()
    await log_in(bot, STRANGER, PASSWORD)
    assert bot.texts(STRANGER)[-1] == EN.t("admin.access_denied")
    assert await attempts(world, STRANGER) == ["failed", "failed", "failed", "locked_out"]
    assert await access_sessions(world, STRANGER) == []

    # A confirmation screen from before the restart is stale, not a second credit.
    data = await credit_to_confirmation(bot, OPERATOR, str(CUSTOMER), 1)
    screen = bot.showing(OPERATOR, data)
    bot.restart()
    await bot.press(OPERATOR, data, on=screen)
    assert bot.alerts(OPERATOR)[-1] == (DE.t("error.invalid_callback"), True)
    assert await balance(world, CUSTOMER) == 5
    await books_balance(world)
    assert no_errors(bot, OPERATOR, CUSTOMER)


# ======================================================= repetition and duplicates


async def test_repeated_commands_and_duplicated_updates_change_nothing_twice(world: World) -> None:
    bot = world.bot

    # /emergency_admin twice, then the password: one session.
    await bot.send(OPERATOR, "/emergency_admin")
    await bot.send(OPERATOR, "/emergency_admin")
    secret = bot.message(OPERATOR, PASSWORD)
    await bot.feed(secret)
    await bot.feed(secret)  # delivered twice
    assert len(await access_sessions(world, OPERATOR)) == 1
    assert await attempts(world, OPERATOR) == ["succeeded"]

    # Logging in again supersedes: still one active session, two on record.
    await log_in(bot, OPERATOR)
    rows = await access_sessions(world, OPERATOR)
    assert [s.is_active(datetime.now(UTC)) for s in rows] == [False, True]

    # /admin_adjust_stamps twice, the amount twice, the tap twice: one credit.
    await bot.send(OPERATOR, "/admin_adjust_stamps")
    await bot.send(OPERATOR, "/admin_adjust_stamps")
    await bot.send(OPERATOR, str(CUSTOMER))
    await bot.send(OPERATOR, "3")
    await bot.send(OPERATOR, "3")  # at the confirmation step: "use the buttons"
    tap = bot.tap(OPERATOR, bot.button(OPERATOR, CALLBACK_LOYALTY_CONFIRM_PREFIX))
    await bot.feed(tap)
    await bot.feed(tap)  # the same callback delivered twice
    assert await ledger(world, CUSTOMER) == [(LoyaltyTransactionType.ADJUSTMENT.value, 3, 3)]
    assert len(await authors(world)) == 1

    # Cancel at every step leaves no trace.
    await bot.send(OPERATOR, "/admin_adjust_stamps")
    await bot.send(OPERATOR, DE.t("common.cancel"))
    data = await credit_to_confirmation(bot, OPERATOR, str(CUSTOMER), 2)
    screen = bot.showing(OPERATOR, data)
    await bot.press(OPERATOR, CALLBACK_LOYALTY_CANCEL)
    await bot.press(OPERATOR, data, on=screen)  # the button is gone from the screen: a stale tap
    assert await balance(world, CUSTOMER) == 3
    await books_balance(world)
    assert no_errors(bot, OPERATOR)


# ============================================ concurrency across both features and the shop


async def test_a_burst_of_concurrent_operations_leaves_the_books_exact(
    world: World, lands: Callable[[str], None]
) -> None:
    bot = world.bot
    await log_in(bot, OPERATOR)
    confirm = await credit_to_confirmation(bot, OPERATOR, str(CUSTOMER), 4)
    confirm_screen = bot.showing(OPERATOR, confirm)
    await buy(bot, CUSTOMER, world.bottle)
    await checkout_to_confirmation(bot, CUSTOMER)
    checkout_screen = bot.showing(CUSTOMER, "checkout:confirm")
    await bot.send(ADMIN_ID, "/admin")
    await bot.send(ADMIN_ID, EN.t("admin.menu_statistics"))
    stats_screen = bot.showing(ADMIN_ID, CALLBACK_STATS_REFRESH)
    lands("stamp_1")
    await bot.send(REFERRER, EN.t("menu.roulette"))
    spin = bot.button(REFERRER, CALLBACK_ROULETTE_SPIN_PREFIX)
    spin_screen = bot.showing(REFERRER, spin)
    marker = len(bot.telegram.calls)

    await asyncio.gather(
        *(bot.feed(bot.tap(OPERATOR, confirm, on=confirm_screen)) for _ in range(3)),
        *(bot.feed(bot.tap(CUSTOMER, "checkout:confirm", on=checkout_screen)) for _ in range(2)),
        *(bot.feed(bot.tap(ADMIN_ID, CALLBACK_STATS_REFRESH, on=stats_screen)) for _ in range(2)),
        *(bot.feed(bot.tap(REFERRER, spin, on=spin_screen)) for _ in range(2)),
        bot.feed(bot.message(STRANGER, "/admin_adjust_stamps")),
        bot.feed(bot.message(STRANGER, "/admin")),
    )

    # One credit, one order, one alert, one spin — and every screen answered.
    assert await ledger(world, CUSTOMER) == [(LoyaltyTransactionType.ADJUSTMENT.value, 4, 4)]
    async with world.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Order)) == 1
    assert len(bot.texts(MANAGER_CHAT_ID)) == 1 and len(bot.texts(ADMIN_ID)) >= 1
    assert await ledger(world, REFERRER) == [(LoyaltyTransactionType.ROULETTE.value, 1, 1)]
    assert STRANGER not in chats(bot, marker) or bot.texts(STRANGER) == [
        EN.t("admin.access_denied")
    ]
    assert CUSTOMER in chats(bot, marker)  # their order confirmation
    await bot.send(CUSTOMER, RU.t("menu.stamp_card"))  # after the burst: the credit is on the card
    assert "4/10" in bot.texts(CUSTOMER)[-1]
    await books_balance(world)
    assert no_errors(bot, OPERATOR, CUSTOMER, ADMIN_ID, REFERRER)


# ======================================================= nothing else changed


async def test_the_shop_around_the_two_features_works_as_before(
    world: World, lands: Callable[[str], None]
) -> None:
    bot = world.bot
    await log_in(bot, OPERATOR)

    # The customer shops: catalog, cart, checkout — the manager and the admin are alerted once.
    await buy(bot, CUSTOMER, world.bottle)
    await check_out(bot, CUSTOMER)
    assert len(bot.texts(MANAGER_CHAT_ID)) == 1 and len(bot.texts(ADMIN_ID)) == 1
    async with world.sessions() as session:
        order = (await session.scalars(select(Order))).one()
    customer_heard = len(bot.texts(CUSTOMER))

    # The configured admin moves it to Completed: status messages in Russian, one purchase stamp.
    await admin_moves(bot, order.id, *TO_COMPLETED)
    assert bot.texts(CUSTOMER)[customer_heard:] == [
        RU.t("notification.status_accepted", order_id=order.id),
        RU.t("notification.status_shipped", order_id=order.id),
        RU.t("notification.status_completed", order_id=order.id),
    ]
    assert await ledger(world, CUSTOMER) == [(LoyaltyTransactionType.PURCHASE.value, 1, 1)]

    # The roulette pays out, the invite link opens, the card shows it all.
    lands("stamp_1")
    await bot.send(CUSTOMER, RU.t("menu.roulette"))
    await bot.press(CUSTOMER, bot.button(CUSTOMER, CALLBACK_ROULETTE_SPIN_PREFIX))
    assert (await ledger(world, CUSTOMER))[-1] == (LoyaltyTransactionType.ROULETTE.value, 1, 2)
    await bot.send(CUSTOMER, RU.t("menu.invite"))
    assert any(
        "t.me/VShopTestBot?start=ref_" in str(m.reply_markup)
        for m, _ in bot.telegram.calls
        if getattr(m, "chat_id", None) == CUSTOMER
    )

    # The break-glass operator credits two more; the ledger tells the whole story.
    await credit(bot, OPERATOR, str(CUSTOMER), 2)
    assert await ledger(world, CUSTOMER) == [
        (LoyaltyTransactionType.PURCHASE.value, 1, 1),
        (LoyaltyTransactionType.ROULETTE.value, 1, 2),
        (LoyaltyTransactionType.ADJUSTMENT.value, 2, 4),
    ]
    await bot.send(CUSTOMER, RU.t("menu.stamp_card"))
    assert "4/10" in bot.texts(CUSTOMER)[-1]

    # Statistics, in the operator's German, count the one completed order.
    await bot.send(OPERATOR, DE.t("admin.menu_statistics"))
    assert DE.t("admin.stats_title") in bot.texts(OPERATOR)[-1]

    # The manager chat heard about the order and nothing else.
    assert len(bot.texts(MANAGER_CHAT_ID)) == 1
    await books_balance(world)
    assert no_errors(bot, OPERATOR, CUSTOMER, ADMIN_ID)
