"""Admin access filters — ADMIN_IDS or an active break-glass session."""

from __future__ import annotations

from typing import Any

from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.security.admin import AdminGrant, resolve_admin_grant


def _sender(event: TelegramObject) -> Any:
    user = getattr(event, "from_user", None)
    if user is None and isinstance(event, (Message, CallbackQuery)):
        user = event.from_user
    return user


class IsAdmin(BaseFilter):
    """
    Allow configured admins and holders of an active emergency session.

    ``session`` and ``settings`` arrive from the middleware data. On a pass the
    filter hands the decision on as ``admin_grant``, so the middleware behind it
    and the handler after that never ask the database a second time.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings

    async def __call__(
        self,
        event: TelegramObject,
        settings: Settings | None = None,
        session: AsyncSession | None = None,
        admin_grant: AdminGrant | None = None,
    ) -> bool | dict[str, Any]:
        cfg = settings or self._settings or get_settings()
        sender = _sender(event)
        # A grant already in the data is reused only if it names this sender:
        # whatever put it there, it must not vouch for anyone else.
        if admin_grant is None or sender is None or admin_grant.telegram_id != sender.id:
            admin_grant = await resolve_admin_grant(sender, cfg, session)
        if admin_grant is None:
            return False
        return {"admin_grant": admin_grant}


class IsNotAdmin(BaseFilter):
    """Inverse of :class:`IsAdmin` (used for access-denied replies)."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings

    async def __call__(
        self,
        event: TelegramObject,
        settings: Settings | None = None,
        session: AsyncSession | None = None,
        admin_grant: AdminGrant | None = None,
    ) -> bool:
        admin_filter = IsAdmin(self._settings)
        return not await admin_filter(
            event, settings=settings, session=session, admin_grant=admin_grant
        )
