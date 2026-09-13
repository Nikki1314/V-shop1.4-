"""
Who may use the admin panel: ``ADMIN_IDS``, or an active emergency session.

The decision is ``resolve_admin_grant``; the admin router's filter and middleware
and the user router's ``/admin`` denial all ask it. These tests take it on its
own, then drive the production dispatcher: a configured admin, a stranger, a
break-glass holder, an expired session, a revoked one, someone else's, and all of
them at once.
"""

from __future__ import annotations

import asyncio
import pathlib
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest_asyncio
from aiogram.types import Chat, Message
from aiogram.types import User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.keyboards.admin import admin_menu_keyboard
from app.keyboards.admin_statistics import CALLBACK_STATS_REFRESH
from app.middlewares.admin import AdminOnlyMiddleware
from app.models.enums import AdminAccessMethod
from app.repositories.admin_access_session import AdminAccessSessionRepository
from app.repositories.user import UserRepository
from app.security.admin import (
    AdminAccessKind,
    AdminGrant,
    is_admin_id,
    is_admin_user,
    resolve_admin_grant,
)
from app.services.admin_access import AdminAccessService
from app.services.localization import LocalizationService
from tests.factories import make_user
from tests.production_bot import ADMIN_ID, RunningBot, tree_settings
from tests.test_loyalty_journeys import no_errors, sessions  # noqa: F401  (fixture)

ROOT = pathlib.Path(__file__).resolve().parent.parent
EN = LocalizationService("en")
PANEL = EN.t("admin.panel_ready")
DENIED = EN.t("admin.access_denied")
BREAK_GLASS, ORDINARY, EXPIRED, REVOKED, OTHER = 9801, 9802, 9803, 9804, 9805
NEVER_SEEN = 9899
TTL = timedelta(minutes=30)


def tg(telegram_id: int) -> TgUser:
    return TgUser(id=telegram_id, is_bot=False, first_name="T")


async def register(db: async_sessionmaker[AsyncSession], *telegram_ids: int) -> None:
    async with db() as session:
        for telegram_id in telegram_ids:
            await make_user(session, telegram_id=telegram_id)
        await session.commit()


async def open_session(
    db: async_sessionmaker[AsyncSession],
    telegram_id: int,
    *,
    ttl: timedelta = TTL,
    ago: timedelta = timedelta(0),
) -> int:
    """A session for ``telegram_id`` opened ``ago`` ago, lasting ``ttl``. Returns its id."""
    async with db() as session:
        user = await UserRepository(session).get_by_telegram_id(telegram_id)
        assert user is not None
        started = datetime.now(UTC) - ago
        access = await AdminAccessSessionRepository(session).open(
            user.id,
            auth_method=AdminAccessMethod.BREAK_GLASS,
            created_at=started,
            expires_at=started + ttl,
        )
        await session.commit()
        return access.id


async def revoke(db: async_sessionmaker[AsyncSession], session_id: int) -> None:
    async with db() as session:
        access = await AdminAccessSessionRepository(session).get_by_id(session_id)
        assert access is not None
        assert await AdminAccessService(session).revoke(access)
        await session.commit()


