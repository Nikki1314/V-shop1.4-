"""Admin loyalty section keyboards (localized)."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.services.localization import LocalizationService

CALLBACK_LOYALTY_CREDIT = "admin:loy:credit"
# The confirm button carries only the operation id the confirmation screen was
# drawn with — never the customer or the amount. Those live in the operator's
# FSM data and are compared against the id on the tap. 13 + 36 bytes.
CALLBACK_LOYALTY_CONFIRM_PREFIX = "admin:loy:ok:"
CALLBACK_LOYALTY_CANCEL = "admin:loy:cancel"

__all__ = [
    "CALLBACK_LOYALTY_CANCEL",
    "CALLBACK_LOYALTY_CONFIRM_PREFIX",
    "CALLBACK_LOYALTY_CREDIT",
    "loyalty_actions_keyboard",
    "loyalty_confirm_keyboard",
]


def loyalty_actions_keyboard(i18n: LocalizationService) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=i18n.t("admin.loyalty_credit"), callback_data=CALLBACK_LOYALTY_CREDIT
                )
            ]
        ]
    )


def loyalty_confirm_keyboard(i18n: LocalizationService, operation_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=i18n.t("admin.loyalty_confirm_button"),
                    callback_data=f"{CALLBACK_LOYALTY_CONFIRM_PREFIX}{operation_id}",
                ),
                InlineKeyboardButton(
                    text=i18n.t("common.cancel"), callback_data=CALLBACK_LOYALTY_CANCEL
                ),
            ]
        ]
    )
