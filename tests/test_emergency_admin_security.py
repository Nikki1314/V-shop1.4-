"""
Security review of emergency admin access — the attacks, as regression tests.

Each test is an attempt that was made against the implementation during the
review: the hash mangled by the deployment's own `.env` parsing, a lockout that
an attacker could inflate into a table-filling flood, a password left in the
chat, a stale FSM after a restart, a replayed update, a spoofed sender, a
forged grant, a restart or a second process forgetting the lockout.
"""

from __future__ import annotations

import asyncio
import pathlib
import re
import tempfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest_asyncio
from aiogram.methods import DeleteMessage
from aiogram.types import Chat, Message, Update
from aiogram.types import User as TgUser
from dotenv import dotenv_values
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.admin_access import AdminAccessAttempt, AdminAccessSession
from app.repositories.user import UserRepository
from app.security.admin import AdminAccessKind, AdminGrant
from app.services.localization import LocalizationService
from app.services.notification import OrderNotificationService
from app.utils.passwords import MIN_LOG_N, hash_password, parse_password_hash, verify_password
from tests.factories import make_user
from tests.production_bot import ADMIN_ID, MANAGER_CHAT_ID, RunningBot, tree_settings
from tests.test_loyalty_journeys import no_errors, sessions  # noqa: F401  (fixture)

ROOT = pathlib.Path(__file__).resolve().parent.parent
EN = LocalizationService("en")
PASSWORD = "correct horse battery staple"
HASH = hash_password(PASSWORD, log_n=MIN_LOG_N)
OPERATOR, STRANGER = 9951, 9952
ASK = EN.t("admin.emergency_ask_password")
DENIED = EN.t("admin.access_denied")
PANEL = EN.t("admin.panel_ready")
GRANTED = EN.t("admin.emergency_granted", minutes=30)

