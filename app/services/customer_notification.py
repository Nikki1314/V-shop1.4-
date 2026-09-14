"""Customer-facing order status notifications.

Delivery is deliberately decoupled from the database write. The admin handler
commits the status change first and only then calls this service, because:

* the customer must never be told about a change that has not been persisted;
* a Telegram outage, a blocked bot or a deleted account must not roll back a
  status the shop has already applied.

Every failure mode is therefore swallowed here and reported through the return
value, never by raising into the caller's transaction.

Triggered by one event only: an admin changing an order's status to one of
``STATUS_MESSAGE_KEYS``. Nothing here reads the loyalty ledger, and no ledger
row — least of all a manual stamp credit by an operator — ever produces a
customer message. The customer sees credited stamps on their card, when they
open it.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

from app.models.enums import OrderStatus
from app.models.order import Order
from app.models.user import User
from app.services.localization import LocalizationService

logger = logging.getLogger(__name__)

# Only these transitions are worth interrupting a customer for. Reopening a
# cancelled order (-> New) is an internal correction, not customer news.
STATUS_MESSAGE_KEYS: dict[OrderStatus, str] = {
    OrderStatus.ACCEPTED: "notification.status_accepted",
    OrderStatus.SHIPPED: "notification.status_shipped",
    OrderStatus.COMPLETED: "notification.status_completed",
    OrderStatus.CANCELLED: "notification.status_cancelled",
}


def is_notifiable(status: OrderStatus) -> bool:
    """Whether a move into ``status`` should reach the customer."""
    return status in STATUS_MESSAGE_KEYS


class CustomerOrderNotificationService:
    """Tells a customer that their order moved on."""

    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    def build_message(self, order: Order, user: User) -> str | None:
        """Render the notification in the customer's persisted language."""
        key = STATUS_MESSAGE_KEYS.get(order.status)
        if key is None:
            return None
        i18n = LocalizationService.from_user(user)
        return i18n.t(key, order_id=order.id)

    async def notify_status_change(self, order: Order, user: User) -> bool:
        """
        Send the notification. Returns whether it was delivered.

        Never raises: the status change is already committed, so a delivery
        problem must not surface as a handler error.
        """
        text = self.build_message(order, user)
        if text is None:
            logger.debug(
                "Order %s moved to %s; no customer notification for that status",
                order.id,
                order.status,
            )
            return False

        try:
            await self.bot.send_message(chat_id=user.telegram_id, text=text)
        except TelegramForbiddenError:
            # Blocked the bot, or deleted their account. Expected, not an error.
            logger.info(
                "Customer notification skipped: bot blocked or account gone "
                "order_id=%s telegram_id=%s status=%s",
                order.id,
                user.telegram_id,
                order.status,
            )
            return False
        except TelegramRetryAfter as exc:
            logger.warning(
                "Customer notification rate-limited order_id=%s telegram_id=%s retry_after=%s",
                order.id,
                user.telegram_id,
                exc.retry_after,
            )
            return False
        except TelegramBadRequest:
            logger.warning(
                "Customer notification rejected order_id=%s telegram_id=%s",
                order.id,
                user.telegram_id,
                exc_info=True,
            )
            return False
        except TelegramAPIError:
            logger.warning(
                "Customer notification failed order_id=%s telegram_id=%s",
                order.id,
                user.telegram_id,
                exc_info=True,
            )
            return False
        except Exception:
            logger.exception(
                "Unexpected error notifying customer order_id=%s telegram_id=%s",
                order.id,
                user.telegram_id,
            )
            return False

        logger.info(
            "Customer notified order_id=%s telegram_id=%s status=%s",
            order.id,
            user.telegram_id,
            order.status,
        )
        return True
