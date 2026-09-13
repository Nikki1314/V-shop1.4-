"""/emergency_admin — break-glass entry: ask for the password, check it, open the panel.

Three steps and nothing else. The command asks for the password (and says the
message will be deleted); the next message is handed, unread, to
:class:`~app.services.emergency_admin.EmergencyAdminAuthService`, which decides
and records; a success shows the existing admin panel exactly as ``/admin``
does. Every further tap and button goes through the admin router's own gates,
which find the session this opens. Nothing here performs an admin operation,
and nothing here decides who is an admin.

What the handler promises about the secret: the message carrying it is deleted
from the chat before it is checked (best effort — Telegram may refuse an old
message), its text is never logged, never echoed, never put in a reply, and
every denial — wrong, empty, locked out — is answered with the same text a
non-admin gets from ``/admin``. When the feature is not configured the command
says nothing at all, so nobody can tell whether it exists.

The attempt and the session are committed before any answer: what the operator
is told is durable, and the user-row lock the service takes is released before
Telegram is awaited.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, ReplyKeyboardMarkup, ReplyKeyboardRemove
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.filters.localized_text import LocalizedText
from app.handlers.admin.panel import show_admin_menu
from app.keyboards.admin import admin_cancel_keyboard
from app.keyboards.reply import main_menu_keyboard, remove_keyboard
from app.models.user import User
from app.services.emergency_admin import EmergencyAccessPolicy, EmergencyAdminAuthService
from app.services.localization import LocalizationService
from app.services.user import UserService
from app.states.emergency import EmergencyAccessStates
from app.utils.concurrency import keyed_lock

logger = logging.getLogger(__name__)

router = Router(name="user_emergency_admin")


def _home_keyboard(
    user: User, i18n: LocalizationService
) -> ReplyKeyboardMarkup | ReplyKeyboardRemove:
    """Give back whatever keyboard the person had: the shop menu, or none."""
    return main_menu_keyboard(i18n) if UserService.is_onboarded(user) else remove_keyboard()


async def _discard(message: Message) -> None:
    """Take the credential out of the chat. Best effort: the check goes ahead regardless."""
    try:
        await message.delete()
    except TelegramAPIError:
        logger.debug("Could not delete an emergency password message", exc_info=True)


@router.message(Command("emergency_admin"))
async def start_emergency_access(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
) -> None:
    if message.from_user is None or not settings.emergency_admin_enabled:
        return  # unconfigured: indistinguishable from a command that does not exist
    if command.args:
        # "/emergency_admin <password>": the secret is already in the chat, so it is
        # taken out and checked at once rather than left there while we ask again.
        await _check(message, command.args, session=session, settings=settings, state=state)
        return
    user = await UserService(session).ensure_user(message.from_user)
    i18n = LocalizationService.from_user(user)
    await state.set_state(EmergencyAccessStates.password)
    await message.answer(
        i18n.t("admin.emergency_ask_password"),
        reply_markup=admin_cancel_keyboard(i18n),
    )


@router.message(StateFilter(EmergencyAccessStates.password), LocalizedText("common.cancel"))
async def cancel_emergency_access(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    if message.from_user is None:
        return
    user = await UserService(session).ensure_user(message.from_user)
    i18n = LocalizationService.from_user(user)
    await state.clear()
    await message.answer(
        i18n.t("admin.emergency_cancelled"),
        reply_markup=_home_keyboard(user, i18n),
    )


# A command is never a password: "/admin" typed here goes on to the admin router,
# "/start" to onboarding, instead of being spent as a failed attempt.
@router.message(StateFilter(EmergencyAccessStates.password), F.text, ~F.text.startswith("/"))
async def check_emergency_password(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
) -> None:
    await _check(message, message.text or "", session=session, settings=settings, state=state)


async def _check(
    message: Message,
    password: str,
    *,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
) -> None:
    if message.from_user is None:
        return
    user = await UserService(session).ensure_user(message.from_user)
    i18n = LocalizationService.from_user(user)
    # One message, one attempt: a failure ends the flow, and the command starts it again.
    await state.clear()
    await _discard(message)

    async with keyed_lock(f"emergency:{message.from_user.id}"):
        service = EmergencyAdminAuthService(session, EmergencyAccessPolicy.from_settings(settings))
        result = await service.authenticate(user.id, password)
        # Durable before anyone is told, and the user-row lock released before Telegram.
        await session.commit()

    if not result.granted:
        await message.answer(i18n.t("admin.access_denied"), reply_markup=_home_keyboard(user, i18n))
        return
    await message.answer(
        i18n.t("admin.emergency_granted", minutes=settings.emergency_admin_session_ttl_minutes)
    )
    await show_admin_menu(message, i18n, state)


@router.message(StateFilter(EmergencyAccessStates.password), ~F.text)
async def repeat_emergency_prompt(message: Message, session: AsyncSession) -> None:
    """Anything but text (a sticker, a photo) is not a password: ask again, count nothing."""
    if message.from_user is None:
        return
    user = await UserService(session).ensure_user(message.from_user)
    i18n = LocalizationService.from_user(user)
    await message.answer(
        i18n.t("admin.emergency_ask_password"),
        reply_markup=admin_cancel_keyboard(i18n),
    )
