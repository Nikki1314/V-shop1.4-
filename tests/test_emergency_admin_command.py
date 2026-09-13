"""
``/emergency_admin`` through the production bot.

The command asks, the next message is the password, the service decides, and a
success opens the existing panel. These tests check what the operator sees in
every language, what the chat and the log never see (the password), that every
denial reads like a non-admin's ``/admin``, that the lockout and the session's
expiry and revocation hold end to end, and that the command performs no admin
operation of its own.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import pathlib
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from aiogram.methods import DeleteMessage
from aiogram.types import ReplyKeyboardRemove, Update
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.keyboards.admin import admin_cancel_keyboard, admin_menu_keyboard
from app.keyboards.reply import main_menu_keyboard
from app.models.admin_access import AdminAccessAttempt, AdminAccessSession
from app.models.enums import LanguageCode
from app.repositories.user import UserRepository
from app.services.admin_access import AdminAccessService
from app.services.localization import LocalizationService
from app.utils.passwords import MIN_LOG_N, hash_password
from tests.factories import make_user
from tests.production_bot import ADMIN_ID, RunningBot, tree_settings
from tests.test_loyalty_journeys import no_errors, sessions  # noqa: F401  (fixture)

ROOT = pathlib.Path(__file__).resolve().parent.parent
HANDLER = ROOT / "app" / "handlers" / "user" / "emergency_admin.py"
EN = LocalizationService("en")
PASSWORD = "correct horse battery staple"
HASH = hash_password(PASSWORD, log_n=MIN_LOG_N)
OPERATOR, NEWCOMER, SECOND = 9901, 9902, 9903
TTL_MINUTES = 20

ASK = EN.t("admin.emergency_ask_password")
DENIED = EN.t("admin.access_denied")
GRANTED = EN.t("admin.emergency_granted", minutes=TTL_MINUTES)
PANEL = EN.t("admin.panel_ready")
CANCELLED = EN.t("admin.emergency_cancelled")
CANCEL = EN.t("common.cancel")


def settings(**overrides: Any) -> Any:
    return tree_settings(
        **(
            {
                "emergency_admin_password_hash": HASH,
                "emergency_admin_session_ttl_minutes": TTL_MINUTES,
                "emergency_admin_max_failed_attempts": 2,
            }
            | overrides
        )
    )


@pytest_asyncio.fixture
async def bot(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[RunningBot]:  # noqa: F811
    async with sessions() as session:
        await make_user(session, telegram_id=ADMIN_ID)
        await make_user(session, telegram_id=OPERATOR)
        await make_user(session, telegram_id=NEWCOMER, city=None)  # not onboarded
        await session.commit()
    yield RunningBot(sessions, settings())


async def log_in(bot: RunningBot, telegram_id: int, password: str = PASSWORD) -> Update:
    """The command, then the password. Returns the password update (to check its deletion)."""
    await bot.send(telegram_id, "/emergency_admin")
    secret = bot.message(telegram_id, password)
    await bot.feed(secret)
    return secret


def deleted(bot: RunningBot, update: Update) -> bool:
    assert update.message is not None
    return any(
        isinstance(method, DeleteMessage)
        and method.chat_id == update.message.chat.id
        and method.message_id == update.message.message_id
        for method, _ in bot.telegram.calls
    )


def outgoing(bot: RunningBot) -> str:
    """Every text the bot sent anywhere, joined."""
    return "\n".join(
        str(getattr(method, "text", "") or "") + str(getattr(method, "caption", "") or "")
        for method, _ in bot.telegram.calls
    )


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


async def active_sessions(bot: RunningBot) -> list[AdminAccessSession]:
    async with bot.sessions() as session:
        rows = await session.scalars(select(AdminAccessSession).order_by(AdminAccessSession.id))
        now = datetime.now(UTC)
        return [row for row in rows if row.is_active(now)]


# ======================================================= the happy path


async def test_the_command_asks_then_the_password_opens_the_existing_panel(
    bot: RunningBot,
) -> None:
    secret = await log_in(bot, OPERATOR)

    assert bot.texts(OPERATOR) == [ASK, GRANTED, PANEL]
    prompt, _, panel = (
        m
        for m, _ in bot.telegram.calls
        if getattr(m, "chat_id", None) == OPERATOR and getattr(m, "text", None)
    )
    assert prompt.reply_markup == admin_cancel_keyboard(EN)
    assert panel.reply_markup == admin_menu_keyboard(EN)
    assert deleted(bot, secret)
    assert await attempts(bot, OPERATOR) == ["succeeded"]
    (access,) = await active_sessions(bot)
    assert access.expires_at - access.created_at == timedelta(minutes=TTL_MINUTES)

    # From here on the operator is an admin like any other, through the same gates.
    await bot.send(OPERATOR, "/admin")
    await bot.send(OPERATOR, EN.t("admin.menu_products"))
    assert bot.texts(OPERATOR)[3:5] == [PANEL, EN.t("admin.section_products")]
    assert no_errors(bot, OPERATOR)
    assert bot.settings.admin_ids == [ADMIN_ID]


@pytest.mark.parametrize("language", list(LanguageCode))
async def test_every_text_is_in_the_operators_language(
    sessions: async_sessionmaker[AsyncSession],  # noqa: F811
    language: LanguageCode,
) -> None:
    async with sessions() as session:
        await make_user(session, telegram_id=SECOND, language=language)
        await session.commit()
    bot = RunningBot(sessions, settings())
    own = LocalizationService(language.value)

    await bot.send(SECOND, "/emergency_admin")
    await bot.send(SECOND, "wrong")
    await bot.send(SECOND, "/emergency_admin")
    await bot.send(SECOND, own.t("common.cancel"))
    await log_in(bot, SECOND)

    assert bot.texts(SECOND) == [
        own.t("admin.emergency_ask_password"),
        own.t("admin.access_denied"),
        own.t("admin.emergency_ask_password"),
        own.t("admin.emergency_cancelled"),
        own.t("admin.emergency_ask_password"),
        own.t("admin.emergency_granted", minutes=TTL_MINUTES),
        own.t("admin.panel_ready"),
    ]
    assert PASSWORD not in outgoing(bot)


# ======================================================= the secret never shows


async def test_the_password_never_reaches_the_chat_or_the_log(
    bot: RunningBot, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        wrong = await log_in(bot, OPERATOR, PASSWORD + "!")
        right = await log_in(bot, OPERATOR)

    assert deleted(bot, wrong) and deleted(bot, right)
    assert PASSWORD not in outgoing(bot)
    assert PASSWORD not in caplog.text and HASH not in caplog.text
    assert "emergency" in caplog.text.lower()  # the audit line is there, without the secret


def test_the_handler_never_logs_or_formats_a_message_text() -> None:
    module = ast.parse(HANDLER.read_text(encoding="utf-8"))
    for node in ast.walk(module):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"debug", "info", "warning", "error", "exception"}:
                assert all(isinstance(arg, ast.Constant) for arg in node.args), ast.dump(node)
        if isinstance(node, ast.JoinedStr):  # an f-string
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            attrs = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
            assert "text" not in attrs and not names & {"password", "secret"}, ast.dump(node)


# ======================================================= denials read the same


async def test_a_wrong_password_is_the_non_admins_access_denied(bot: RunningBot) -> None:
    await bot.send(NEWCOMER, "/admin")
    secret = await log_in(bot, OPERATOR, "wrong")

    assert bot.texts(OPERATOR)[-1] == bot.texts(NEWCOMER)[-1] == DENIED
    assert deleted(bot, secret)
    assert await attempts(bot, OPERATOR) == ["failed"]
    assert await active_sessions(bot) == []
    denial = [
        m
        for m, _ in bot.telegram.calls
        if getattr(m, "text", None) == DENIED and m.chat_id == OPERATOR
    ][0]
    assert denial.reply_markup == main_menu_keyboard(EN)  # the shop menu comes back

    # The flow is over: the next message is not a password (and not an attempt).
    await bot.send(OPERATOR, "another guess")
    assert await attempts(bot, OPERATOR) == ["failed"]
    assert bot.texts(OPERATOR).count(DENIED) == 1


async def test_a_newcomer_who_never_onboarded_gets_no_keyboard_back(bot: RunningBot) -> None:
    await log_in(bot, NEWCOMER, "wrong")
    denial = next(m for m, _ in bot.telegram.calls if getattr(m, "text", None) == DENIED)
    assert isinstance(denial.reply_markup, ReplyKeyboardRemove)
    assert bot.texts(NEWCOMER) == [ASK, DENIED]


async def test_repeated_failures_lock_the_operator_out_even_with_the_right_password(
    bot: RunningBot,
) -> None:
    await log_in(bot, OPERATOR, "wrong")
    await log_in(bot, OPERATOR, "wrong again")
    await log_in(bot, OPERATOR)  # right, but locked out

    assert bot.texts(OPERATOR) == [ASK, DENIED] * 3
    assert await attempts(bot, OPERATOR) == ["failed", "failed", "locked_out"]
    assert await active_sessions(bot) == []
    await bot.send(OPERATOR, "/admin")
    assert bot.texts(OPERATOR)[-1] == DENIED


async def test_an_empty_or_non_text_message_is_never_the_password(bot: RunningBot) -> None:
    await bot.send(OPERATOR, "/emergency_admin")
    sticker = bot.message(OPERATOR, "placeholder")
    assert sticker.message is not None
    sticker = Update(
        update_id=sticker.update_id,
        message=sticker.message.model_copy(update={"text": None}),
    )
    await bot.feed(sticker)
    assert bot.texts(OPERATOR) == [ASK, ASK]  # asked again, nothing counted
    assert await attempts(bot, OPERATOR) == []

    await bot.send(OPERATOR, "   ")
    assert bot.texts(OPERATOR)[-1] == DENIED
    assert await attempts(bot, OPERATOR) == ["failed"]


async def test_cancel_leaves_the_flow_without_an_attempt(bot: RunningBot) -> None:
    await bot.send(OPERATOR, "/emergency_admin")
    await bot.send(OPERATOR, CANCEL)

    assert bot.texts(OPERATOR) == [ASK, CANCELLED]
    assert await attempts(bot, OPERATOR) == []
    await bot.send(OPERATOR, PASSWORD)  # no longer a password step
    assert await attempts(bot, OPERATOR) == [] and await active_sessions(bot) == []


async def test_with_the_feature_off_the_command_says_nothing(
    sessions: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    async with sessions() as session:
        await make_user(session, telegram_id=OPERATOR)
        await session.commit()
    bot = RunningBot(sessions, settings(emergency_admin_password_hash=None))

    assert await bot.send(OPERATOR, "/emergency_admin") == []
    assert await bot.send(OPERATOR, PASSWORD) == []  # no password step was opened
    assert await attempts(bot, OPERATOR) == []
    assert bot.texts(OPERATOR) == []


# ======================================================= the session's life


async def test_an_expired_session_is_rejected_by_the_panel(bot: RunningBot) -> None:
    await log_in(bot, OPERATOR)
    async with bot.sessions() as session:
        # Age the row: opened two hours ago, expired an hour ago (the CHECK keeps the order).
        await session.execute(
            update(AdminAccessSession).values(
                created_at=datetime.now(UTC) - timedelta(hours=2),
                expires_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        await session.commit()

    await bot.send(OPERATOR, "/admin")
    assert bot.texts(OPERATOR)[-1] == DENIED
    assert await bot.send(OPERATOR, EN.t("admin.menu_products")) == []


async def test_a_revoked_session_is_rejected_by_the_panel(bot: RunningBot) -> None:
    await log_in(bot, OPERATOR)
    (access,) = await active_sessions(bot)
    async with bot.sessions() as session:
        row = await session.get(AdminAccessSession, access.id)
        assert row is not None and await AdminAccessService(session).revoke(row)
        await session.commit()

    await bot.send(OPERATOR, "/admin")
    assert bot.texts(OPERATOR)[-1] == DENIED


async def test_logging_in_again_replaces_the_session(bot: RunningBot) -> None:
    await log_in(bot, OPERATOR)
    await log_in(bot, OPERATOR)

    assert len(await active_sessions(bot)) == 1
    assert await attempts(bot, OPERATOR) == ["succeeded", "succeeded"]
    async with bot.sessions() as session:
        total = len(list(await session.scalars(select(AdminAccessSession))))
    assert total == 2


# ======================================================= at once


async def test_two_operators_logging_in_at_once_each_get_their_own_session(
    sessions: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    async with sessions() as session:
        await make_user(session, telegram_id=OPERATOR)
        await make_user(session, telegram_id=SECOND)
        await session.commit()
    bot = RunningBot(sessions, settings())
    await asyncio.gather(
        bot.feed(bot.message(OPERATOR, "/emergency_admin")),
        bot.feed(bot.message(SECOND, "/emergency_admin")),
    )

    await asyncio.gather(
        bot.feed(bot.message(OPERATOR, PASSWORD)),
        bot.feed(bot.message(SECOND, PASSWORD)),
    )

    assert bot.texts(OPERATOR) == bot.texts(SECOND) == [ASK, GRANTED, PANEL]
    async with sessions() as session:
        users = UserRepository(session)
        expected = set()
        for telegram_id in (OPERATOR, SECOND):
            user = await users.get_by_telegram_id(telegram_id)
            assert user is not None
            expected.add(user.id)
    assert {a.user_id for a in await active_sessions(bot)} == expected
    assert no_errors(bot, OPERATOR, SECOND)


async def test_one_operator_sending_the_password_twice_at_once_holds_one_session(
    bot: RunningBot,
) -> None:
    await bot.send(OPERATOR, "/emergency_admin")

    await asyncio.gather(
        bot.feed(bot.message(OPERATOR, PASSWORD)),
        bot.feed(bot.message(OPERATOR, PASSWORD)),
    )

    active = await active_sessions(bot)
    assert len(active) == 1
    assert bot.texts(OPERATOR).count(PANEL) == (await attempts(bot, OPERATOR)).count("succeeded")
    assert PASSWORD not in outgoing(bot)
    assert no_errors(bot, OPERATOR)


# ======================================================= the command itself does nothing admin


def test_the_command_performs_no_admin_operation_of_its_own() -> None:
    source = HANDLER.read_text(encoding="utf-8")
    admin_imports = re.findall(
        r"^from app\.(?:handlers\.admin|services\.admin)\S* import (.+)$", source, re.M
    )
    assert admin_imports == ["show_admin_menu"]
    assert "AdminService" not in source and "admin:" not in source
    assert "resolve_admin_grant" not in source and "AdminAccessService" not in source


def test_the_password_step_is_registered_before_every_free_text_handler() -> None:
    source = (ROOT / "app" / "handlers" / "user" / "__init__.py").read_text(encoding="utf-8")
    order = re.findall(r"router\.include_router\((\w+)\.router\)", source)
    assert order.index("start") < order.index("emergency_admin") < order.index("catalog")
    assert order.index("emergency_admin") < order.index("checkout")