@pytest_asyncio.fixture
async def bot(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[RunningBot]:  # noqa: F811
    await register(sessions, ADMIN_ID, BREAK_GLASS, ORDINARY, EXPIRED, REVOKED, OTHER)
    yield RunningBot(sessions, tree_settings())


# ======================================================= the decision itself


async def test_a_configured_admin_is_granted_from_settings_alone(session: AsyncSession) -> None:
    grant = await resolve_admin_grant(tg(ADMIN_ID), tree_settings(), None)
    assert grant == AdminGrant(telegram_id=ADMIN_ID, kind=AdminAccessKind.CONFIGURED)
    assert not grant.is_break_glass


async def test_nobody_and_an_ordinary_user_get_nothing(session: AsyncSession) -> None:
    await make_user(session, telegram_id=ORDINARY)
    settings = tree_settings()
    assert await resolve_admin_grant(None, settings, session) is None
    assert await resolve_admin_grant(tg(ORDINARY), settings, session) is None
    assert await resolve_admin_grant(tg(NEVER_SEEN), settings, session) is None


async def test_an_active_session_grants_break_glass_access(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=BREAK_GLASS)
    opened = await AdminAccessService(session).open_break_glass(user.id, ttl=TTL)

    grant = await resolve_admin_grant(tg(BREAK_GLASS), tree_settings(), session)

    assert grant is not None and grant.is_break_glass
    assert grant.kind == AdminAccessKind.BREAK_GLASS
    assert grant.session_id == opened.session.id
    assert grant.expires_at == opened.session.expires_at
    # A grant is not membership.
    assert not is_admin_id(BREAK_GLASS, tree_settings())
    assert not is_admin_user(tg(BREAK_GLASS), tree_settings())


async def test_an_expired_or_revoked_session_grants_nothing(session: AsyncSession) -> None:
    expired = await make_user(session, telegram_id=EXPIRED)
    revoked = await make_user(session, telegram_id=REVOKED)
    access = AdminAccessService(session)
    started = datetime.now(UTC) - timedelta(hours=2)
    await access.sessions.open(
        expired.id,
        auth_method=AdminAccessMethod.BREAK_GLASS,
        created_at=started,
        expires_at=started + timedelta(hours=1),
    )
    opened = await access.open_break_glass(revoked.id, ttl=TTL)
    await access.revoke(opened.session)

    settings = tree_settings()
    assert await resolve_admin_grant(tg(EXPIRED), settings, session) is None
    assert await resolve_admin_grant(tg(REVOKED), settings, session) is None


async def test_without_a_database_session_only_configured_admins_pass(
    session: AsyncSession,
) -> None:
    """A path that runs before DatabaseMiddleware cannot consult sessions: it fails closed."""
    user = await make_user(session, telegram_id=BREAK_GLASS)
    await AdminAccessService(session).open_break_glass(user.id, ttl=TTL)
    assert await resolve_admin_grant(tg(BREAK_GLASS), tree_settings(), None) is None
    assert await resolve_admin_grant(tg(ADMIN_ID), tree_settings(), None) is not None


# ======================================================= the middleware reuses the decision


async def _handler(event: Any, data: dict[str, Any]) -> dict[str, Any]:
    return data


def _message(telegram_id: int) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=telegram_id, type="private"),
        from_user=tg(telegram_id),
        text="/admin",
    )


async def test_the_middleware_trusts_the_filters_grant_and_injects_it() -> None:
    grant = AdminGrant(telegram_id=BREAK_GLASS, kind=AdminAccessKind.BREAK_GLASS, session_id=7)
    data: dict[str, Any] = {
        "settings": tree_settings(),
        "event_from_user": tg(BREAK_GLASS),
        "admin_grant": grant,
        "session": None,  # no database: proves the grant was reused, not re-resolved
    }
    result = await AdminOnlyMiddleware()(_handler, _message(BREAK_GLASS), data)
    assert result is not None
    assert result["is_admin"] is True and result["admin_grant"] is grant


async def test_the_middleware_does_not_trust_a_grant_for_someone_else() -> None:
    stray = AdminGrant(telegram_id=BREAK_GLASS, kind=AdminAccessKind.BREAK_GLASS, session_id=7)
    data: dict[str, Any] = {
        "settings": tree_settings(),
        "event_from_user": tg(ORDINARY),
        "admin_grant": stray,
        "session": None,
    }
    assert await AdminOnlyMiddleware()(_handler, _message(ORDINARY), data) is None


