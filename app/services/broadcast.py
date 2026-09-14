"""Broadcast service — send messages to all registered users.

Sent only when an admin composes and confirms a broadcast. Nothing here is
triggered by loyalty state: a manual stamp credit by an operator never starts,
joins or targets a broadcast.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, int], Awaitable[None]]  # sent, failed, total

# Soft pacing between successful sends (Telegram ~30 msg/s theoretical).
_DEFAULT_DELAY_SECONDS = 0.05
_MAX_RATE_LIMIT_RETRIES = 5


@dataclass
class BroadcastResult:
    total: int
    sent: int = 0
    failed: list[int] = field(default_factory=list)

    @property
    def failed_count(self) -> int:
        return len(self.failed)


class BroadcastService:
    """Deliver a text and/or photo broadcast with rate-limit handling."""

    def __init__(
        self,
        bot: Bot,
        *,
        delay_seconds: float = _DEFAULT_DELAY_SECONDS,
    ) -> None:
        self.bot = bot
        self.delay_seconds = delay_seconds

    async def _deliver_once(
        self,
        chat_id: int,
        *,
        text: str | None,
        photo_file_id: str | None,
    ) -> None:
        if photo_file_id:
            caption = text or None
            if caption is not None and len(caption) > 1024:
                await self.bot.send_photo(chat_id=chat_id, photo=photo_file_id)
                await self.bot.send_message(chat_id=chat_id, text=caption)
            else:
                await self.bot.send_photo(
                    chat_id=chat_id,
                    photo=photo_file_id,
                    caption=caption,
                )
            return

        if text:
            await self.bot.send_message(chat_id=chat_id, text=text)
            return

        raise ValueError("Broadcast requires text and/or photo")

    async def _deliver_with_retries(
        self,
        chat_id: int,
        *,
        text: str | None,
        photo_file_id: str | None,
    ) -> None:
        attempts = 0
        while True:
            try:
                await self._deliver_once(
                    chat_id,
                    text=text,
                    photo_file_id=photo_file_id,
                )
                return
            except TelegramRetryAfter as exc:
                attempts += 1
                if attempts > _MAX_RATE_LIMIT_RETRIES:
                    raise
                wait_for = float(exc.retry_after) + 0.5
                logger.warning(
                    "Broadcast rate-limited chat_id=%s retry_after=%.1fs attempt=%s",
                    chat_id,
                    wait_for,
                    attempts,
                )
                await asyncio.sleep(wait_for)

    async def send(
        self,
        chat_ids: Sequence[int],
        *,
        text: str | None = None,
        photo_file_id: str | None = None,
        on_progress: ProgressCallback | None = None,
        progress_every: int = 10,
    ) -> BroadcastResult:
        if not text and not photo_file_id:
            raise ValueError("Broadcast requires text and/or photo")

        result = BroadcastResult(total=len(chat_ids))
        for index, chat_id in enumerate(chat_ids, start=1):
            try:
                await self._deliver_with_retries(
                    chat_id,
                    text=text,
                    photo_file_id=photo_file_id,
                )
                result.sent += 1
            except (TelegramForbiddenError, TelegramBadRequest) as exc:
                logger.info(
                    "Broadcast failed chat_id=%s error=%s",
                    chat_id,
                    exc,
                )
                result.failed.append(chat_id)
            except TelegramNetworkError as exc:
                logger.warning(
                    "Broadcast network error chat_id=%s error=%s",
                    chat_id,
                    exc,
                )
                result.failed.append(chat_id)
            except Exception:
                logger.exception("Broadcast unexpected error chat_id=%s", chat_id)
                result.failed.append(chat_id)

            if on_progress is not None and (
                index == result.total or index % max(1, progress_every) == 0
            ):
                await on_progress(result.sent, result.failed_count, result.total)

            if self.delay_seconds > 0 and index < result.total:
                await asyncio.sleep(self.delay_seconds)

        return result
