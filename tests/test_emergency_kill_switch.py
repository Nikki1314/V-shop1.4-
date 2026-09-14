"""
The kill switch, and the record of what an emergency session did.

Knowing the secret grants temporary access and nothing more. Two further
guarantees make that hold when the secret must be withdrawn: unsetting
``EMERGENCY_ADMIN_PASSWORD_HASH`` ends every session's power at once (the
resolver stops honouring sessions) and the next start revokes them on record;
and every update served under a session is logged with the session id, so the
request log says which privileged actions each emergency session took.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import lifecycle
from app.models.admin_access import AdminAccessSession
from app.models.enums import AdminAccessMethod
from app.repositories.admin_access_session import AdminAccessSessionRepository
from app.security.admin import resolve_admin_grant
from app.services.admin_access import AdminAccessService
from app.services.localization import LocalizationService
from app.utils.passwords import MIN_LOG_N, hash_password
from tests.factories import make_user
from tests.production_bot import ADMIN_ID, RunningBot, tree_settings
from tests.test_loyalty_journeys import sessions  # noqa: F401  (fixture)

EN = LocalizationService("en")
HASH = hash_password("operator on call tonight", log_n=MIN_LOG_N)
HOLDER, OTHER = 9_961, 9_962
PANEL, DENIED = EN.t("admin.panel_ready"), EN.t("admin.access_denied")


async def open_session(db: async_sessionmaker[AsyncSession], telegram_id: int) -> int:
    async with db() as session:
        user = await make_user(session, telegram_id=telegram_id)
        now = datetime.now(UTC)
        access = await AdminAccessSessionRepository(session).open(
            user.id,
            auth_method=AdminAccessMethod.BREAK_GLASS,
            created_at=now,
            expires_at=now + timedelta(hours=1),
        )
        await session.commit()
        return access.id


async def active_count(db: async_sessionmaker[AsyncSession]) -> int:
    async with db() as session:
        return await AdminAccessService(session).count_active()


@pytest_asyncio.fixture
async def holders(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[list[int]]:  # noqa: F811
    async with sessions() as session:
        await make_user(session, telegram_id=ADMIN_ID)
        await session.commit()
    yield [await open_session(sessions, HOLDER), await open_session(sessions, OTHER)]


# ======================================================= the switch, at the decision


async def test_with_the_feature_off_no_session_grants_anything(session: AsyncSession) -> None:
    user = await make_user(session, telegram_id=HOLDER)
    await AdminAccessService(session).open_break_glass(user.id, ttl=timedelta(minutes=30))
    holder = TgUser(id=HOLDER, is_bot=False, first_name="H")

    on = tree_settings(emergency_admin_password_hash=HASH)
    off = tree_settings(emergency_admin_password_hash=None)

    assert await resolve_admin_grant(holder, on, session) is not None
    assert await resolve_admin_grant(holder, off, session) is None
    # Configured admins are unaffected either way.
    admin = TgUser(id=ADMIN_ID, is_bot=False, first_name="A")
    assert await resolve_admin_grant(admin, off, None) is not None


async def test_unsetting_the_hash_locks_holders_out_on_the_next_update(
    sessions: async_sessionmaker[AsyncSession],  # noqa: F811
    holders: list[int],
) -> None:
    bot = RunningBot(sessions, tree_settings(emergency_admin_password_hash=HASH))
    await bot.send(HOLDER, "/admin")
    assert bot.texts(HOLDER) == [PANEL]

    # The operator removes the hash and restarts: a new process, the same database.
    bot = RunningBot(sessions, tree_settings(emergency_admin_password_hash=None))
    await bot.send(HOLDER, "/admin")
    assert bot.texts(HOLDER) == [DENIED]
    assert await bot.send(HOLDER, EN.t("admin.menu_products")) == []
    assert await bot.send(HOLDER, "/emergency_admin") == []  # and cannot log in again
    await bot.send(ADMIN_ID, "/admin")
    assert bot.texts(ADMIN_ID) == [PANEL]  # configured admins keep working
    assert await active_count(sessions) == 2  # rows untouched until the start-up reconciliation


# ======================================================= the switch, at start-up


async def test_start_up_revokes_every_active_session_while_the_feature_is_off(
    sessions: async_sessionmaker[AsyncSession],  # noqa: F811
    holders: list[int],
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):
        revoked = await lifecycle.reconcile_emergency_sessions(
            tree_settings(emergency_admin_password_hash=None)
        )

    assert revoked == 2 and await active_count(sessions) == 0
    async with sessions() as session:
        rows = list(await session.scalars(select(AdminAccessSession)))
    assert len(rows) == 2 and all(row.is_revoked for row in rows)  # on record, not deleted
    assert "Emergency admin access: disabled, sessions_revoked=2" in caplog.text

    # Switching the feature back on does not bring them back.
    bot = RunningBot(sessions, tree_settings(emergency_admin_password_hash=HASH))
    await bot.send(HOLDER, "/admin")
    assert bot.texts(HOLDER) == [DENIED]


async def test_start_up_with_the_feature_on_keeps_sessions_and_logs_the_count(
    sessions: async_sessionmaker[AsyncSession],  # noqa: F811
    holders: list[int],
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):
        revoked = await lifecycle.reconcile_emergency_sessions(
            tree_settings(emergency_admin_password_hash=HASH)
        )
    assert revoked == 0 and await active_count(sessions) == 2
    assert "Emergency admin access: enabled, active_sessions=2" in caplog.text


async def test_reconciliation_never_stops_the_bot(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken() -> object:
        raise RuntimeError("database not reachable")

    monkeypatch.setattr(lifecycle, "get_session_factory", broken)
    with caplog.at_level(logging.ERROR):
        assert await lifecycle.reconcile_emergency_sessions(tree_settings()) is None
    assert "Could not reconcile emergency admin sessions" in caplog.text


def test_start_up_runs_the_reconciliation() -> None:
    import inspect

    source = inspect.getsource(lifecycle.on_startup)
    assert "reconcile_emergency_sessions(settings)" in source
    assert source.index("activate_loyalty") < source.index("reconcile_emergency_sessions")
    assert source.index("reconcile_emergency_sessions") < source.index("delete_webhook")


# ======================================================= attribution


async def test_every_update_under_an_emergency_session_is_logged_with_its_session(
    sessions: async_sessionmaker[AsyncSession],  # noqa: F811
    holders: list[int],
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = RunningBot(sessions, tree_settings(emergency_admin_password_hash=HASH))
    session_id = holders[0]

    with caplog.at_level(logging.INFO):
        await bot.send(HOLDER, "/admin")
        await bot.send(HOLDER, EN.t("admin.menu_statistics"))
        await bot.press(HOLDER, "admin:st:rf")
        await bot.send(ADMIN_ID, "/admin")

    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Break-glass")]
    assert lines == [
        f"Break-glass admin update user_id={HOLDER} session_id={session_id} event=Message",
        f"Break-glass admin update user_id={HOLDER} session_id={session_id} event=Message",
        f"Break-glass admin update user_id={HOLDER} session_id={session_id} event=CallbackQuery",
    ]
    assert not any(f"user_id={ADMIN_ID}" in line for line in lines)
    assert EN.t("admin.menu_statistics") not in caplog.text  # ids only, never what was typed