async def test_the_middleware_decides_alone_when_no_filter_ran() -> None:
    data: dict[str, Any] = {
        "settings": tree_settings(),
        "event_from_user": tg(ADMIN_ID),
        "session": None,
    }
    result = await AdminOnlyMiddleware()(_handler, _message(ADMIN_ID), data)
    assert result is not None and result["admin_grant"].kind == AdminAccessKind.CONFIGURED
    data["event_from_user"] = tg(ORDINARY)
    assert await AdminOnlyMiddleware()(_handler, _message(ORDINARY), data) is None


# ======================================================= through the production bot


async def test_a_configured_admin_works_exactly_as_before(bot: RunningBot) -> None:
    await bot.send(ADMIN_ID, "/admin")
    await bot.send(ADMIN_ID, EN.t("admin.menu_products"))

    assert bot.texts(ADMIN_ID)[:2] == [PANEL, EN.t("admin.section_products")]
    assert no_errors(bot, ADMIN_ID)
    async with bot.sessions() as session:
        assert await AdminAccessSessionRepository(session).count() == 0


async def test_an_ordinary_user_is_denied_and_admin_buttons_stay_silent(bot: RunningBot) -> None:
    await bot.send(ORDINARY, "/admin")
    assert bot.texts(ORDINARY) == [DENIED]

    calls = await bot.send(ORDINARY, EN.t("admin.menu_products"))
    assert calls == []
    calls = await bot.press(ORDINARY, CALLBACK_STATS_REFRESH, on=_message(ORDINARY))
    assert calls == [] and bot.alerts(ORDINARY) == []

    await bot.send(NEVER_SEEN, "/admin")
    assert bot.texts(NEVER_SEEN) == [DENIED]


async def test_an_active_session_opens_the_very_same_panel(bot: RunningBot) -> None:
    await open_session(bot.sessions, BREAK_GLASS)

    await bot.send(ADMIN_ID, "/admin")
    await bot.send(BREAK_GLASS, "/admin")

    admin_screen = bot.screens(ADMIN_ID).popitem()[1]
    holder_screen = bot.screens(BREAK_GLASS).popitem()[1]
    assert holder_screen.text == admin_screen.text == PANEL
    admin_menu = next(m for m, _ in bot.telegram.calls if getattr(m, "chat_id", None) == ADMIN_ID)
    holder_menu = next(
        m for m, _ in bot.telegram.calls if getattr(m, "chat_id", None) == BREAK_GLASS
    )
    assert holder_menu.reply_markup == admin_menu.reply_markup == admin_menu_keyboard(EN)

    # The existing sections work for the holder too, through the same handlers.
    await bot.send(BREAK_GLASS, EN.t("admin.menu_statistics"))
    assert bot.shows(BREAK_GLASS, CALLBACK_STATS_REFRESH)
    await bot.press(BREAK_GLASS, CALLBACK_STATS_REFRESH)
    assert bot.alerts(BREAK_GLASS) == [(EN.t("admin.stats_refreshed"), False)]
    assert no_errors(bot, BREAK_GLASS)

    # ...and the permanent list is untouched.
    assert bot.settings.admin_ids == [ADMIN_ID]
    assert not is_admin_id(BREAK_GLASS, bot.settings)


async def test_an_expired_session_is_a_stranger_again(bot: RunningBot) -> None:
    await open_session(bot.sessions, EXPIRED, ttl=timedelta(hours=1), ago=timedelta(hours=2))

    await bot.send(EXPIRED, "/admin")
    assert bot.texts(EXPIRED) == [DENIED]
    assert await bot.send(EXPIRED, EN.t("admin.menu_products")) == []


async def test_a_revoked_session_stops_working_on_the_next_message(bot: RunningBot) -> None:
    session_id = await open_session(bot.sessions, REVOKED)
    await bot.send(REVOKED, "/admin")
    await bot.send(REVOKED, EN.t("admin.menu_statistics"))
    assert bot.texts(REVOKED)[0] == PANEL and bot.shows(REVOKED, CALLBACK_STATS_REFRESH)
    seen = len(bot.texts(REVOKED))

    await revoke(bot.sessions, session_id)

    await bot.send(REVOKED, "/admin")
    assert bot.texts(REVOKED)[seen:] == [DENIED]
    # The statistics screen is still on their phone; its button now does nothing at all.
    calls = await bot.press(REVOKED, CALLBACK_STATS_REFRESH)
    assert calls == [] and bot.alerts(REVOKED) == []


