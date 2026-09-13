"""Block non-admin users from admin router handlers."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User

from app.config import Settings, get_settings
from app.security.admin import AdminGrant, resolve_admin_grant

logger = logging.getLogger(__name__)


class AdminOnlyMiddleware(BaseMiddleware):
    """
    Hard gate for the admin router.

    Unauthorized updates are dropped silently so non-admins never reach
    admin handlers or see admin UI. Normally the router's ``IsAdmin`` filter has
    already decided and left ``admin_grant`` in the data; if it has not (the
    filter removed, or a router mounted without it) the decision is made here,
    by the same function. Injects ``is_admin=True`` and ``admin_grant``.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        settings: Settings = data.get("settings") or self._settings or get_settings()
        user: User | None = data.get("event_from_user")

        grant: AdminGrant | None = data.get("admin_grant")
        if grant is None or (user is not None and grant.telegram_id != user.id):
            grant = await resolve_admin_grant(user, settings, data.get("session"))
        if grant is None:
            user_id = user.id if user is not None else None
            logger.warning("Blocked non-admin access to admin router user_id=%s", user_id)
            return None

        data["is_admin"] = True
        data["admin_grant"] = grant
        return await handler(event, data)
