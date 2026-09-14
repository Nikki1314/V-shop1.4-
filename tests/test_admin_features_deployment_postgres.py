"""
Deploying the administrative features onto an existing shop — opt-in, PostgreSQL.

A shop that went live before the loyalty programme, launched it, and has been
trading since: catalog, customers, orders, stamps, a welcome spin. Then the
deploy that brings emergency access and manual stamp credits: the three
migrations, a start, the bot in use, a restart, a start with the feature
switched off, and a downgrade that must refuse. At every step the catalog, the
customers, the orders and the stamps are exactly what they were.

Runs only when ``VSHOP_TEST_POSTGRES_URL`` names an empty database whose name
ends in ``_test``; see ``tests/test_loyalty_activation_postgres.py``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.middlewares.database
from alembic import command
from app import lifecycle
from app.keyboards.admin_loyalty import CALLBACK_LOYALTY_CONFIRM_PREFIX
from app.keyboards.stamp_card import CALLBACK_STAMP_OPEN
from app.models.admin_access import AdminAccessSession
from app.models.enums import AdminAccessKind, LoyaltyTransactionType, OrderStatus
from app.models.loyalty import LoyaltyTransaction
from app.models.loyalty_adjustment import LoyaltyStampAdjustment
from app.models.order import Order
from app.models.user import User
from app.services.admin import AdminService
from app.services.localization import LocalizationService
from app.services.loyalty import LoyaltyService
from app.services.loyalty_activation import ActivationReport
from app.utils.cache import invalidate_categories_cache
from app.utils.passwords import MIN_LOG_N, hash_password
from app.verify_deployment import loyalty_health
from tests.factories import make_order
from tests.production_bot import ADMIN_ID, MANAGER_CHAT_ID, RunningBot, tree_settings
from tests.test_loyalty_activation_postgres import (  # noqa: F401  (scratch is a fixture)
    BEFORE_LOYALTY,
    LEGACY_SHOP,
    _alembic_config,
    alembic,
    head,
    legacy_columns,
    pytestmark,
    scalar,
    scratch,
    snapshot,
)
from tests.test_loyalty_journeys import buy

EN, RU = LocalizationService("en"), LocalizationService("ru")
PASSWORD = "operator on call tonight"
HASH = hash_password(PASSWORD, log_n=MIN_LOG_N)
LOYALTY_RELEASE_HEAD = "c5d2e8f1a6b3"  # the head before the administrative features
ANNA, OPERATOR = 7_100_001, 7_100_042  # a legacy customer; an operator hired after launch
LOYALTY_TABLES = (
    "loyalty_accounts",
    "loyalty_transactions",
    "roulette_spin_grants",
    "roulette_spins",
    "user_rewards",
    "referrals",
)


def settings(hash_value: str | None = HASH):
    return tree_settings(emergency_admin_password_hash=hash_value)


async def counts(session: AsyncSession, tables: tuple[str, ...]) -> dict[str, int]:
    return {name: int(await scalar(session, f"SELECT count(*) FROM {name}")) for name in tables}


async def balances(session: AsyncSession) -> dict[int, int]:
    rows = await session.execute(text("SELECT user_id, stamp_balance FROM loyalty_accounts"))
    return {user_id: balance for user_id, balance in rows.all()}


async def start(cfg) -> tuple[ActivationReport | None, int | None]:
    """What ``on_startup`` does to the database: loyalty activation, session reconciliation."""
    return await lifecycle.activate_loyalty(cfg), await lifecycle.reconcile_emergency_sessions(cfg)


async def test_deploying_the_administrative_features_onto_a_trading_shop(
    scratch: async_sessionmaker[AsyncSession],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app.middlewares.database, "get_session_factory", lambda: scratch)
    invalidate_categories_cache()
    cfg = settings()

    # ---- 1–4. A shop that predates the loyalty programme, then launched it and traded on.
    await alembic("upgrade", BEFORE_LOYALTY)
    async with scratch() as session:
        for sql in LEGACY_SHOP:
            await session.execute(text(sql))
        await session.commit()
    await alembic("upgrade", LOYALTY_RELEASE_HEAD)  # the loyalty launch, as it happened
    async with scratch() as session:
        anna = (await session.scalars(select(User).where(User.telegram_id == ANNA))).one()
        order = await make_order(session, anna, status=OrderStatus.NEW)
        order.total_price = Decimal("40.00")
        order.loyalty_eligible = True
        await session.flush()
        admin = AdminService(session, settings=cfg)
        for status in (OrderStatus.ACCEPTED, OrderStatus.SHIPPED, OrderStatus.COMPLETED):
            order = await admin.set_order_status(order, status)
        await session.commit()
        columns = await legacy_columns(session)
        shop_before = await snapshot(session, columns)
        # What trading must never touch: the catalog and the order history. Carts
        # and profiles change as customers use the bot, so they are checked by identity.
        untouchable = ("categories", "subcategories", "products", "orders", "order_items")
        catalog_and_orders = {name: dict(shop_before[name]) for name in untouchable}
        legacy_identities = {row_id: row[1] for row_id, row in shop_before["users"].items()}
        loyalty_before = await counts(session, LOYALTY_TABLES)
        balances_before = await balances(session)
        anna_id = anna.id
    assert balances_before[anna_id] == 2  # €40 at €20 a stamp
    assert loyalty_before["loyalty_transactions"] == 1
    assert len(shop_before["orders"]) == 5 and len(shop_before["products"]) == 1

    # ---- 5. The deploy: three additive migrations, then a redeploy with nothing to apply.
    await alembic("upgrade", "head")
    await alembic("upgrade", "head")
    await asyncio.to_thread(command.check, _alembic_config())
    async with scratch() as session:
        assert await scalar(session, "SELECT version_num FROM alembic_version") == head()
        assert await snapshot(session, columns) == shop_before
        assert await counts(session, LOYALTY_TABLES) == loyalty_before
        assert await balances(session) == balances_before
        new_tables = ("admin_access_sessions", "admin_access_attempts", "loyalty_stamp_adjustments")
        assert await counts(session, new_tables) == dict.fromkeys(new_tables, 0)

    # ---- 6. The bot starts: nothing to activate, nothing to revoke.
    assert await start(cfg) == (ActivationReport(0, 0), 0)

    # ---- 8–10. The catalog, the loyalty state and the admin panel are what they were.
    bot = RunningBot(scratch, cfg)
    async with scratch() as session:
        await session.execute(
            text(
                "INSERT INTO users (telegram_id, first_name, language, selected_city) "
                "VALUES (:admin, 'Admin', 'en', 'berlin'), (:operator, 'Op', 'en', 'berlin')"
            ),
            {"admin": ADMIN_ID, "operator": OPERATOR},
        )
        await session.commit()
    product_id = int(next(iter(shop_before["products"])))
    await buy(bot, ANNA, product_id)  # Catalog → Liquids → Brand → Mango → add to cart
    assert any("Mango" in t or "Манго" in t for t in bot.texts(ANNA))
    await bot.send(ANNA, RU.t("menu.stamp_card"))
    assert bot.shows(ANNA, CALLBACK_STAMP_OPEN) and "2/10" in bot.texts(ANNA)[-1]
    await bot.send(ADMIN_ID, "/admin")
    assert bot.texts(ADMIN_ID)[-1] == EN.t("admin.panel_ready")

    # ---- 11. Emergency access for an operator who is not in ADMIN_IDS.
    await bot.send(OPERATOR, "/admin")
    assert bot.texts(OPERATOR)[-1] == EN.t("admin.access_denied")
    await bot.send(OPERATOR, "/emergency_admin")
    await bot.send(OPERATOR, PASSWORD)
    assert bot.texts(OPERATOR)[-1] == EN.t("admin.panel_ready")
    async with scratch() as session:
        (access,) = list(await session.scalars(select(AdminAccessSession)))
        assert access.is_active(datetime.now(UTC))

    # ---- 12–14. A manual credit: the ledger row, its author, and nobody told.
    anna_texts_before = len(bot.texts(ANNA))
    calls_before = len(bot.telegram.calls)
    await bot.send(OPERATOR, "/admin_adjust_stamps")
    await bot.send(OPERATOR, str(ANNA))
    await bot.send(OPERATOR, "3")
    await bot.press(OPERATOR, bot.button(OPERATOR, CALLBACK_LOYALTY_CONFIRM_PREFIX))
    async with scratch() as session:
        assert await LoyaltyService(session).balance(anna_id) == 5
        rows = list(
            await session.scalars(
                select(LoyaltyTransaction)
                .where(LoyaltyTransaction.user_id == anna_id)
                .order_by(LoyaltyTransaction.id)
            )
        )
        assert [(r.kind, r.amount, r.balance_after) for r in rows] == [
            (LoyaltyTransactionType.PURCHASE, 2, 2),
            (LoyaltyTransactionType.ADJUSTMENT, 3, 5),
        ]
        (author,) = list(await session.scalars(select(LoyaltyStampAdjustment)))
        assert (author.transaction_id, author.user_id) == (rows[1].id, anna_id)
        assert (author.actor_kind, author.access_session_id) == (
            AdminAccessKind.BREAK_GLASS,
            access.id,
        )
        health = await loyalty_health(session)
        assert set(health["integrity"].values()) == {0}, health["integrity"]
        assert health["audit"] == {"adjustments_without_their_author": 0}
    touched = {
        int(m.chat_id)
        for m, _ in bot.telegram.calls[calls_before:]
        if getattr(m, "chat_id", None) is not None
    }
    assert touched == {OPERATOR}  # the operator's own screens; callback answers name no chat
    assert len(bot.texts(ANNA)) == anna_texts_before and bot.texts(MANAGER_CHAT_ID) == []

    # ---- 7. A restart: a new process on the same database.
    await scratch.kw["bind"].dispose()  # every connection closed, as when the database restarts
    bot.restart()
    invalidate_categories_cache()
    # The admin and the operator were inserted behind the bot's back above (a
    # rolling deploy's old instance would do the same): this start catches them
    # up with an account and a welcome spin each; the next one has nothing to do.
    assert await start(cfg) == (ActivationReport(accounts_opened=2, welcome_spins_granted=2), 0)
    assert await start(cfg) == (ActivationReport(0, 0), 0)
    async with scratch() as session:
        now = await snapshot(session, columns)
        assert {name: now[name] for name in untouchable} == catalog_and_orders
        assert {i: row[1] for i, row in now["users"].items() if i in legacy_identities} == (
            legacy_identities
        )  # every legacy customer, same Telegram id
        after = await balances(session)
        assert {k: v for k, v in after.items() if k in balances_before} == {
            **balances_before,
            anna_id: 5,
        }  # the legacy customers' stamps
        assert all(
            v == 0 for k, v in after.items() if k not in balances_before
        )  # newcomers: empty accounts
        assert await counts(session, ("loyalty_stamp_adjustments",)) == {
            "loyalty_stamp_adjustments": 1
        }
    await buy(bot, ANNA, product_id)
    await bot.send(ANNA, RU.t("menu.stamp_card"))
    assert "5/10" in bot.texts(ANNA)[-1]
    await bot.send(ADMIN_ID, "/admin")
    assert bot.texts(ADMIN_ID)[-1] == EN.t("admin.panel_ready")
    await bot.send(OPERATOR, "/admin")
    assert bot.texts(OPERATOR)[-1] == EN.t("admin.panel_ready")  # the session survived

    # ---- A restart with emergency access switched off: the kill switch, on record.
    off = settings(None)
    assert await start(off) == (ActivationReport(0, 0), 1)
    bot = RunningBot(scratch, off)
    await bot.send(OPERATOR, "/admin")
    assert bot.texts(OPERATOR) == [EN.t("admin.access_denied")]
    await bot.send(ADMIN_ID, "/admin")
    assert bot.texts(ADMIN_ID) == [EN.t("admin.panel_ready")]
    async with scratch() as session:
        (access,) = list(await session.scalars(select(AdminAccessSession)))
        assert access.is_revoked  # revoked, not deleted

    # ---- Nothing destructive can happen by accident: the downgrade refuses.
    with pytest.raises(RuntimeError, match="Refusing to downgrade f3c7a1d9e2b5"):
        await alembic("downgrade", "e8b2c4d6f1a3")
    async with scratch() as session:
        assert await scalar(session, "SELECT version_num FROM alembic_version") == head()
        assert await counts(session, ("loyalty_stamp_adjustments",)) == {
            "loyalty_stamp_adjustments": 1
        }
        now = await snapshot(session, columns)
        assert {name: now[name] for name in untouchable} == catalog_and_orders
        assert await scalar(session, "SELECT count(*) FROM orders") == 5
        assert (await session.execute(select(Order.id))).all()  # still there