async def test_someone_elses_or_a_superseded_session_is_no_session(bot: RunningBot) -> None:
    await open_session(bot.sessions, OTHER)
    await bot.send(ORDINARY, "/admin")
    assert bot.texts(ORDINARY) == [DENIED]

    # Re-authenticating supersedes: the first session is revoked, the second is the one that counts.
    async with bot.sessions() as session:
        user = await UserRepository(session).get_by_telegram_id(BREAK_GLASS)
        assert user is not None
        access = AdminAccessService(session)
        first = (await access.open_break_glass(user.id, ttl=TTL)).session
        second = (await access.open_break_glass(user.id, ttl=TTL)).session
        await session.commit()
        first_id, second_id = first.id, second.id
    await bot.send(BREAK_GLASS, "/admin")
    assert bot.texts(BREAK_GLASS) == [PANEL]
    await revoke(bot.sessions, second_id)
    await bot.send(BREAK_GLASS, "/admin")
    assert bot.texts(BREAK_GLASS) == [PANEL, DENIED]
    async with bot.sessions() as session:
        stale = await AdminAccessSessionRepository(session).get_by_id(first_id)
        assert stale is not None and stale.is_revoked


async def test_concurrent_requests_are_each_decided_for_their_own_sender(
    bot: RunningBot,
) -> None:
    await open_session(bot.sessions, BREAK_GLASS)
    await open_session(bot.sessions, EXPIRED, ttl=timedelta(hours=1), ago=timedelta(hours=2))
    senders = [ADMIN_ID, BREAK_GLASS, ORDINARY, EXPIRED] * 6

    await asyncio.gather(*(bot.feed(bot.message(sender, "/admin")) for sender in senders))

    assert bot.texts(ADMIN_ID) == [PANEL] * 6
    assert bot.texts(BREAK_GLASS) == [PANEL] * 6
    assert bot.texts(ORDINARY) == [DENIED] * 6
    assert bot.texts(EXPIRED) == [DENIED] * 6
    assert no_errors(bot, ADMIN_ID, BREAK_GLASS, ORDINARY, EXPIRED)


async def test_concurrent_requests_after_revocation_are_all_refused(bot: RunningBot) -> None:
    session_id = await open_session(bot.sessions, REVOKED)
    await asyncio.gather(*(bot.feed(bot.message(REVOKED, "/admin")) for _ in range(5)))
    assert bot.texts(REVOKED) == [PANEL] * 5

    await revoke(bot.sessions, session_id)

    await asyncio.gather(*(bot.feed(bot.message(REVOKED, "/admin")) for _ in range(5)))
    assert bot.texts(REVOKED) == [PANEL] * 5 + [DENIED] * 5


# ======================================================= no handler decides on its own


def test_no_handler_checks_emergency_access_itself() -> None:
    """The decision lives in app/security; handlers only ever receive ``admin_grant``."""
    forbidden = re.compile(
        r"resolve_admin_grant|AdminAccessService|admin_access_session"
        r"|services\.emergency_admin|is_admin_user|is_admin_id"
    )
    # The one handler that *authenticates* — it opens a session through the
    # service and decides nothing about admin access itself.
    authenticates = "app/handlers/user/emergency_admin.py"
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in sorted((ROOT / "app" / "handlers").rglob("*.py"))
        if forbidden.search(path.read_text(encoding="utf-8"))
        and not (
            path.relative_to(ROOT).as_posix() == authenticates
            and set(forbidden.findall(path.read_text(encoding="utf-8")))
            == {"services.emergency_admin"}
        )
    ]
    assert offenders == []
