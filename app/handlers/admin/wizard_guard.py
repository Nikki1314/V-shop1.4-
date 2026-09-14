"""Block admin menu navigation while an FSM wizard is active."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import StateFilter
from aiogram.types import Message

from app.filters.localized_text import LocalizedText
from app.keyboards.admin import admin_cancel_keyboard
from app.services.localization import LocalizationService
from app.states.admin import ADMIN_WIZARD_STATES

router = Router(name="admin_wizard_guard")

_MENU_KEYS = (
    "admin.menu_products",
    "admin.menu_categories",
    "admin.menu_orders",
    "admin.menu_broadcast",
    "admin.menu_statistics",
    "admin.menu_settings",
    "admin.menu_loyalty",
)


@router.message(StateFilter(*ADMIN_WIZARD_STATES), LocalizedText(_MENU_KEYS[0]))
@router.message(StateFilter(*ADMIN_WIZARD_STATES), LocalizedText(_MENU_KEYS[1]))
@router.message(StateFilter(*ADMIN_WIZARD_STATES), LocalizedText(_MENU_KEYS[2]))
@router.message(StateFilter(*ADMIN_WIZARD_STATES), LocalizedText(_MENU_KEYS[3]))
@router.message(StateFilter(*ADMIN_WIZARD_STATES), LocalizedText(_MENU_KEYS[4]))
@router.message(StateFilter(*ADMIN_WIZARD_STATES), LocalizedText(_MENU_KEYS[5]))
@router.message(StateFilter(*ADMIN_WIZARD_STATES), LocalizedText(_MENU_KEYS[6]))
async def admin_menu_during_wizard(
    message: Message,
    i18n: LocalizationService,
) -> None:
    """Prevent jumping to another admin section mid-wizard (invalid state)."""
    await message.answer(
        i18n.t("admin.wizard_in_progress"),
        reply_markup=admin_cancel_keyboard(i18n),
    )
