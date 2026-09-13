"""
Temporary admin sessions — the persistence behind break-glass access.

No password and no command exist yet; these tests pin what the table and the
service promise on their own: a session belongs to one registered user, lives
from one clock for a bounded time, stops granting anything the moment it expires
or is revoked, is never deleted, and stores no credential.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.admin_access import AdminAccessSession, as_utc
from app.models.enums import AdminAccessMethod
from app.services.admin_access import (
    MAX_SESSION_TTL,
    MIN_SESSION_TTL,
    AdminAccessError,
    AdminAccessService,
    InvalidSessionTtlError,
)
from tests.factories import make_user

ROOT = pathlib.Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "alembic" / "versions" / "d7a3f9c2e8b1_admin_access_sessions.py"

START = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
TTL = timedelta(minutes=30)
OPERATOR, BYSTANDER = 9601, 9602


class Clock:
    """A clock the tests move by hand."""

    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def service(session: AsyncSession, clock: Clock) -> AdminAccessService:
    return AdminAccessService(session, clock=clock)


# ======================================================= creation


async def test_a_session_is_opened_for_the_user_from_the_service_clock(
    session: AsyncSession,
) -> None:
    operator = await make_user(session, telegram_id=OPERATOR)
    clock = Clock()

    opened = await service(session, clock).open_break_glass(operator.id, ttl=TTL)

    access = opened.session
    assert access.id is not None
    assert opened.superseded == 0
    assert access.user_id == operator.id
    assert access.auth_method == AdminAccessMethod.BREAK_GLASS
    assert access.created_at == START
    assert access.expires_at == START + TTL
    assert access.revoked_at is None and not access.is_revoked
    assert access.is_active(clock())


async def test_the_session_is_found_for_its_holders_telegram_id(session: AsyncSession) -> None:
    operator = await make_user(session, telegram_id=OPERATOR)
    clock = Clock()
    admin_access = service(session, clock)
    opened = await admin_access.open_break_glass(operator.id, ttl=TTL)

    found = await admin_access.active_session(OPERATOR)

    assert found is not None and found.id == opened.session.id
    assert await admin_access.count_active() == 1


async def test_a_second_session_supersedes_the_first_and_both_stay_on_record(
    session: AsyncSession,
) -> None:
    operator = await make_user(session, telegram_id=OPERATOR)
    clock = Clock()
    admin_access = service(session, clock)
    first = (await admin_access.open_break_glass(operator.id, ttl=TTL)).session
    clock.advance(timedelta(minutes=5))

    second = await admin_access.open_break_glass(operator.id, ttl=TTL)

    assert second.superseded == 1
    assert first.revoked_at == clock() and not first.is_active(clock())
    active = await admin_access.active_session(OPERATOR)
    assert active is not None and active.id == second.session.id
    assert await admin_access.count_active() == 1
    assert [a.id for a in await admin_access.history(operator.id)] == [second.session.id, first.id]


# ======================================================= expiration


async def test_an_expired_session_grants_nothing(session: AsyncSession) -> None:
    operator = await make_user(session, telegram_id=OPERATOR)
    clock = Clock()
    admin_access = service(session, clock)
    access = (await admin_access.open_break_glass(operator.id, ttl=TTL)).session

    clock.advance(TTL - timedelta(seconds=1))
    assert access.is_active(clock())
    assert await admin_access.active_session(OPERATOR) is not None

    clock.advance(timedelta(seconds=1))  # exactly expires_at: no longer active
    assert not access.is_active(clock())
    assert await admin_access.active_session(OPERATOR) is None
    assert await admin_access.count_active() == 0

    # Expiry is not a write: the row is still there, unrevoked, for the record.
    assert access.revoked_at is None
    assert [a.id for a in await admin_access.history(operator.id)] == [access.id]


async def test_an_expired_session_is_not_revived_by_the_clock_running_backwards(
    session: AsyncSession,
) -> None:
    """A later, shorter session does not lean on an older one with a later expiry."""
    operator = await make_user(session, telegram_id=OPERATOR)
    clock = Clock()
    admin_access = service(session, clock)
    long = (await admin_access.open_break_glass(operator.id, ttl=timedelta(hours=2))).session
    clock.advance(timedelta(minutes=1))
    short = (await admin_access.open_break_glass(operator.id, ttl=MIN_SESSION_TTL)).session

    clock.advance(MIN_SESSION_TTL)

    assert long.is_revoked and not short.is_active(clock())
    assert await admin_access.active_session(OPERATOR) is None


async def test_the_row_read_back_from_the_database_still_knows_whether_it_is_active(
    session: AsyncSession,
) -> None:
    """SQLite hands timestamps back naive; the comparison must not care."""
    operator = await make_user(session, telegram_id=OPERATOR)
    clock = Clock()
    access = (await service(session, clock).open_break_glass(operator.id, ttl=TTL)).session

    await session.refresh(access)  # every column re-read from the database

    assert access.is_active(clock())
    clock.advance(TTL)
    assert not access.is_active(clock())
    assert as_utc(access.expires_at) == START + TTL


# ======================================================= revocation


async def test_revoking_ends_the_session_at_once(session: AsyncSession) -> None:
    operator = await make_user(session, telegram_id=OPERATOR)
    clock = Clock()
    admin_access = service(session, clock)
    access = (await admin_access.open_break_glass(operator.id, ttl=TTL)).session
    clock.advance(timedelta(minutes=10))

    assert await admin_access.revoke(access) is True

    assert access.revoked_at == clock() and access.is_revoked
    assert not access.is_active(clock())
    assert await admin_access.active_session(OPERATOR) is None
    assert await admin_access.count_active() == 0


async def test_revocation_is_final(session: AsyncSession) -> None:
    operator = await make_user(session, telegram_id=OPERATOR)
    clock = Clock()
    admin_access = service(session, clock)
    access = (await admin_access.open_break_glass(operator.id, ttl=TTL)).session
    await admin_access.revoke(access)
    revoked_at = access.revoked_at
    clock.advance(timedelta(minutes=1))

    assert await admin_access.revoke(access) is False
    assert access.revoked_at == revoked_at

    with pytest.raises(ValueError, match="never changes"):
        access.revoked_at = None
    with pytest.raises(ValueError, match="never changes"):
        access.revoked_at = clock()
    assert access.revoked_at == revoked_at


async def test_revoke_all_ends_only_that_users_sessions(session: AsyncSession) -> None:
    operator = await make_user(session, telegram_id=OPERATOR)
    bystander = await make_user(session, telegram_id=BYSTANDER)
    clock = Clock()
    admin_access = service(session, clock)
    await admin_access.open_break_glass(operator.id, ttl=TTL)
    await admin_access.open_break_glass(bystander.id, ttl=TTL)

    assert await admin_access.revoke_all(operator.id) == 1
    assert await admin_access.revoke_all(operator.id) == 0

    assert await admin_access.active_session(OPERATOR) is None
    assert await admin_access.active_session(BYSTANDER) is not None


# ======================================================= ownership


async def test_a_session_grants_nothing_to_anyone_else(session: AsyncSession) -> None:
    operator = await make_user(session, telegram_id=OPERATOR)
    await make_user(session, telegram_id=BYSTANDER)
    clock = Clock()
    admin_access = service(session, clock)
    await admin_access.open_break_glass(operator.id, ttl=TTL)

    assert await admin_access.active_session(BYSTANDER) is None
    assert await admin_access.active_session(OPERATOR + 1) is None  # nobody at all
    assert await admin_access.history(BYSTANDER) == []


async def test_a_session_needs_a_registered_user(session: AsyncSession) -> None:
    admin_access = service(session, Clock())

    with pytest.raises(LookupError):
        await admin_access.open_break_glass(424242, ttl=TTL)

    assert await admin_access.count_active() == 0
    assert (await session.scalars(select(AdminAccessSession))).first() is None


def test_the_table_points_at_users_and_never_lets_go() -> None:
    table = AdminAccessSession.__table__
    foreign_keys = [c for c in table.constraints if isinstance(c, ForeignKeyConstraint)]
    assert len(foreign_keys) == 1
    (fk,) = foreign_keys
    assert [c.name for c in fk.columns] == ["user_id"]
    assert fk.referred_table.name == "users"
    assert fk.ondelete == "RESTRICT"
    assert table.c.user_id.nullable is False


async def test_a_session_never_changes_hands(session: AsyncSession) -> None:
    operator = await make_user(session, telegram_id=OPERATOR)
    bystander = await make_user(session, telegram_id=BYSTANDER)
    access = (await service(session, Clock()).open_break_glass(operator.id, ttl=TTL)).session

    with pytest.raises(ValueError, match="never changes"):
        access.user_id = bystander.id
    with pytest.raises(ValueError, match="never changes"):
        access.expires_at = access.expires_at + timedelta(hours=1)
    assert (access.user_id, access.expires_at) == (operator.id, START + TTL)


# ======================================================= bounds and refusals


@pytest.mark.parametrize(
    "ttl",
    [
        timedelta(0),
        timedelta(seconds=-1),
        MIN_SESSION_TTL - timedelta(seconds=1),
        MAX_SESSION_TTL + timedelta(seconds=1),
        timedelta(days=365),
    ],
    ids=["zero", "negative", "under minimum", "over maximum", "a year"],
)
async def test_a_lifetime_outside_the_bounds_is_refused_before_writing(
    session: AsyncSession, ttl: timedelta
) -> None:
    operator = await make_user(session, telegram_id=OPERATOR)
    admin_access = service(session, Clock())

    with pytest.raises(InvalidSessionTtlError):
        await admin_access.open_break_glass(operator.id, ttl=ttl)

    assert issubclass(InvalidSessionTtlError, AdminAccessError)
    assert issubclass(AdminAccessError, ValueError)
    assert await admin_access.history(operator.id) == []


@pytest.mark.parametrize("ttl", [MIN_SESSION_TTL, MAX_SESSION_TTL])
async def test_the_bounds_themselves_are_allowed(session: AsyncSession, ttl: timedelta) -> None:
    operator = await make_user(session, telegram_id=OPERATOR)
    clock = Clock()
    opened = await service(session, clock).open_break_glass(operator.id, ttl=ttl)
    assert opened.session.expires_at == START + ttl


# ======================================================= what the schema itself refuses


async def _rejected(session: AsyncSession, row: AdminAccessSession) -> bool:
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        return True
    return False


async def test_the_database_refuses_a_session_that_expires_before_it_starts(
    session: AsyncSession,
) -> None:
    operator = await make_user(session, telegram_id=OPERATOR)
    for expires_at in (START, START - timedelta(minutes=1)):
        assert await _rejected(
            session,
            AdminAccessSession(
                user_id=operator.id,
                auth_method=AdminAccessMethod.BREAK_GLASS,
                created_at=START,
                expires_at=expires_at,
            ),
        )
    assert not await _rejected(
        session,
        AdminAccessSession(
            user_id=operator.id,
            auth_method=AdminAccessMethod.BREAK_GLASS,
            created_at=START,
            expires_at=START + timedelta(minutes=1),
        ),
    )


async def test_the_database_refuses_a_revocation_before_creation(session: AsyncSession) -> None:
    operator = await make_user(session, telegram_id=OPERATOR)
    assert await _rejected(
        session,
        AdminAccessSession(
            user_id=operator.id,
            auth_method=AdminAccessMethod.BREAK_GLASS,
            created_at=START,
            expires_at=START + TTL,
            revoked_at=START - timedelta(seconds=1),
        ),
    )


def test_the_table_stores_no_credential() -> None:
    """Only the grant itself is recorded: never a password, a hash or a token."""
    columns = {c.name for c in AdminAccessSession.__table__.columns}
    assert columns == {"id", "user_id", "auth_method", "created_at", "expires_at", "revoked_at"}
    assert not any(word in name for name in columns for word in ("pass", "hash", "secret", "token"))


def test_the_active_session_lookup_has_its_index() -> None:
    indexes = {i.name: i for i in AdminAccessSession.__table__.indexes}
    index = indexes["ix_admin_access_sessions_user_id_expires_at"]
    assert [c.name for c in index.columns] == ["user_id", "expires_at"]
    assert str(index.dialect_options["postgresql"]["where"]) == "revoked_at IS NULL"


# ======================================================= the migration matches the model


def test_the_migration_creates_exactly_the_models_constraints() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    table = AdminAccessSession.__table__
    for constraint in table.constraints:
        if isinstance(constraint, CheckConstraint):
            assert str(constraint.sqltext) in source, constraint.name
            assert constraint.name in source
    assert '"admin_access_sessions"' in source
    assert 'down_revision: str | None = "c5d2e8f1a6b3"' in source
    assert "ix_admin_access_sessions_user_id_expires_at" in source


def test_the_downgrade_is_guarded() -> None:
    module = ast.parse(MIGRATION.read_text(encoding="utf-8"))
    downgrade = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "downgrade"
    )
    first = downgrade.body[0]
    assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)
    assert isinstance(first.value.func, ast.Name)
    assert first.value.func.id == "_refuse_to_forget_admin_access_history"
