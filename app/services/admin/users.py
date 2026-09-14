"""Admin user queries: broadcast recipients, and finding one customer by identity.

Finding a customer (:meth:`AdminUserService.resolve_customer`) is how an
operator names the target of a manual stamp credit. The canonical identifier is
the Telegram user id — it is what Telegram authenticates and what ``users`` is
keyed by. A ``@username`` is accepted as a convenience and resolved to that id:
usernames change hands, may be absent, and are only as fresh as the customer's
last message to the bot, so what the resolver returns is always the id plus the
identity as stored, for the operator to look at before confirming.

Parsing has no grey zone: digits are an id (Telegram usernames start with a
letter), a handle is a handle, anything else is malformed. Lookup has none
either: an id matches its row or nothing; a handle matches exactly one row,
case-insensitively, or is refused — missing and ambiguous alike — never guessed.

None of this is authorization. Whatever a client sends here — an id typed into
a wizard, a value carried by a callback — identifies a *target*; who may credit
stamps is decided by the admin router's gates on the sender, and the credit
itself re-checks the target by id at the moment it is booked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.repositories.user import UserRepository
from app.services.admin.exceptions import (
    AmbiguousCustomerError,
    CustomerNotFoundError,
    MalformedCustomerIdentifierError,
)

# Telegram user ids are positive and fit a signed 64-bit integer (users.telegram_id is BigInteger).
MAX_TELEGRAM_ID = 2**63 - 1
_TELEGRAM_ID = re.compile(r"^\d{1,19}$")
# Telegram usernames: 5–32 characters, letters, digits and underscores, starting with a letter.
_USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
_LINK_PREFIXES = ("https://t.me/", "http://t.me/", "t.me/", "https://telegram.me/", "telegram.me/")


class IdentifierKind(StrEnum):
    TELEGRAM_ID = "telegram_id"
    USERNAME = "username"


@dataclass(frozen=True, slots=True)
class CustomerIdentifier:
    """What the operator typed, understood: an id or a normalised handle."""

    kind: IdentifierKind
    telegram_id: int | None = None
    username: str | None = None  # lower-case, without the @


def parse_customer_identifier(raw: str) -> CustomerIdentifier:
    """
    Read an id or a handle out of operator input. Raises
    :class:`MalformedCustomerIdentifierError` for anything else.

    Accepted: ``123456789``; ``@name``, ``name``, ``t.me/name``,
    ``https://t.me/name`` (case-insensitive). Nothing is ever *both*.
    """
    if not isinstance(raw, str):
        raise MalformedCustomerIdentifierError("a customer is named by a Telegram id or @username")
    text = raw.strip()
    if _TELEGRAM_ID.match(text):
        value = int(text)
        if not 1 <= value <= MAX_TELEGRAM_ID:
            raise MalformedCustomerIdentifierError("a Telegram id is a positive number")
        return CustomerIdentifier(IdentifierKind.TELEGRAM_ID, telegram_id=value)
    for prefix in _LINK_PREFIXES:
        if text.lower().startswith(prefix):
            text = text[len(prefix) :]
            break
    if text.startswith("@"):
        text = text[1:]
    if _USERNAME.match(text):
        return CustomerIdentifier(IdentifierKind.USERNAME, username=text.lower())
    raise MalformedCustomerIdentifierError("a customer is named by a Telegram id or @username")


@dataclass(frozen=True, slots=True)
class CustomerIdentity:
    """A resolved customer: the canonical id, plus the stored identity for the operator to check."""

    user_id: int
    telegram_id: int
    username: str | None
    first_name: str | None
    resolved_by: IdentifierKind

    @property
    def label(self) -> str:
        """``@name (123456789)`` or ``123456789`` — plain text; escape before HTML."""
        return f"@{self.username} ({self.telegram_id})" if self.username else str(self.telegram_id)

    @classmethod
    def of(cls, user: User, resolved_by: IdentifierKind) -> CustomerIdentity:
        return cls(
            user_id=user.id,
            telegram_id=user.telegram_id,
            username=user.username,
            first_name=user.first_name,
            resolved_by=resolved_by,
        )


class AdminUserService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.users = UserRepository(session)

    async def list_broadcast_recipient_ids(self) -> list[int]:
        return await self.users.list_telegram_ids()

    async def count_users(self) -> int:
        return await self.users.count()

    # --- finding one customer --------------------------------------------------------

    async def resolve_customer(self, raw: str) -> CustomerIdentity:
        """
        The one customer ``raw`` names.

        An id finds its row or raises :class:`CustomerNotFoundError`. A handle
        must match exactly one stored username, case-insensitively: none raises
        :class:`CustomerNotFoundError`, several — a handle that changed hands
        while its old holder has not been seen since — raises
        :class:`AmbiguousCustomerError`; the operator then uses the id. Anything
        unreadable raises :class:`MalformedCustomerIdentifierError`. All three
        are :class:`CustomerLookupError`, a ``ValueError``.
        """
        identifier = parse_customer_identifier(raw)
        if identifier.kind == IdentifierKind.TELEGRAM_ID:
            assert identifier.telegram_id is not None
            return await self.resolve_telegram_id(identifier.telegram_id)
        assert identifier.username is not None
        matches = await self.users.list_by_username(identifier.username)
        if not matches:
            raise CustomerNotFoundError(f"no customer has the username @{identifier.username}")
        if len(matches) > 1:
            raise AmbiguousCustomerError(
                f"@{identifier.username} is stored for {len(matches)} customers; use the id"
            )
        return CustomerIdentity.of(matches[0], IdentifierKind.USERNAME)

    async def resolve_telegram_id(self, telegram_id: int) -> CustomerIdentity:
        """The customer with this id: the re-check a confirmation step makes before booking."""
        if isinstance(telegram_id, bool) or not isinstance(telegram_id, int):
            raise MalformedCustomerIdentifierError("a Telegram id is a positive number")
        if not 1 <= telegram_id <= MAX_TELEGRAM_ID:
            raise MalformedCustomerIdentifierError("a Telegram id is a positive number")
        user = await self.users.get_by_telegram_id(telegram_id)
        if user is None:
            raise CustomerNotFoundError(f"no customer has the Telegram id {telegram_id}")
        return CustomerIdentity.of(user, IdentifierKind.TELEGRAM_ID)
