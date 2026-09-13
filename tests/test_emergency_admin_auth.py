"""
Emergency admin authentication — the service, its settings, and its record.

No Telegram command exists yet. These tests pin what the backend promises on its
own: the secret is only ever a hash, an empty or wrong password is a recorded
failure, repeated failures lock the user out for the configured window, a
success opens exactly one bounded session, and neither the password nor the
hash ever reaches a log line, a result or an exception.
"""

from __future__ import annotations

import ast
import logging
import pathlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import CheckConstraint, ForeignKeyConstraint
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.admin_access import AdminAccessAttempt
from app.models.enums import AdminAccessAttemptOutcome, AdminAccessMethod
from app.security.admin import is_admin_id
from app.services.admin_access import MAX_SESSION_TTL, AdminAccessService
from app.services.emergency_admin import (
    AuthenticationResult,
    DenialReason,
    EmergencyAccessPolicy,
    EmergencyAdminAuthService,
)
from app.utils.passwords import MIN_LOG_N, hash_password
from tests.factories import make_user
from tests.production_bot import tree_settings

ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVICE = ROOT / "app" / "services" / "emergency_admin.py"
MIGRATION = ROOT / "alembic" / "versions" / "e8b2c4d6f1a3_admin_access_attempts.py"

PASSWORD = "correct horse battery staple"
HASH = hash_password(PASSWORD, log_n=MIN_LOG_N)  # cheap parameters: same code path
START = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
OPERATOR, BYSTANDER = 9701, 9702


