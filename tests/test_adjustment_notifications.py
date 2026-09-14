"""
A manual stamp credit is silent — and every other notification still speaks.

The bot has five outbound message paths: the new-order alert to the manager chat
and the admins, the order-status message to the customer, the referral news to
both sides, the broadcast fan-out, and the screens that answer the customer's
own taps. Each is triggered by its own event — a checkout, a status change, a
/start with a code, a confirmed broadcast, a tap — and none reads ledger rows.
A manual credit is a ledger row and an author row, nothing else: these tests
prove the customer and the manager chat hear nothing, that the state and the
audit trail change, and that the order and reward notifications around it keep
working exactly as before.
"""

from __future__ import annotations

import pathlib
import re
from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import lifecycle
from app.keyboards.admin_loyalty import CALLBACK_LOYALTY_CONFIRM_PREFIX
from app.keyboards.stamp_card import CALLBACK_STAMP_OPEN
from app.models.enums import LoyaltyTransactionType, OrderStatus
from app.models.loyalty import LoyaltyTransaction
from app.models.loyalty_adjustment import LoyaltyStampAdjustment
from app.models.user import User
from app.services.localization import LocalizationService
from app.services.loyalty import LoyaltyService
from tests.factories import make_user
from tests.production_bot import ADMIN_ID, MANAGER_CHAT_ID, RunningBot, tree_settings
from tests.test_loyalty_journeys import (  # noqa: F401  (sessions is a fixture)
    admin_moves,
    buy,
    check_out,
    latest_order,
    no_errors,
    open_shop,
    sessions,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent
EN = LocalizationService("en")
CUSTOMER, ALEX, BEA, SECOND_ADMIN = 9_901, 9_902, 9_903, 9_904
TO_COMPLETED = (OrderStatus.ACCEPTED, OrderStatus.SHIPPED, OrderStatus.COMPLETED)

# Where messages are made, and where a credit is made.
SENDERS = (
    "app/services/notification.py",
    "app/services/customer_notification.py",
    "app/services/referral_notification.py",
    "app/services/broadcast.py",
    "app/handlers/admin/broadcast.py",
    "app/handlers/admin/orders.py",
    "app/handlers/user/checkout.py",
    "app/handlers/user/start.py",
)
CREDIT_MODULES = ("app/services/admin/loyalty.py", "app/handlers/admin/loyalty.py")
ADJUSTMENT_MARKS = re.compile(
    r"LoyaltyTransactionType\.ADJUSTMENT|LoyaltyStampAdjustment|AdminLoyaltyService"
    r"|credit_stamps|loyalty_stamp_adjustments|PANEL_CREDIT_NOTE"
)
SENDER_MARKS = re.compile(
    r"OrderNotificationService|CustomerOrderNotificationService|ReferralNotificationService"
    r"|BroadcastService|send_message|send_photo|copy_message|notify_"
)


@pytest_asyncio.fixture
async def bot(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[RunningBot]:  # noqa: F811
    await open_shop(sessions)
    async with sessions() as session:
        await make_user(session, telegram_id=ADMIN_ID)
        customer = await make_user(session, telegram_id=CUSTOMER)
        customer.username = "quiet_customer"
        await session.commit()
    settings = tree_settings(admin_ids=[ADMIN_ID, SECOND_ADMIN])
    await lifecycle.activate_loyalty(settings)
    yield RunningBot(sessions, settings)


async def credit(bot: RunningBot, target: str, amount: int) -> None:
    await bot.send(ADMIN_ID, "/admin_adjust_stamps")
    await bot.send(ADMIN_ID, target)
    await bot.send(ADMIN_ID, str(amount))
    await bot.press(ADMIN_ID, bot.button(ADMIN_ID, CALLBACK_LOYALTY_CONFIRM_PREFIX))


def chats_written_to(bot: RunningBot, since: int = 0) -> set[int]:
    return {
        int(m.chat_id)
        for m, _ in bot.telegram.calls[since:]
        if getattr(m, "chat_id", None) is not None
    }


async def user_id(bot: RunningBot, telegram_id: int) -> int:
    async with bot.sessions() as session:
        return int(
            await session.scalar(select(User.id).where(User.telegram_id == telegram_id)) or 0
        )


async def adjustments(bot: RunningBot, uid: int) -> list[tuple[int, int, str | None, int | None]]:
    """(amount, balance_after, note, author's actor_user_id) of each adjustment row."""
    async with bot.sessions() as session:
        rows = await session.execute(
            select(LoyaltyTransaction, LoyaltyStampAdjustment)
            .outerjoin(
                LoyaltyStampAdjustment,
                LoyaltyStampAdjustment.transaction_id == LoyaltyTransaction.id,
            )
            .where(
                LoyaltyTransaction.user_id == uid,
                LoyaltyTransaction.kind == LoyaltyTransactionType.ADJUSTMENT,
            )
            .order_by(LoyaltyTransaction.id)
        )
        return [
            (txn.amount, txn.balance_after, txn.note, author.actor_user_id if author else None)
            for txn, author in rows.all()
        ]


# ======================================================= 1 + 2: state and audit


async def test_a_manual_credit_moves_the_balance_and_leaves_its_audit_trail(
    bot: RunningBot,
) -> None:
    uid = await user_id(bot, CUSTOMER)
    operator = await user_id(bot, ADMIN_ID)

    await credit(bot, "@quiet_customer", 3)

    async with bot.sessions() as session:
        assert await LoyaltyService(session).balance(uid) == 3
        assert await LoyaltyService(session).ledger_balance(uid) == 3
    assert await adjustments(bot, uid) == [(3, 3, "manual_credit:admin_panel", operator)]
    assert no_errors(bot, ADMIN_ID)


# ======================================================= 3 + 4: silence


async def test_the_customer_receives_no_message_about_the_credit(bot: RunningBot) -> None:
    await credit(bot, str(CUSTOMER), 3)

    assert CUSTOMER not in chats_written_to(bot)
    assert bot.texts(CUSTOMER) == []

    # The customer learns by looking: the card they open shows the new balance,
    # as the answer to their own tap — the only message they ever get about it.
    await bot.send(CUSTOMER, EN.t("menu.stamp_card"))
    assert bot.shows(CUSTOMER, CALLBACK_STAMP_OPEN)
    assert "3/10" in bot.texts(CUSTOMER)[-1]
    assert len(bot.texts(CUSTOMER)) == 1


async def test_the_manager_chat_and_the_other_admins_hear_nothing(bot: RunningBot) -> None:
    await credit(bot, str(CUSTOMER), 2)
    await credit(bot, str(CUSTOMER), 1)

    assert chats_written_to(bot) == {ADMIN_ID}  # the operator's own screens, nothing else
    assert MANAGER_CHAT_ID not in chats_written_to(bot)
    assert SECOND_ADMIN not in chats_written_to(bot)
    assert bot.texts(MANAGER_CHAT_ID) == [] and bot.texts(SECOND_ADMIN) == []


# ======================================================= 5: order notifications still speak


async def test_order_notifications_still_work_around_a_credit(bot: RunningBot) -> None:
    bottle = await _bottle_id(bot)
    uid = await user_id(bot, CUSTOMER)

    await credit(bot, str(CUSTOMER), 2)
    await buy(bot, CUSTOMER, bottle)
    await check_out(bot, CUSTOMER)

    # The new-order alert reaches the manager chat and every admin — including the
    # one who is not the operator — exactly once each.
    assert len(bot.texts(MANAGER_CHAT_ID)) == 1
    assert len(bot.texts(SECOND_ADMIN)) == 1
    assert "Order" in bot.texts(MANAGER_CHAT_ID)[0]

    heard_before = len(bot.texts(CUSTOMER))
    order = await latest_order(bot.sessions, uid)
    await admin_moves(bot, order.id, *TO_COMPLETED)
    status_texts = bot.texts(CUSTOMER)[heard_before:]
    assert [
        EN.t("notification.status_accepted", order_id=order.id),
        EN.t("notification.status_shipped", order_id=order.id),
        EN.t("notification.status_completed", order_id=order.id),
    ] == status_texts

    # Another credit afterwards: no further message anywhere but the operator's chat.
    marker = len(bot.telegram.calls)
    await credit(bot, str(CUSTOMER), 1)
    assert chats_written_to(bot, marker) == {ADMIN_ID}
    assert len(bot.texts(MANAGER_CHAT_ID)) == 1 and len(bot.texts(SECOND_ADMIN)) == 1
    async with bot.sessions() as session:
        # Two credits and one €20 purchase: 2 + 1 (purchase) + 1.
        assert await LoyaltyService(session).balance(uid) == 4
    assert no_errors(bot, CUSTOMER, ADMIN_ID)


# ======================================================= 6: reward notifications still speak


async def test_referral_reward_notifications_still_work_around_a_credit(
    sessions: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    bottle = await open_shop(sessions)
    async with sessions() as session:
        await make_user(session, telegram_id=ADMIN_ID)
        alex_id = (await make_user(session, telegram_id=ALEX)).id
        await session.commit()
    settings = tree_settings()
    await lifecycle.activate_loyalty(settings)
    bot = RunningBot(sessions, settings)

    # Alex invites Bea; Bea joins through the link.
    await bot.send(ALEX, EN.t("menu.invite"))
    invite = bot.screens(ALEX)[max(bot.screens(ALEX))]
    assert invite.reply_markup is not None
    link = next(
        b.copy_text.text for row in invite.reply_markup.inline_keyboard for b in row if b.copy_text
    )
    await bot.send(BEA, f"/start {link.split('?start=')[1]}", first_name="Bea")
    await bot.press(BEA, "lang:en")
    await bot.press(BEA, "city:berlin")
    assert sum(t.startswith(EN.t("invite.news.joined")) for t in bot.texts(ALEX)) == 1

    # A manual credit to Bea before her first order: no news to anyone.
    bea_heard, alex_heard = len(bot.texts(BEA)), len(bot.texts(ALEX))
    await credit(bot, str(BEA), 2)
    assert (len(bot.texts(BEA)), len(bot.texts(ALEX))) == (bea_heard, alex_heard)

    # Bea's first order completes: the reward news lands on both sides, as always.
    await buy(bot, BEA, bottle)
    await check_out(bot, BEA)
    bea_id = await user_id(bot, BEA)
    await admin_moves(bot, (await latest_order(sessions, bea_id)).id, *TO_COMPLETED)
    assert sum(t.startswith(EN.t("invite.news.paid")) for t in bot.texts(ALEX)) == 1
    assert sum(t.startswith(EN.t("invite.news.welcome_bonus")) for t in bot.texts(BEA)) == 1
    assert len(bot.texts(BEA)) > bea_heard

    # And a credit after the payout changes nothing about the news.
    marker = len(bot.telegram.calls)
    await credit(bot, str(BEA), 1)
    await credit(bot, str(ALEX), 1)
    assert chats_written_to(bot, marker) == {ADMIN_ID}
    news = ("invite.news.joined", "invite.news.paid", "invite.news.welcome_bonus")
    assert [sum(t.startswith(EN.t(k)) for t in bot.texts(ALEX)) for k in news] == [1, 1, 0]
    assert [sum(t.startswith(EN.t(k)) for t in bot.texts(BEA)) for k in news] == [0, 0, 1]
    async with sessions() as session:
        loyalty = LoyaltyService(session)
        assert await loyalty.balance(bea_id) == 2 + 1 + 2 + 1  # credit, purchase, bonus, credit
        assert await loyalty.balance(alex_id) == 2 + 1  # bonus, credit
    assert no_errors(bot, ALEX, BEA, ADMIN_ID)


# ======================================================= the exclusion is structural


def test_no_notification_path_knows_about_manual_credits_and_vice_versa() -> None:
    """Senders never read ledger rows or the credit; the credit never touches a sender."""
    for rel in SENDERS:
        source = (ROOT / rel).read_text(encoding="utf-8")
        assert not ADJUSTMENT_MARKS.search(source), rel
    for rel in CREDIT_MODULES:
        source = (ROOT / rel).read_text(encoding="utf-8")
        assert not SENDER_MARKS.search(source), rel
        assert "notification" not in source.lower() or "no message" in source.lower(), rel


def test_every_sender_states_that_no_ledger_row_triggers_it() -> None:
    for rel in SENDERS[:4]:
        source = (ROOT / rel).read_text(encoding="utf-8")
        assert "manual stamp credit" in source, f"{rel} does not state the exclusion"


async def _bottle_id(bot: RunningBot) -> int:
    from app.models.product import Product

    async with bot.sessions() as session:
        return int(await session.scalar(select(Product.id).order_by(Product.id).limit(1)) or 0)
