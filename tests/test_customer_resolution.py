"""
Naming the customer of a manual stamp credit — ``AdminUserService.resolve_customer``.

The Telegram id is canonical; a username is a convenience that resolves to it.
These tests pin the parsing (digits are an id, a handle is a handle, nothing
else is anything), the lookup (exact id; exactly one stored handle, or refused),
what the operator is shown, and that the stored handle is only as fresh as the
customer's last message.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
from aiogram.types import User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.services.admin import (
    AmbiguousCustomerError,
    CustomerLookupError,
    CustomerNotFoundError,
    MalformedCustomerIdentifierError,
)
from app.services.admin.users import (
    MAX_TELEGRAM_ID,
    AdminUserService,
    CustomerIdentifier,
    CustomerIdentity,
    IdentifierKind,
    parse_customer_identifier,
)
from app.services.user import UserService
from app.utils.html import e
from tests.factories import make_user

ROOT = pathlib.Path(__file__).resolve().parent.parent
ALICE_ID = 5_123_456_789  # above 2**31: real Telegram ids are, and the column is BigInteger


async def alice(session: AsyncSession, username: str | None = "Alice_Vape") -> User:
    user = await make_user(session, telegram_id=ALICE_ID)
    user.username = username
    user.first_name = "Alice"
    await session.flush()
    return user


# ======================================================= by Telegram id


@pytest.mark.parametrize("raw", ["5123456789", "  5123456789  ", "5123456789\n"])
async def test_a_valid_telegram_id_resolves_exactly(session: AsyncSession, raw: str) -> None:
    user = await alice(session)

    identity = await AdminUserService(session).resolve_customer(raw)

    assert identity == CustomerIdentity(
        user_id=user.id,
        telegram_id=ALICE_ID,
        username="Alice_Vape",
        first_name="Alice",
        resolved_by=IdentifierKind.TELEGRAM_ID,
    )
    assert identity.label == "@Alice_Vape (5123456789)"


async def test_a_nonexistent_telegram_id_is_not_found(session: AsyncSession) -> None:
    await alice(session)
    with pytest.raises(CustomerNotFoundError):
        await AdminUserService(session).resolve_customer("5123456788")
    with pytest.raises(CustomerNotFoundError):
        await AdminUserService(session).resolve_telegram_id(42)


async def test_a_telegram_id_is_never_read_as_a_username(session: AsyncSession) -> None:
    """A stored username made of digits (impossible on Telegram) never catches an id lookup."""
    user = await alice(session, username="12345")
    with pytest.raises(CustomerNotFoundError):
        await AdminUserService(session).resolve_customer("12345")
    assert (await AdminUserService(session).resolve_customer(str(ALICE_ID))).user_id == user.id


# ======================================================= by username


@pytest.mark.parametrize(
    "raw",
    [
        "@Alice_Vape",
        "Alice_Vape",
        "alice_vape",
        "@ALICE_VAPE",
        "t.me/Alice_Vape",
        "https://t.me/alice_vape",
        " @Alice_Vape ",
    ],
)
async def test_a_valid_username_resolves_to_the_telegram_id(
    session: AsyncSession, raw: str
) -> None:
    user = await alice(session)

    identity = await AdminUserService(session).resolve_customer(raw)

    assert (identity.user_id, identity.telegram_id) == (user.id, ALICE_ID)
    assert identity.resolved_by == IdentifierKind.USERNAME
    assert identity.username == "Alice_Vape"  # as stored, not as typed
    assert identity.label == "@Alice_Vape (5123456789)"


async def test_a_nonexistent_username_is_not_found(session: AsyncSession) -> None:
    await alice(session)
    for raw in ("@Alice_Vapes", "@bob_smith", "t.me/nobody_here"):
        with pytest.raises(CustomerNotFoundError):
            await AdminUserService(session).resolve_customer(raw)


async def test_a_changed_username_is_only_as_fresh_as_the_last_message(
    session: AsyncSession,
) -> None:
    user = await alice(session, username="old_handle")
    service = AdminUserService(session)

    # Alice renamed herself on Telegram; the bot has not heard from her since.
    stale = await service.resolve_customer("@old_handle")
    assert stale.telegram_id == ALICE_ID and stale.username == "old_handle"
    with pytest.raises(CustomerNotFoundError):
        await service.resolve_customer("@new_handle")

    # Her next message refreshes the row; the old handle stops resolving.
    await UserService(session).ensure_user(
        TgUser(id=ALICE_ID, is_bot=False, first_name="Alice", username="new_handle")
    )
    fresh = await service.resolve_customer("@new_handle")
    assert fresh.user_id == user.id and fresh.username == "new_handle"
    with pytest.raises(CustomerNotFoundError):
        await service.resolve_customer("@old_handle")
    # The id never moved.
    assert (await service.resolve_customer(str(ALICE_ID))).user_id == user.id


async def test_a_customer_without_a_username_is_reachable_only_by_id(session: AsyncSession) -> None:
    user = await alice(session, username=None)
    service = AdminUserService(session)

    identity = await service.resolve_customer(str(ALICE_ID))

    assert identity.user_id == user.id and identity.username is None
    assert identity.label == "5123456789"
    with pytest.raises(CustomerNotFoundError):
        await service.resolve_customer("@Alice_Vape")


async def test_a_handle_stored_for_two_customers_is_ambiguous(session: AsyncSession) -> None:
    """The handle changed hands and the old holder has not been seen since: refuse, never guess."""
    first = await alice(session, username="Shared_Name")
    second = await make_user(session, telegram_id=ALICE_ID + 1)
    second.username = "shared_name"  # a different case is the same Telegram handle
    await session.flush()
    service = AdminUserService(session)

    with pytest.raises(AmbiguousCustomerError):
        await service.resolve_customer("@shared_name")

    # Each is still reachable by id, which is why the error points there.
    assert (await service.resolve_customer(str(ALICE_ID))).user_id == first.id
    assert (await service.resolve_customer(str(ALICE_ID + 1))).user_id == second.id


# ======================================================= malformed input


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "@",
        "@ab",
        "abcd",  # four characters: too short for a handle
        "@1alice",  # must start with a letter
        "alice vape",
        "alice@vape",
        "user!",
        "-5",
        "0",
        "+5123456789",
        "5123456789x",
        "12345678901234567890",  # twenty digits: past any Telegram id
        "t.me/",
        "https://t.me/",
        "<script>alert(1)</script>",
        "'; DROP TABLE users;--",
        "tg://user?id=5123456789",
    ],
)
async def test_anything_else_is_malformed(session: AsyncSession, raw: str) -> None:
    await alice(session)
    with pytest.raises(MalformedCustomerIdentifierError):
        await AdminUserService(session).resolve_customer(raw)


@pytest.mark.parametrize("value", [None, 42, 5.0, b"5123456789"])
async def test_a_non_string_is_malformed(session: AsyncSession, value: Any) -> None:
    with pytest.raises(MalformedCustomerIdentifierError):
        await AdminUserService(session).resolve_customer(value)


@pytest.mark.parametrize("value", [0, -1, MAX_TELEGRAM_ID + 1, True, "5123456789", 5.0])
async def test_the_id_re_check_refuses_anything_but_a_positive_id(
    session: AsyncSession, value: Any
) -> None:
    with pytest.raises(MalformedCustomerIdentifierError):
        await AdminUserService(session).resolve_telegram_id(value)


def test_parsing_has_no_grey_zone() -> None:
    assert parse_customer_identifier("5123456789") == CustomerIdentifier(
        IdentifierKind.TELEGRAM_ID, telegram_id=5_123_456_789
    )
    assert parse_customer_identifier(str(MAX_TELEGRAM_ID)).telegram_id == MAX_TELEGRAM_ID
    assert parse_customer_identifier("@Alice_Vape") == CustomerIdentifier(
        IdentifierKind.USERNAME, username="alice_vape"
    )
    assert parse_customer_identifier("https://t.me/ALICE_VAPE").username == "alice_vape"
    assert parse_customer_identifier("a" * 32).username == "a" * 32
    with pytest.raises(MalformedCustomerIdentifierError):
        parse_customer_identifier("a" * 33)


def test_every_refusal_is_a_lookup_error_a_wizard_can_answer() -> None:
    for error in (MalformedCustomerIdentifierError, CustomerNotFoundError, AmbiguousCustomerError):
        assert issubclass(error, CustomerLookupError)
    assert issubclass(CustomerLookupError, ValueError)


# ======================================================= what is shown, and what is not trusted


async def test_the_identity_shown_before_confirmation_is_the_stored_one(
    session: AsyncSession,
) -> None:
    """The operator sees the row, not their input — and escapes it before HTML."""
    user = await alice(session, username="b_and_a")
    user.first_name = "<b>Alice</b> & co"
    await session.flush()

    identity = await AdminUserService(session).resolve_customer("@B_AND_A")

    assert identity.first_name == "<b>Alice</b> & co"
    assert e(identity.first_name) == "&lt;b&gt;Alice&lt;/b&gt; &amp; co"
    assert e(identity.label) == "@b_and_a (5123456789)"


def test_resolution_knows_nothing_about_authorization() -> None:
    """A target is not a grant: the resolver takes no sender, no grant, no settings."""
    source = (ROOT / "app" / "services" / "admin" / "users.py").read_text(encoding="utf-8")
    for forbidden in ("app.security", "admin_grant", "AdminGrant", "is_admin", "settings"):
        assert forbidden not in source, forbidden
