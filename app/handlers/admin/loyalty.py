"""🪪 Loyalty — an operator credits a customer some stamps by hand.

``/admin_adjust_stamps`` (or the section's button) asks for the customer, shows
who that is and how many stamps they have, asks how many to add, shows a
confirmation, and books the credit on the tap. Authorization is the admin
router's: a configured admin or an active break-glass session reaches this
module, nobody else, and the ``admin_grant`` the router injects is recorded on
the credit's author row.

What the screens never decide: the customer and the amount live in the
operator's FSM data, put there by the steps above; the confirm button carries
only the operation id those steps generated. A tap is honoured when its id is
the one in the FSM data — a button from an earlier screen, a forged id, another
operator's copied button or a screen that outlived a restart is answered "no
longer valid" and credits nothing. The customer is re-resolved by Telegram id
right before booking, and the amount is checked again by the service.

Booking happens inside ``confirm_once`` (so a second tap, or two at once, meet
``submitted``) and commits before the operator is told — the account lock the
credit takes is released before Telegram is awaited. No message goes to the
customer or the manager chat: the customer sees the stamps on their card.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.filters.localized_text import LocalizedText
from app.keyboards.admin import admin_cancel_keyboard, admin_menu_keyboard
from app.keyboards.admin_loyalty import (
    CALLBACK_LOYALTY_CANCEL,
    CALLBACK_LOYALTY_CONFIRM_PREFIX,
    CALLBACK_LOYALTY_CREDIT,
    loyalty_actions_keyboard,
    loyalty_confirm_keyboard,
)
from app.security.admin import AdminGrant
from app.services.admin import (
    AdminUserService,
    AmbiguousCustomerError,
    CustomerIdentity,
    CustomerLookupError,
    CustomerNotFoundError,
)
from app.services.admin.loyalty import (
    PANEL_CREDIT_NOTE,
    AdjustmentActor,
    AdjustmentError,
    AdminLoyaltyService,
    StampAdjustmentPolicy,
)
from app.services.localization import LocalizationService
from app.services.stamp_card import StampCardPolicy
from app.services.user import UserService
from app.states.admin import ADMIN_WIZARD_STATES, LOYALTY_WIZARD_STATES, AdjustStampsStates
from app.utils.confirm import confirm_once
from app.utils.html import e
from app.utils.telegram_ui import as_message, clear_inline_markup
from app.utils.validators import parse_positive_int

logger = logging.getLogger(__name__)

router = Router(name="admin_loyalty")


def _service(session: AsyncSession, settings: Settings) -> AdminLoyaltyService:
    """The credit service under the configured limits — never the defaults."""
    return AdminLoyaltyService(
        session,
        StampAdjustmentPolicy.from_settings(settings),
        stamp_policy=StampCardPolicy.from_settings(settings),
    )


def _who(identity: CustomerIdentity, i18n: LocalizationService) -> str:
    """The customer as shown: their handle, escaped, or a localized 'no username'."""
    return e(f"@{identity.username}") if identity.username else i18n.t("admin.loyalty_no_username")


async def _ask_target(message: Message, i18n: LocalizationService, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AdjustStampsStates.target)
    await message.answer(
        i18n.t("admin.loyalty_ask_target"), reply_markup=admin_cancel_keyboard(i18n)
    )


async def _leave(
    message: Message | None,
    i18n: LocalizationService,
    state: FSMContext,
    text_key: str,
) -> None:
    await state.clear()
    if message is not None:
        await message.answer(i18n.t(text_key), reply_markup=admin_menu_keyboard(i18n))


# --- entry -----------------------------------------------------------------------


@router.message(LocalizedText("admin.menu_loyalty"), ~StateFilter(*ADMIN_WIZARD_STATES))
async def open_loyalty(message: Message, i18n: LocalizationService) -> None:
    await message.answer(
        i18n.t("admin.section_loyalty"), reply_markup=loyalty_actions_keyboard(i18n)
    )


@router.callback_query(F.data == CALLBACK_LOYALTY_CREDIT)
async def start_credit_from_button(
    callback: CallbackQuery, i18n: LocalizationService, state: FSMContext
) -> None:
    await callback.answer()
    message = as_message(callback)
    if message is not None:
        await _ask_target(message, i18n, state)


@router.message(Command("admin_adjust_stamps"))
async def cmd_adjust_stamps(message: Message, i18n: LocalizationService, state: FSMContext) -> None:
    await _ask_target(message, i18n, state)


@router.message(StateFilter(*LOYALTY_WIZARD_STATES), LocalizedText("common.cancel"))
async def cancel_by_button(message: Message, i18n: LocalizationService, state: FSMContext) -> None:
    await _leave(message, i18n, state, "admin.loyalty_cancelled")


# --- step 1: the customer ----------------------------------------------------------------


@router.message(StateFilter(AdjustStampsStates.target), F.text)
async def receive_target(
    message: Message,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
) -> None:
    if message.from_user is None:
        return
    try:
        identity = await AdminUserService(session).resolve_customer(message.text or "")
    except CustomerNotFoundError:
        await message.answer(i18n.t("admin.loyalty_target_not_found"))
        return
    except AmbiguousCustomerError:
        await message.answer(i18n.t("admin.loyalty_target_ambiguous"))
        return
    except CustomerLookupError:
        await message.answer(i18n.t("admin.loyalty_target_invalid"))
        return

    operator = await UserService(session).ensure_user(message.from_user)
    if identity.user_id == operator.id:
        await message.answer(i18n.t("admin.loyalty_target_self"))
        return

    card = await _service(session, settings).card(identity.user_id)
    await state.update_data(
        target_telegram_id=identity.telegram_id,
        target_user_id=identity.user_id,
        balance=card.stamps,
    )
    await state.set_state(AdjustStampsStates.amount)
    await message.answer(
        i18n.t(
            "admin.loyalty_target_card",
            username=_who(identity, i18n),
            telegram_id=identity.telegram_id,
            balance=card.stamps,
            max=settings.loyalty_admin_max_stamp_adjustment,
        )
    )


@router.message(StateFilter(AdjustStampsStates.target))
async def receive_target_not_text(message: Message, i18n: LocalizationService) -> None:
    await message.answer(i18n.t("admin.loyalty_target_invalid"))


# --- step 2: the amount ----------------------------------------------------------------


@router.message(StateFilter(AdjustStampsStates.amount), F.text)
async def receive_amount(
    message: Message,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
) -> None:
    limit = settings.loyalty_admin_max_stamp_adjustment
    amount = parse_positive_int(message.text)
    if amount is None or amount > limit:
        await message.answer(i18n.t("admin.loyalty_amount_invalid", max=limit))
        return
    data = await state.get_data()
    target_telegram_id = data.get("target_telegram_id")
    if not isinstance(target_telegram_id, int):
        await _leave(message, i18n, state, "admin.loyalty_refused")
        return
    try:
        identity = await AdminUserService(session).resolve_telegram_id(target_telegram_id)
    except CustomerLookupError:
        await _leave(message, i18n, state, "admin.loyalty_refused")
        return
    card = await _service(session, settings).card(identity.user_id)

    # A fresh id for this confirmation screen: the tap must quote it back.
    operation_id = str(uuid.uuid4())
    await state.update_data(
        amount=amount, balance=card.stamps, operation_id=operation_id, submitted=False
    )
    await state.set_state(AdjustStampsStates.confirmation)
    await message.answer(
        i18n.t(
            "admin.loyalty_confirm",
            amount=amount,
            username=_who(identity, i18n),
            telegram_id=identity.telegram_id,
            balance=card.stamps,
            after=card.stamps + amount,
        ),
        reply_markup=loyalty_confirm_keyboard(i18n, operation_id),
    )


@router.message(StateFilter(AdjustStampsStates.amount))
async def receive_amount_not_text(
    message: Message, i18n: LocalizationService, settings: Settings
) -> None:
    await message.answer(
        i18n.t("admin.loyalty_amount_invalid", max=settings.loyalty_admin_max_stamp_adjustment)
    )


@router.message(StateFilter(AdjustStampsStates.confirmation))
async def waiting_for_the_tap(message: Message, i18n: LocalizationService) -> None:
    await message.answer(i18n.t("admin.loyalty_confirm_waiting"))


# --- step 3: the tap --------------------------------------------------------------------


@router.callback_query(
    StateFilter(AdjustStampsStates.confirmation),
    F.data.startswith(CALLBACK_LOYALTY_CONFIRM_PREFIX),
)
async def confirm_credit(
    callback: CallbackQuery,
    i18n: LocalizationService,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    admin_grant: AdminGrant,
) -> None:
    if callback.from_user is None or callback.data is None:
        await callback.answer()
        return
    message = as_message(callback)
    tapped = callback.data.removeprefix(CALLBACK_LOYALTY_CONFIRM_PREFIX)

    outcome: Any = None
    async with confirm_once(state, lock_key=f"stamp_credit:{callback.from_user.id}") as data:
        if data is None:
            await callback.answer(i18n.t("admin.loyalty_in_progress"))
            return
        target_telegram_id = data.get("target_telegram_id")
        amount = data.get("amount")
        if (
            data.get("operation_id") != tapped
            or not isinstance(target_telegram_id, int)
            or not isinstance(amount, int)
        ):
            await state.clear()
            await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
            if message is not None:
                await clear_inline_markup(message)
            return

        operator = await UserService(session).ensure_user(callback.from_user)
        try:
            identity = await AdminUserService(session).resolve_telegram_id(target_telegram_id)
            outcome = await _service(session, settings).credit_stamps(
                target_user_id=identity.user_id,
                amount=amount,
                reason=PANEL_CREDIT_NOTE,
                actor=AdjustmentActor.from_grant(operator.id, admin_grant),
                operation_id=tapped,
            )
        except (CustomerLookupError, AdjustmentError):
            # Refused before any write: end the transaction (and the account
            # lock) before answering, then start the operator afresh.
            await session.rollback()
            await state.clear()
            await callback.answer(i18n.t("admin.loyalty_refused"), show_alert=True)
            if message is not None:
                await clear_inline_markup(message)
                await message.answer(
                    i18n.t("admin.loyalty_refused"), reply_markup=admin_menu_keyboard(i18n)
                )
            return
        # Durable before the operator is told, and the lock released before Telegram.
        await session.commit()

    await state.clear()
    logger.info(
        "Admin credited stamps operator_user_id=%s kind=%s target_user_id=%s amount=%s "
        "operation_id=%s created=%s",
        operator.id,
        admin_grant.kind,
        identity.user_id,
        outcome.stamps,
        tapped,
        outcome.created,
    )
    await callback.answer()
    if message is not None:
        await clear_inline_markup(message)
    lines = [
        i18n.t(
            "admin.loyalty_done",
            amount=outcome.stamps,
            username=_who(identity, i18n),
            telegram_id=identity.telegram_id,
            balance=outcome.card.stamps,
        )
    ]
    if outcome.card.can_claim:
        lines.append(i18n.t("admin.loyalty_done_claim"))
    if message is not None:
        await message.answer("\n".join(lines), reply_markup=admin_menu_keyboard(i18n))


@router.callback_query(StateFilter(*LOYALTY_WIZARD_STATES), F.data == CALLBACK_LOYALTY_CANCEL)
async def cancel_by_callback(
    callback: CallbackQuery, i18n: LocalizationService, state: FSMContext
) -> None:
    await callback.answer()
    message = as_message(callback)
    if message is not None:
        await clear_inline_markup(message)
    await _leave(message, i18n, state, "admin.loyalty_cancelled")


# Registered last: a confirm or cancel button whose wizard is over — another
# screen's, another operator's, or one that outlived a restart — is answered and
# taken away instead of left spinning (the root fallback ignores admin buttons).
@router.callback_query(
    F.data.startswith(CALLBACK_LOYALTY_CONFIRM_PREFIX) | (F.data == CALLBACK_LOYALTY_CANCEL)
)
async def stale_credit_button(callback: CallbackQuery, i18n: LocalizationService) -> None:
    await callback.answer(i18n.t("error.invalid_callback"), show_alert=True)
    message = as_message(callback)
    if message is not None:
        await clear_inline_markup(message)
