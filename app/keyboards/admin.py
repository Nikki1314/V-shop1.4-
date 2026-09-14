"""Admin reply keyboard builders (localized)."""

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from app.services.localization import LocalizationService


def admin_menu_keyboard(i18n: LocalizationService) -> ReplyKeyboardMarkup:
    """
    Admin panel Reply Keyboard: Products, Categories, Orders, Broadcast, Stats,
    Loyalty, Settings.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=i18n.t("admin.menu_products")),
                KeyboardButton(text=i18n.t("admin.menu_categories")),
            ],
            [
                KeyboardButton(text=i18n.t("admin.menu_orders")),
                KeyboardButton(text=i18n.t("admin.menu_broadcast")),
            ],
            [
                KeyboardButton(text=i18n.t("admin.menu_statistics")),
                KeyboardButton(text=i18n.t("admin.menu_loyalty")),
            ],
            [KeyboardButton(text=i18n.t("admin.menu_settings"))],
        ],
        resize_keyboard=True,
    )


def admin_cancel_keyboard(i18n: LocalizationService) -> ReplyKeyboardMarkup:
    """Single Cancel button used across admin wizards."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=i18n.t("common.cancel"))]],
        resize_keyboard=True,
    )


def admin_cancel_skip_keyboard(i18n: LocalizationService) -> ReplyKeyboardMarkup:
    """Cancel + Skip (keep current value) during edit wizards."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=i18n.t("common.skip"))],
            [KeyboardButton(text=i18n.t("common.cancel"))],
        ],
        resize_keyboard=True,
    )
