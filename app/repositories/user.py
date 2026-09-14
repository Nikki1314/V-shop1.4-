"""User repository."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.enums import CityChoice, LanguageCode
from app.models.user import User
from app.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    model = User

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        result = await self.session.scalars(select(User).where(User.telegram_id == telegram_id))
        return result.first()

    async def get_for_update(self, user_id: int) -> User | None:
        """
        The user's row, locked until the transaction ends.

        Serialises operations on one person that must see each other's outcome —
        emergency login attempts, where the count of recent failures decides
        whether the next one is checked at all. Nothing else locks user rows, and
        this lock is never held across a Telegram call.
        """
        await self.session.flush()
        result = await self.session.scalars(
            select(User).where(User.id == user_id).with_for_update()
        )
        return result.first()

    async def list_by_username(self, username: str) -> list[User]:
        """
        Every row storing ``username`` (case-insensitive, no ``@``), oldest first.

        Usernames are not unique in the table: a handle can change hands, and
        the old holder keeps it until they next message the bot. Callers treat
        more than one row as ambiguous rather than picking one.
        """
        result = await self.session.scalars(
            select(User)
            .where(func.lower(User.username) == username.lower())
            .order_by(User.id.asc())
        )
        return list(result.all())

    async def get_by_telegram_id_with_cart(self, telegram_id: int) -> User | None:
        result = await self.session.scalars(
            select(User).where(User.telegram_id == telegram_id).options(selectinload(User.cart))
        )
        return result.first()

    async def get_or_create_by_telegram(
        self,
        telegram_id: int,
        *,
        username: str | None = None,
        first_name: str | None = None,
    ) -> tuple[User, bool]:
        """Return (user, created). Creates the user when missing."""
        user = await self.get_by_telegram_id(telegram_id)
        if user is not None:
            return user, False

        try:
            async with self.session.begin_nested():
                user = await self.create_and_add(
                    telegram_id=telegram_id,
                    username=username,
                    first_name=first_name,
                )
            return user, True
        except IntegrityError:
            user = await self.get_by_telegram_id(telegram_id)
            if user is None:
                raise
            return user, False

    async def update_profile(
        self,
        user: User,
        *,
        username: str | None = None,
        first_name: str | None = None,
    ) -> User:
        fields: dict[str, object] = {"last_seen": datetime.now(UTC)}
        if username is not None:
            fields["username"] = username
        if first_name is not None:
            fields["first_name"] = first_name
        return await self.update(user, **fields)

    async def set_language(self, user: User, language: LanguageCode) -> User:
        return await self.update(user, language=language)

    async def set_city(self, user: User, city: CityChoice) -> User:
        return await self.update(user, selected_city=city)

    async def touch_last_seen(self, user: User) -> User:
        return await self.update(user, last_seen=datetime.now(UTC))

    async def list_telegram_ids(self) -> list[int]:
        """
        All registered Telegram IDs (e.g. for broadcasts).

        Restricted to positive IDs. Telegram numbers users positively and
        groups/channels negatively, so this guarantees a broadcast can only
        ever reach individuals — even if a group ID had somehow been recorded
        as a user by an older build or a manual edit.
        """
        result = await self.session.scalars(select(User.telegram_id).where(User.telegram_id > 0))
        return list(result.all())

    async def list_all_users(self, *, offset: int = 0, limit: int | None = None) -> list[User]:
        stmt = select(User).order_by(User.id.asc()).offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self.session.scalars(stmt)
        return list(result.all())