class Clock:
    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class CountingVerifier:
    """The real check, counting how often it ran and on which thread."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, password: str, encoded: str) -> bool:
        from app.utils.passwords import verify_password

        self.calls += 1
        return verify_password(password, encoded)


def settings_with(**overrides: Any) -> Settings:
    return tree_settings(**({"emergency_admin_password_hash": HASH} | overrides))


def policy(**overrides: Any) -> EmergencyAccessPolicy:
    return EmergencyAccessPolicy.from_settings(settings_with(**overrides))


def auth(
    session: AsyncSession,
    clock: Clock,
    *,
    verifier: CountingVerifier | None = None,
    **overrides: Any,
) -> EmergencyAdminAuthService:
    kwargs: dict[str, Any] = {"clock": clock}
    if verifier is not None:
        kwargs["verify"] = verifier
    return EmergencyAdminAuthService(session, policy(**overrides), **kwargs)


async def attempts(session: AsyncSession, user_id: int) -> list[str]:
    """Outcomes, oldest first."""
    rows = await EmergencyAdminAuthService(session, policy()).attempts.list_for_user(user_id)
    return [row.outcome.value for row in reversed(rows)]


# ======================================================= settings


def test_the_secret_is_configured_as_a_hash_and_never_shown() -> None:
    settings = settings_with()
    secret = settings.emergency_admin_password_hash
    assert isinstance(secret, SecretStr)
    assert settings.emergency_admin_enabled
    assert HASH not in repr(settings) and HASH not in str(secret) and HASH not in repr(secret)
    assert PASSWORD not in repr(settings)
    assert secret.get_secret_value() == HASH


@pytest.mark.parametrize("value", [None, "", "   "])
def test_unset_or_blank_means_the_feature_is_off(value: str | None) -> None:
    settings = settings_with(emergency_admin_password_hash=value)
    assert settings.emergency_admin_password_hash is None
    assert not settings.emergency_admin_enabled
    assert not EmergencyAccessPolicy.from_settings(settings).enabled


@pytest.mark.parametrize("value", [PASSWORD, "$2b$12$notours", "scrypt:ln=99,r=8,p=1:AAAA:AAAA"])
def test_a_plaintext_or_malformed_hash_stops_the_process(value: str) -> None:
    """Better a loud refusal to boot than a break-glass that refuses everyone later."""
    with pytest.raises(ValidationError) as refused:
        settings_with(emergency_admin_password_hash=value)
    assert value not in str(refused.value).replace(repr(value), "")


def test_the_defaults_are_short_and_the_bounds_match_the_session_service() -> None:
    settings = settings_with()
    assert settings.emergency_admin_session_ttl_minutes == 30
    assert settings.emergency_admin_max_failed_attempts == 5
    assert settings.emergency_admin_lockout_minutes == 15
    ttl_field = Settings.model_fields["emergency_admin_session_ttl_minutes"]
    upper = next(getattr(m, "le", None) for m in ttl_field.metadata if hasattr(m, "le"))
    assert timedelta(minutes=upper) == MAX_SESSION_TTL


@pytest.mark.parametrize(
    "overrides",
    [
        {"emergency_admin_session_ttl_minutes": 0},
        {"emergency_admin_session_ttl_minutes": 721},
        {"emergency_admin_max_failed_attempts": 0},
        {"emergency_admin_lockout_minutes": 0},
    ],
    ids=["ttl zero", "ttl over a day's half", "no attempts", "no lockout"],
)
def test_out_of_range_limits_are_refused_at_startup(overrides: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        settings_with(**overrides)


# ======================================================= denials


async def test_when_the_feature_is_off_nothing_is_checked_or_recorded(
    session: AsyncSession,
) -> None:
    operator = await make_user(session, telegram_id=OPERATOR)
    verifier = CountingVerifier()
    service = auth(session, Clock(), verifier=verifier, emergency_admin_password_hash=None)
    assert not service.enabled

    result = await service.authenticate(operator.id, PASSWORD)

    assert result == AuthenticationResult.denied(DenialReason.NOT_CONFIGURED)
    assert verifier.calls == 0
    assert await attempts(session, operator.id) == []


@pytest.mark.parametrize("password", ["", "   ", "\n"], ids=["empty", "spaces", "newline"])
async def test_an_empty_credential_is_a_recorded_failure_that_is_never_hashed(
    session: AsyncSession, password: str
) -> None:
    operator = await make_user(session, telegram_id=OPERATOR)
    verifier = CountingVerifier()
    service = auth(session, Clock(), verifier=verifier)

    result = await service.authenticate(operator.id, password)

    assert result == AuthenticationResult.denied(DenialReason.EMPTY_PASSWORD)
    assert verifier.calls == 0
    assert await attempts(session, operator.id) == ["failed"]
    assert await service.access.active_session(OPERATOR) is None


async def test_a_wrong_password_is_a_recorded_failure(session: AsyncSession) -> None:
    operator = await make_user(session, telegram_id=OPERATOR)
    verifier = CountingVerifier()
    service = auth(session, Clock(), verifier=verifier)

    result = await service.authenticate(operator.id, PASSWORD + "!")

    assert result == AuthenticationResult.denied(DenialReason.WRONG_PASSWORD)
    assert verifier.calls == 1
    assert await attempts(session, operator.id) == ["failed"]
    assert await service.access.active_session(OPERATOR) is None


async def test_an_unregistered_user_is_a_caller_error(session: AsyncSession) -> None:
    with pytest.raises(LookupError):
        await auth(session, Clock()).authenticate(424242, PASSWORD)


# ======================================================= success


async def test_the_right_password_opens_one_session_for_the_configured_time(
    session: AsyncSession,
) -> None:
    operator = await make_user(session, telegram_id=OPERATOR)
    clock = Clock()
    service = auth(session, clock, emergency_admin_session_ttl_minutes=45)

    result = await service.authenticate(operator.id, PASSWORD)

    assert result.granted and result.reason is None and result.superseded == 0
    assert result.session is not None
    assert result.session.user_id == operator.id
    assert result.session.auth_method == AdminAccessMethod.BREAK_GLASS
    assert result.session.expires_at == START + timedelta(minutes=45)
    active = await service.access.active_session(OPERATOR)
    assert active is not None and active.id == result.session.id
    assert await attempts(session, operator.id) == ["succeeded"]


async def test_authenticating_again_replaces_the_session_and_never_accumulates_rights(
    session: AsyncSession,
) -> None:
    operator = await make_user(session, telegram_id=OPERATOR)
    clock = Clock()
    service = auth(session, clock)
    settings = settings_with()
    first = await service.authenticate(operator.id, PASSWORD)
    clock.advance(timedelta(minutes=10))

    second = await service.authenticate(operator.id, PASSWORD)
    clock.advance(timedelta(minutes=10))
    third = await service.authenticate(operator.id, PASSWORD)

    assert (second.superseded, third.superseded) == (1, 1)
    assert first.session is not None and second.session is not None and third.session is not None
    assert first.session.is_revoked and second.session.is_revoked
    assert await service.access.count_active() == 1
    assert third.session.expires_at == clock() + timedelta(minutes=30)
    # Still the same allow-list: a session is not membership.
    assert settings.admin_ids == [tree_settings().admin_ids[0]]
    assert not is_admin_id(OPERATOR, settings)
    history = await AdminAccessService(session, clock=clock).history(operator.id)
    assert [a.id for a in history] == [third.session.id, second.session.id, first.session.id]


# ======================================================= lockout


async def test_repeated_failures_lock_the_user_out_for_the_window(session: AsyncSession) -> None:
    operator = await make_user(session, telegram_id=OPERATOR)
    clock = Clock()
    verifier = CountingVerifier()
    service = auth(
        session,
        clock,
        verifier=verifier,
        emergency_admin_max_failed_attempts=3,
        emergency_admin_lockout_minutes=10,
    )

    for _ in range(3):
        assert not (await service.authenticate(operator.id, "wrong")).granted
        clock.advance(timedelta(minutes=1))
    assert await service.is_locked_out(operator.id)
    checks = verifier.calls

    # Locked: the right password is refused unchecked, and that is recorded too.
    locked = await service.authenticate(operator.id, PASSWORD)
    assert locked == AuthenticationResult.denied(DenialReason.LOCKED_OUT)
    assert verifier.calls == checks
    assert await attempts(session, operator.id) == ["failed"] * 3 + ["locked_out"]

    # A locked-out attempt does not extend the lockout: the window runs from the
    # failures, and a failure counts until exactly the lockout after it.
    clock.advance(timedelta(minutes=10) - timedelta(minutes=3))
    assert await service.is_locked_out(operator.id)
    clock.advance(timedelta(seconds=1))  # the oldest failure leaves the window
    assert not await service.is_locked_out(operator.id)

    result = await service.authenticate(operator.id, PASSWORD)
    assert result.granted
    assert await attempts(session, operator.id) == ["failed"] * 3 + ["locked_out", "succeeded"]


async def test_one_failure_short_of_the_limit_still_lets_the_right_password_in(
    session: AsyncSession,
) -> None:
    operator = await make_user(session, telegram_id=OPERATOR)
    service = auth(session, Clock(), emergency_admin_max_failed_attempts=3)
    for _ in range(2):
        await service.authenticate(operator.id, "wrong")

    assert not await service.is_locked_out(operator.id)
    assert (await service.authenticate(operator.id, PASSWORD)).granted


async def test_a_success_starts_the_failure_count_afresh(session: AsyncSession) -> None:
    operator = await make_user(session, telegram_id=OPERATOR)
    service = auth(session, Clock(), emergency_admin_max_failed_attempts=3)
    await service.authenticate(operator.id, "wrong")
    await service.authenticate(operator.id, "wrong")
    assert (await service.authenticate(operator.id, PASSWORD)).granted

    await service.authenticate(operator.id, "wrong")
    await service.authenticate(operator.id, "wrong")

    assert not await service.is_locked_out(operator.id)
    assert (await service.authenticate(operator.id, PASSWORD)).granted


async def test_a_lockout_is_per_user(session: AsyncSession) -> None:
    operator = await make_user(session, telegram_id=OPERATOR)
    bystander = await make_user(session, telegram_id=BYSTANDER)
    service = auth(session, Clock(), emergency_admin_max_failed_attempts=2)
    for _ in range(2):
        await service.authenticate(operator.id, "wrong")

    assert await service.is_locked_out(operator.id)
    assert not await service.is_locked_out(bystander.id)
    assert (await service.authenticate(bystander.id, PASSWORD)).granted
    assert await attempts(session, bystander.id) == ["succeeded"]


async def test_empty_credentials_count_towards_the_lockout(session: AsyncSession) -> None:
    operator = await make_user(session, telegram_id=OPERATOR)
    service = auth(session, Clock(), emergency_admin_max_failed_attempts=2)
    await service.authenticate(operator.id, "")
    await service.authenticate(operator.id, " ")
    assert await service.is_locked_out(operator.id)


# ======================================================= nothing leaks


async def test_neither_the_password_nor_the_hash_reaches_the_log(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    operator = await make_user(session, telegram_id=OPERATOR)
    service = auth(session, Clock(), emergency_admin_max_failed_attempts=1)
    with caplog.at_level(logging.DEBUG):
        results = [
            await service.authenticate(operator.id, PASSWORD + "?"),
            await service.authenticate(operator.id, PASSWORD),  # locked out now
        ]
        service = auth(session, Clock(START + timedelta(hours=1)))
        results.append(await service.authenticate(operator.id, PASSWORD))

    assert [r.granted for r in results] == [False, False, True]
    text = caplog.text
    assert PASSWORD not in text and HASH not in text
    assert "user_id=" in text and "reason=" in text and "session_id=" in text
    for result in results:
        assert PASSWORD not in repr(result) and HASH not in repr(result)


def test_the_service_never_formats_the_password_or_the_hash_into_a_log_line() -> None:
    module = ast.parse(SERVICE.read_text(encoding="utf-8"))
    for node in ast.walk(module):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in {"debug", "info", "warning", "error", "exception", "critical"}:
            continue
        names = {n.id for arg in node.args for n in ast.walk(arg) if isinstance(n, ast.Name)}
        attrs = {n.attr for arg in node.args for n in ast.walk(arg) if isinstance(n, ast.Attribute)}
        assert not names & {"password", "encoded"}, ast.dump(node)
        assert not attrs & {"password_hash", "get_secret_value"}, ast.dump(node)


def test_the_policy_keeps_the_hash_secret() -> None:
    assert HASH not in repr(policy())


async def test_verification_runs_off_the_event_loop(session: AsyncSession) -> None:
    operator = await make_user(session, telegram_id=OPERATOR)
    ran: list[tuple[str, str]] = []

    async def runner(verify: Any, password: str, encoded: str) -> bool:
        ran.append((password, encoded))
        return True

    service = EmergencyAdminAuthService(session, policy(), clock=Clock(), run=runner)
    assert (await service.authenticate(operator.id, PASSWORD)).granted
    assert ran == [(PASSWORD, HASH)]


async def test_the_default_runner_uses_a_worker_thread(session: AsyncSession) -> None:
    import threading

    operator = await make_user(session, telegram_id=OPERATOR)
    threads: list[int] = []

    def verify(password: str, encoded: str) -> bool:
        threads.append(threading.get_ident())
        return True

    service = EmergencyAdminAuthService(session, policy(), clock=Clock(), verify=verify)
    assert (await service.authenticate(operator.id, PASSWORD)).granted
    assert threads and threads[0] != threading.get_ident()


# ======================================================= the record


def test_the_attempts_table_stores_no_credential_and_points_at_users() -> None:
    table = AdminAccessAttempt.__table__
    assert {c.name for c in table.columns} == {
        "id",
        "user_id",
        "auth_method",
        "outcome",
        "created_at",
    }
    assert not any(
        w in n for n in {c.name for c in table.columns} for w in ("pass", "hash", "secret")
    )
    (fk,) = [c for c in table.constraints if isinstance(c, ForeignKeyConstraint)]
    assert fk.referred_table.name == "users" and fk.ondelete == "RESTRICT"
    assert table.c.user_id.nullable is False
    index = next(
        i for i in table.indexes if i.name == "ix_admin_access_attempts_user_id_created_at"
    )
    assert [c.name for c in index.columns] == ["user_id", "created_at"]


async def test_an_attempt_is_a_fact_that_never_changes(session: AsyncSession) -> None:
    operator = await make_user(session, telegram_id=OPERATOR)
    await auth(session, Clock()).authenticate(operator.id, "wrong")
    (row,) = await EmergencyAdminAuthService(session, policy()).attempts.list_for_user(operator.id)
    with pytest.raises(ValueError, match="never changes"):
        row.outcome = AdminAccessAttemptOutcome.SUCCEEDED
    assert row.outcome == AdminAccessAttemptOutcome.FAILED


def test_the_migration_matches_the_model_and_guards_its_downgrade() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    table = AdminAccessAttempt.__table__
    assert not [c for c in table.constraints if isinstance(c, CheckConstraint)]
    assert '"admin_access_attempts"' in source
    assert 'down_revision: str | None = "d7a3f9c2e8b1"' in source
    assert "ix_admin_access_attempts_user_id_created_at" in source
    for value in AdminAccessAttemptOutcome:
        assert f'"{value.value}"' in source
    module = ast.parse(source)
    downgrade = next(
        n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == "downgrade"
    )
    first = downgrade.body[0]
    assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)
    assert isinstance(first.value.func, ast.Name)
    assert first.value.func.id == "_refuse_to_forget_login_attempts"


def test_only_the_service_layer_verifies_passwords() -> None:
    """Handlers and middlewares never touch the hash or the verifier."""
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in sorted((ROOT / "app").rglob("*.py"))
        if not path.as_posix().endswith(
            ("app/services/emergency_admin.py", "app/utils/passwords.py")
        )
        and (
            "verify_password" in path.read_text(encoding="utf-8")
            or "hashlib.scrypt" in path.read_text(encoding="utf-8")
        )
    ]
    assert offenders == []
