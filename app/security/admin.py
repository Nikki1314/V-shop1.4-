"""Admin authorization: who may use the admin router, decided in one place.

Two ways in, one answer::

    authorized = configured admin (ADMIN_IDS)
                 OR an active break-glass session (admin_access_sessions)

:func:`resolve_admin_grant` is that answer. The admin router's ``IsAdmin`` filter
and ``AdminOnlyMiddleware`` both call it, the user router's ``IsNotAdmin`` negates
it, and no handler asks again. A configured admin is decided from settings
alone, without touching the database — exactly as before sessions existed. Anyone
else is granted only by a session that is neither revoked nor expired, read from
the database on every update: revocation and expiry take effect on the next
message, never later.

A grant is not membership. It says how this update was authorized, so a handler
can record it (an audit row names the session), and nothing more: ``ADMIN_IDS``
is never read back changed, and no session ever becomes a configured admin.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from aiogram.types import User
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models.enums import AdminAccessKind
from app.services.admin_access import AdminAccessService, Clock, utc_now

__all__ = [
    "AdminAccessKind",
    "AdminGrant",
    "is_admin_id",
    "is_admin_user",
    "resolve_admin_grant",
]


def is_admin_id(telegram_id: int | None, settings: Settings | None = None) -> bool:
    """Return True when ``telegram_id`` is listed in ADMIN_IDS."""
    if telegram_id is None:
        return False
    cfg = settings or get_settings()
    return telegram_id in cfg.admin_ids


def is_admin_user(user: User | None, settings: Settings | None = None) -> bool:
    """Return True when the Telegram user is a *configured* admin (ADMIN_IDS only)."""
    if user is None:
        return False
    return is_admin_id(user.id, settings)


@dataclass(frozen=True, slots=True)
class AdminGrant:
    """Why this update may use the admin router. Injected into handlers as ``admin_grant``."""

    telegram_id: int
    kind: AdminAccessKind
    session_id: int | None = None
    expires_at: datetime | None = None

    @property
    def is_break_glass(self) -> bool:
        return self.kind == AdminAccessKind.BREAK_GLASS


async def resolve_admin_grant(
    user: User | None,
    settings: Settings,
    session: AsyncSession | None,
    *,
    clock: Clock = utc_now,
) -> AdminGrant | None:
    """
    The one authorization decision for the admin router.

    Configured admins need no database. Everyone else needs an active session,
    which needs a database session to read — without one (a path that runs
    before ``DatabaseMiddleware``) the answer is *no*: it fails closed. With
    emergency access switched off (no ``EMERGENCY_ADMIN_PASSWORD_HASH``) no
    session counts, whatever the table holds: unsetting the hash is the kill
    switch, effective on the next update.
    """
    if user is None:
        return None
    if is_admin_id(user.id, settings):
        return AdminGrant(telegram_id=user.id, kind=AdminAccessKind.CONFIGURED)
    if session is None or not settings.emergency_admin_enabled:
        return None
    access = await AdminAccessService(session, clock=clock).active_session(user.id)
    if access is None:
        return None
    return AdminGrant(
        telegram_id=user.id,
        kind=AdminAccessKind.BREAK_GLASS,
        session_id=access.id,
        expires_at=access.expires_at,
    )