# What Docker Compose (env_file interpolation) and POSIX shells treat as a variable.
VARIABLE_REFERENCE = re.compile(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?")


def settings(**overrides: Any) -> Any:
    return tree_settings(
        **(
            {"emergency_admin_password_hash": HASH, "emergency_admin_max_failed_attempts": 2}
            | overrides
        )
    )


@pytest_asyncio.fixture
async def bot(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[RunningBot]:  # noqa: F811
    async with sessions() as session:
        await make_user(session, telegram_id=ADMIN_ID)
        await make_user(session, telegram_id=OPERATOR)
        await make_user(session, telegram_id=STRANGER)
        await session.commit()
    yield RunningBot(sessions, settings())


async def attempts(bot: RunningBot, telegram_id: int) -> list[str]:
    async with bot.sessions() as session:
        user = await UserRepository(session).get_by_telegram_id(telegram_id)
        assert user is not None
        rows = await session.scalars(
            select(AdminAccessAttempt)
            .where(AdminAccessAttempt.user_id == user.id)
            .order_by(AdminAccessAttempt.id)
        )
        return [row.outcome.value for row in rows]


async def active_sessions(bot: RunningBot) -> int:
    async with bot.sessions() as session:
        rows = await session.scalars(select(AdminAccessSession))
        now = datetime.now(UTC)
        return sum(1 for row in rows if row.is_active(now))


def deleted(bot: RunningBot, update: Update) -> bool:
    assert update.message is not None
    return any(
        isinstance(m, DeleteMessage) and m.message_id == update.message.message_id
        for m, _ in bot.telegram.calls
    )


# ======================================================= P1: the hash must survive `.env`


def test_the_hash_contains_nothing_an_env_parser_or_compose_would_touch() -> None:
    """
    Found in review: the first encoding was the PHC form, `$scrypt$ln=…$salt$digest`.
    Docker Compose interpolates `$name` inside `env_file` values, so the bot
    container would have received a mangled hash and refused every operator.
    """
    for _ in range(20):
        encoded = hash_password(PASSWORD, log_n=MIN_LOG_N)
        assert VARIABLE_REFERENCE.sub("", encoded) == encoded
        assert not set(encoded) & set("$#'\"\\ \t\n`")
        assert parse_password_hash(encoded).encode() == encoded


def test_the_hash_round_trips_through_a_dotenv_file() -> None:
    encoded = hash_password(PASSWORD, log_n=MIN_LOG_N)
    with tempfile.TemporaryDirectory() as folder:
        env = pathlib.Path(folder) / ".env"
        env.write_text(f"EMERGENCY_ADMIN_PASSWORD_HASH={encoded}\n", encoding="utf-8")
        read = dotenv_values(env)["EMERGENCY_ADMIN_PASSWORD_HASH"]
    assert read == encoded
    assert verify_password(PASSWORD, read or "")


def test_the_old_dollar_form_is_refused_rather_than_half_read() -> None:
    assert not verify_password(
        PASSWORD,
        "$scrypt$ln=10,r=8,p=1$AAAAAAAAAAAAAAAAAAAAAA$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    )


# ======================================================= P2: a locked-out flood is bounded


async def test_a_locked_out_account_cannot_grow_the_attempts_table(bot: RunningBot) -> None:
    for guess in ("a", "b"):
        await bot.send(OPERATOR, "/emergency_admin")
        await bot.send(OPERATOR, guess)
    for _ in range(25):
        await bot.send(OPERATOR, "/emergency_admin")
        await bot.send(OPERATOR, PASSWORD)

    assert await attempts(bot, OPERATOR) == ["failed", "failed", "locked_out"]
    assert await active_sessions(bot) == 0
    assert bot.texts(OPERATOR).count(DENIED) == 27


# ======================================================= P2: a secret typed after the command


async def test_a_password_given_with_the_command_is_removed_and_checked(bot: RunningBot) -> None:
    wrong = bot.message(OPERATOR, "/emergency_admin nope")
    await bot.feed(wrong)
    assert deleted(bot, wrong)
    assert bot.texts(OPERATOR) == [DENIED]

    right = bot.message(OPERATOR, f"/emergency_admin {PASSWORD}")
    await bot.feed(right)
    assert deleted(bot, right)
    assert bot.texts(OPERATOR) == [DENIED, GRANTED, PANEL]
    assert await attempts(bot, OPERATOR) == ["failed", "succeeded"]
    assert PASSWORD not in "\n".join(str(getattr(m, "text", "")) for m, _ in bot.telegram.calls)


# ======================================================= P3: a command is not a password


async def test_a_command_typed_at_the_prompt_is_not_spent_as_an_attempt(bot: RunningBot) -> None:
    await bot.send(ADMIN_ID, "/emergency_admin")
    await bot.send(ADMIN_ID, "/admin")  # the configured admin's panel, not a failed guess
    assert bot.texts(ADMIN_ID) == [ASK, PANEL]
    assert await attempts(bot, ADMIN_ID) == []

    await bot.send(OPERATOR, "/emergency_admin")
    await bot.send(OPERATOR, "/admin")
    assert bot.texts(OPERATOR) == [ASK, DENIED]
    assert await attempts(bot, OPERATOR) == []
    await bot.send(OPERATOR, PASSWORD)  # the prompt is still open
    assert bot.texts(OPERATOR)[-1] == PANEL


# ======================================================= state survives restarts; FSM does not


async def test_a_restart_forgets_the_prompt_but_neither_the_session_nor_the_lockout(
    bot: RunningBot,
) -> None:
    await bot.send(OPERATOR, "/emergency_admin")
    await bot.send(OPERATOR, PASSWORD)
    assert bot.texts(OPERATOR)[-1] == PANEL

    bot.restart()  # new process: empty FSM storage, same database

    await bot.send(OPERATOR, "/admin")
    assert bot.texts(OPERATOR)[-1] == PANEL  # the session is in the database
    assert await active_sessions(bot) == 1

    for guess in ("x", "y"):
        await bot.send(STRANGER, "/emergency_admin")
        await bot.send(STRANGER, guess)
    bot.restart()
    await bot.send(STRANGER, "/emergency_admin")
    await bot.send(STRANGER, PASSWORD)
    assert bot.texts(STRANGER)[-1] == DENIED  # the lockout is in the database too
    assert await attempts(bot, STRANGER) == ["failed", "failed", "locked_out"]

    # A prompt opened before a restart is gone: the next text is an ordinary message.
    await bot.send(ADMIN_ID, "/emergency_admin")
    bot.restart()
    await bot.send(ADMIN_ID, "not a password any more")
    assert await attempts(bot, ADMIN_ID) == []


async def test_a_replayed_password_update_changes_nothing(bot: RunningBot) -> None:
    await bot.send(OPERATOR, "/emergency_admin")
    secret = bot.message(OPERATOR, PASSWORD)
    await bot.feed(secret)
    before = (await attempts(bot, OPERATOR), await active_sessions(bot), bot.texts(OPERATOR))

    await bot.feed(secret)  # Telegram delivered it twice

    assert (await attempts(bot, OPERATOR), await active_sessions(bot)) == before[:2]
    assert bot.texts(OPERATOR) == before[2]
    assert no_errors(bot, OPERATOR)


# ======================================================= identity is the sender, never the chat


async def test_a_spoofed_sender_in_the_holders_chat_is_the_spoofer(bot: RunningBot) -> None:
    await bot.send(OPERATOR, "/emergency_admin")
    await bot.send(OPERATOR, PASSWORD)
    forged = Update(
        update_id=999_001,
        message=Message(
            message_id=999_001,
            date=datetime.now(UTC),
            chat=Chat(id=OPERATOR, type="private"),
            from_user=TgUser(id=STRANGER, is_bot=False, first_name="S"),
            text="/admin",
        ),
    )

    await bot.feed(forged)

    assert bot.texts(OPERATOR)[-1] == DENIED


async def test_a_forged_grant_cannot_be_smuggled_through_workflow_data(bot: RunningBot) -> None:
    """Even a grant planted in the dispatcher's data names its holder; a stranger is re-checked."""
    bot.dispatcher["admin_grant"] = AdminGrant(
        telegram_id=OPERATOR, kind=AdminAccessKind.BREAK_GLASS, session_id=1
    )
    try:
        await bot.send(STRANGER, "/admin")
        assert bot.texts(STRANGER) == [DENIED]
        calls = await bot.send(STRANGER, EN.t("admin.menu_products"))
        assert calls == []
    finally:
        del bot.dispatcher["admin_grant"]


# ======================================================= concurrency


async def test_concurrent_wrong_guesses_cannot_exceed_the_limit(bot: RunningBot) -> None:
    await bot.send(OPERATOR, "/emergency_admin")
    # Ten guesses land at once against a limit of two.
    await asyncio.gather(*(bot.feed(bot.message(OPERATOR, f"guess {n}")) for n in range(10)))

    recorded = await attempts(bot, OPERATOR)
    assert recorded.count("failed") <= 2
    assert await active_sessions(bot) == 0
    await bot.send(OPERATOR, "/emergency_admin")
    await bot.send(OPERATOR, PASSWORD)
    expected = DENIED if recorded.count("failed") == 2 else PANEL
    assert bot.texts(OPERATOR)[-1] == expected


# ======================================================= membership and alerts


async def test_a_holder_is_never_a_configured_admin_nor_alerted_as_one(bot: RunningBot) -> None:
    await bot.send(OPERATOR, "/emergency_admin")
    await bot.send(OPERATOR, PASSWORD)
    assert bot.texts(OPERATOR)[-1] == PANEL

    assert bot.settings.admin_ids == [ADMIN_ID]
    alerts = OrderNotificationService(bot.bot, bot.settings)
    assert alerts.notification_chat_ids() == [MANAGER_CHAT_ID, ADMIN_ID]
