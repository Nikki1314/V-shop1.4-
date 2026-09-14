"""Temporary admin sessions: open one, find the active one, end it.

This is the persistence half of break-glass access. It decides nothing about
*whether* someone may have a session — no password is checked here, and no
handler or filter consults it yet. It only keeps the sessions honest:

* a session is opened for a registered user, for a bounded time, from one clock;
* the active-session question — the one authorization will ask — is answered
  in one place, :meth:`AdminAccessService.active_session`;
* revocation is a timestamp, never a delete, so every grant stays on record.

Nothing here commits: the caller owns the unit of work.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.admin_access import AdminAccessSession
from app.models.enums import AdminAccessMethod
from app.repositories.admin_access_session import AdminAccessSessionRepository
from app.repositories.user import UserRepository

# Bounds on a session's life. The configured TTL (a later step) must fit them;
# they exist so no caller can open an effectively permanent session by mistake.
MIN_SESSION_TTL = timedelta(minutes=1)
MAX_SESSION_TTL = timedelta(hours=12)

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


class AdminAccessError(ValueError):
    """A session request refused before anything was written."""


class InvalidSessionTtlError(AdminAccessError):
    """The requested lifetime is not between :data:`MIN_SESSION_TTL` and :data:`MAX_SESSION_TTL`."""


@dataclass(frozen=True, slots=True)
class OpenedSession:
    """The new session, and how many active ones it replaced."""

    session: AdminAccessSession
    superseded: int


class AdminAccessService:
    def __init__(self, session: AsyncSession, *, clock: Clock = utc_now) -> None:
        self.session = session
        self.sessions = AdminAccessSessionRepository(session)
        self.users = UserRepository(session)
        self._clock = clock

    def now(self) -> datetime:
        return self._clock()

    # --- opening ------------------------------------------------------------------

    async def open_break_glass(self, user_id: int, *, ttl: timedelta) -> OpenedSession:
        """
        Grant ``user_id`` admin rights for ``ttl`` from now.

        A user holds at most one active session: any still active is revoked
        first, so re-authenticating restarts the clock and the audit trail shows
        both. Refuses a lifetime outside the bounds (:class:`InvalidSessionTtlError`)
        before writing anything. An unregistered user is a caller error
        (:class:`LookupError`): every handler registers its sender first.
        """
        if not isinstance(ttl, timedelta) or not MIN_SESSION_TTL <= ttl <= MAX_SESSION_TTL:
            raise InvalidSessionTtlError(
                f"A session lasts between {MIN_SESSION_TTL} and {MAX_SESSION_TTL}, not {ttl!r}"
            )
        if await self.users.get_by_id(user_id) is None:
            raise LookupError(f"User {user_id} does not exist")

        now = self.now()
        superseded = await self.revoke_all(user_id, now=now)
        opened = await self.sessions.open(
            user_id,
            auth_method=AdminAccessMethod.BREAK_GLASS,
            created_at=now,
            expires_at=now + ttl,
        )
        return OpenedSession(opened, superseded)

    # --- the authorization question -------------------------------------------

    async def active_session(self, telegram_id: int) -> AdminAccessSession | None:
        """The session granting ``telegram_id`` admin rights right now, or ``None``."""
        return await self.sessions.get_active_for_telegram_id(telegram_id, now=self.now())

    async def count_active(self) -> int:
        return await self.sessions.count_active(now=self.now())

    # --- ending ---------------------------------------------------------------------

    async def revoke(self, access: AdminAccessSession) -> bool:
        """End ``access`` now. ``True`` when this call ended it; a second call changes nothing."""
        return await self.sessions.revoke(access, at=self.now())

    async def revoke_all(self, user_id: int, *, now: datetime | None = None) -> int:
        """End every active session of ``user_id``; returns how many there were."""
        at = now if now is not None else self.now()
        active = await self.sessions.list_active_for_user(user_id, now=at)
        for access in active:
            await self.sessions.revoke(access, at=at)
        return len(active)

    async def revoke_every_active(self) -> int:
        """End every active session of every user; returns how many there were."""
        at = self.now()
        active = await self.sessions.list_active(now=at)
        for access in active:
            await self.sessions.revoke(access, at=at)
        return len(active)

    # --- audit ------------------------------------------------------------------

    async def history(self, user_id: int) -> list[AdminAccessSession]:
        """Every session the user ever held, newest first."""
        return await self.sessions.list_for_user(user_id)
