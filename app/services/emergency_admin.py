"""Emergency (break-glass) admin authentication — the decision, not the command.

A registered user offers a password; this service decides whether that opens a
temporary admin session, and records the attempt either way. The handler that
will call it has one job: pass the sender and the text on, then answer every
denial with the same generic text. It never learns *why*.

What the service guarantees:

* the secret exists only as a hash in configuration (``EMERGENCY_ADMIN_PASSWORD_HASH``);
  verification is :func:`~app.utils.passwords.verify_password`, run in a worker
  thread because scrypt is deliberately slow;
* the password never reaches a log line, an exception message or a result;
* an empty password is refused without hashing and counts as a failure;
* after ``EMERGENCY_ADMIN_MAX_FAILED_ATTEMPTS`` failures within
  ``EMERGENCY_ADMIN_LOCKOUT_MINUTES`` the user is locked out for that window —
  further attempts are not checked, right password or not, so a lockout cannot
  be probed and costs no CPU; the first of them is recorded as ``locked_out``
  and the rest are not, so a locked-out account cannot grow the table;
* success opens one session for ``EMERGENCY_ADMIN_SESSION_TTL_MINUTES`` through
  :class:`~app.services.admin_access.AdminAccessService`, which revokes any
  earlier active one: however often someone authenticates, they hold one
  bounded session and never a permanent right. ``ADMIN_IDS`` is never touched.

The user's row is locked for the duration, so two attempts by the same person
are decided one after the other against the real count. Nothing here commits.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.admin_access import AdminAccessSession
from app.models.enums import AdminAccessAttemptOutcome, AdminAccessMethod
from app.repositories.admin_access_attempt import AdminAccessAttemptRepository
from app.repositories.user import UserRepository
from app.services.admin_access import (
    MAX_SESSION_TTL,
    MIN_SESSION_TTL,
    AdminAccessService,
    Clock,
    utc_now,
)
from app.utils.passwords import verify_password

logger = logging.getLogger(__name__)

Verifier = Callable[[str, str], bool]
Runner = Callable[[Verifier, str, str], Awaitable[bool]]


async def in_worker_thread(verify: Verifier, password: str, encoded: str) -> bool:
    """Run the (blocking, memory-hard) verification off the event loop."""
    return await asyncio.to_thread(verify, password, encoded)


@dataclass(frozen=True, slots=True)
class EmergencyAccessPolicy:
    """The configured rules. ``password_hash`` stays a :class:`SecretStr` end to end."""

    password_hash: SecretStr | None
    session_ttl: timedelta
    max_failed_attempts: int
    lockout: timedelta

    def __post_init__(self) -> None:
        if not MIN_SESSION_TTL <= self.session_ttl <= MAX_SESSION_TTL:
            raise ValueError(f"session_ttl must be between {MIN_SESSION_TTL} and {MAX_SESSION_TTL}")
        if self.max_failed_attempts < 1:
            raise ValueError("max_failed_attempts must be at least 1")
        if self.lockout <= timedelta(0):
            raise ValueError("lockout must be positive")

    @classmethod
    def from_settings(cls, settings: Settings) -> EmergencyAccessPolicy:
        return cls(
            password_hash=settings.emergency_admin_password_hash,
            session_ttl=timedelta(minutes=settings.emergency_admin_session_ttl_minutes),
            max_failed_attempts=settings.emergency_admin_max_failed_attempts,
            lockout=timedelta(minutes=settings.emergency_admin_lockout_minutes),
        )

    @property
    def enabled(self) -> bool:
        return self.password_hash is not None


class DenialReason(StrEnum):
    """Why an attempt was denied — for the log and the audit, never for the reply."""

    NOT_CONFIGURED = "not_configured"
    LOCKED_OUT = "locked_out"
    EMPTY_PASSWORD = "empty_password"
    WRONG_PASSWORD = "wrong_password"


@dataclass(frozen=True, slots=True)
class AuthenticationResult:
    """Granted with its session, or denied with the reason. Carries no credential."""

    granted: bool
    reason: DenialReason | None = None
    session: AdminAccessSession | None = None
    superseded: int = 0

    @classmethod
    def denied(cls, reason: DenialReason) -> AuthenticationResult:
        return cls(granted=False, reason=reason)


class EmergencyAdminAuthService:
    def __init__(
        self,
        session: AsyncSession,
        policy: EmergencyAccessPolicy,
        *,
        clock: Clock = utc_now,
        verify: Verifier = verify_password,
        run: Runner = in_worker_thread,
    ) -> None:
        self.session = session
        self.policy = policy
        self.users = UserRepository(session)
        self.attempts = AdminAccessAttemptRepository(session)
        self.access = AdminAccessService(session, clock=clock)
        self._clock = clock
        self._verify = verify
        self._run = run

    @property
    def enabled(self) -> bool:
        return self.policy.enabled

    async def is_locked_out(self, user_id: int) -> bool:
        """Whether the user's next attempt would be refused unchecked."""
        return await self._locked_out(user_id)

    async def authenticate(self, user_id: int, password: str) -> AuthenticationResult:
        """
        Decide one attempt by the registered user ``user_id``.

        An unregistered user is a caller error (:class:`LookupError`): handlers
        register their sender before anything else. Every other outcome is a
        result, never an exception, so the caller cannot leak a reason by accident.
        """
        if not self.policy.enabled:
            return AuthenticationResult.denied(DenialReason.NOT_CONFIGURED)
        if await self.users.get_for_update(user_id) is None:
            raise LookupError(f"User {user_id} does not exist")

        if await self._locked_out(user_id):
            # One row says "kept trying while locked out"; a thousand would only
            # let a locked-out account grow the table without limit.
            latest = await self.attempts.latest_for_user(user_id)
            if latest is None or latest.outcome != AdminAccessAttemptOutcome.LOCKED_OUT:
                await self._record(user_id, AdminAccessAttemptOutcome.LOCKED_OUT)
            return self._deny(user_id, DenialReason.LOCKED_OUT)

        if not isinstance(password, str) or not password.strip():
            await self._record(user_id, AdminAccessAttemptOutcome.FAILED)
            return self._deny(user_id, DenialReason.EMPTY_PASSWORD)

        secret = self.policy.password_hash
        if secret is None:  # pragma: no cover - `enabled` was checked above
            return AuthenticationResult.denied(DenialReason.NOT_CONFIGURED)
        encoded = secret.get_secret_value()
        if not await self._run(self._verify, password, encoded):
            await self._record(user_id, AdminAccessAttemptOutcome.FAILED)
            return self._deny(user_id, DenialReason.WRONG_PASSWORD)

        await self._record(user_id, AdminAccessAttemptOutcome.SUCCEEDED)
        opened = await self.access.open_break_glass(user_id, ttl=self.policy.session_ttl)
        logger.warning(
            "Emergency admin session opened user_id=%s session_id=%s expires_at=%s superseded=%s",
            user_id,
            opened.session.id,
            opened.session.expires_at.isoformat(),
            opened.superseded,
        )
        return AuthenticationResult(
            granted=True, session=opened.session, superseded=opened.superseded
        )

    # --- internals ------------------------------------------------------------------

    async def _locked_out(self, user_id: int) -> bool:
        since = self._clock() - self.policy.lockout
        failures = await self.attempts.count_failures_since(user_id, since=since)
        return failures >= self.policy.max_failed_attempts

    async def _record(self, user_id: int, outcome: AdminAccessAttemptOutcome) -> None:
        await self.attempts.record(
            user_id,
            auth_method=AdminAccessMethod.BREAK_GLASS,
            outcome=outcome,
            created_at=self._clock(),
        )

    @staticmethod
    def _deny(user_id: int, reason: DenialReason) -> AuthenticationResult:
        logger.warning(
            "Emergency admin authentication denied user_id=%s reason=%s", user_id, reason
        )
        return AuthenticationResult.denied(reason)
