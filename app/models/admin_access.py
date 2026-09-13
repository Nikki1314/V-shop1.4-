"""Temporary admin sessions — the persistence behind break-glass access.

A row is a time-boxed grant of admin rights to one registered user, obtained
through a method the row names (``auth_method``). It is *active* while it has
not been revoked and has not expired; authorization that consults this table
asks exactly that question and nothing else.

What the table deliberately does not hold:

* any credential — no password, no hash, no token. Authentication happens
  elsewhere and leaves only the fact that it succeeded, as this row;
* membership of ``ADMIN_IDS``. A session never turns anyone into a configured
  admin: it expires on its own, and it can be revoked before that.

Every session is kept: revocation sets ``revoked_at`` rather than deleting, and
expiry is a comparison against the clock, not a write. The table is therefore
its own audit trail — who held admin rights, from when, until when, and whether
someone ended it early.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Index, text
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.database.base import Base, TimestampMixin
from app.models.enums import AdminAccessAttemptOutcome, AdminAccessMethod
from app.models.types import enum_values


def as_utc(value: datetime) -> datetime:
    """A timestamp read back from the database, comparable with an aware clock.

    PostgreSQL returns ``timestamptz`` values aware; SQLite (the test database)
    returns them naive, in UTC. Comparing the two raises, so the model settles it
    once here.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class AdminAccessSession(Base, TimestampMixin):
    """One temporary admin grant. Active while unrevoked and not yet expired."""

    __tablename__ = "admin_access_sessions"
    __table_args__ = (
        # Both timestamps are written from one clock (the service's), so this
        # holds whatever the database clock says.
        CheckConstraint(
            "expires_at > created_at",
            name="ck_admin_access_sessions_expires_after_creation",
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="ck_admin_access_sessions_revoked_after_creation",
        ),
        # The active-session lookup: this user's unrevoked sessions, newest
        # expiry first. Partial, so revoked rows fall out of it as the audit
        # trail grows.
        Index(
            "ix_admin_access_sessions_user_id_expires_at",
            "user_id",
            "expires_at",
            postgresql_where=text("revoked_at IS NULL"),
            sqlite_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # The operator must be a registered user: identity is their Telegram id on
    # the users row, the same identity every other authorization check uses.
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    auth_method: Mapped[AdminAccessMethod] = mapped_column(
        Enum(
            AdminAccessMethod,
            name="admin_access_method",
            native_enum=False,
            length=32,
            values_callable=enum_values,
        ),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # A session records one grant and never becomes a different one: not for
    # another user, not by another method, and never extended. Revocation is
    # final — the timestamp can be set once and never cleared.
    @validates("user_id", "auth_method", "expires_at", "revoked_at")
    def _set_once(self, key: str, value: object) -> object:
        current = self.__dict__.get(key)
        if current is not None and value != current:
            raise ValueError(f"AdminAccessSession.{key} never changes once set")
        return value

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    def is_active(self, now: datetime) -> bool:
        """Unrevoked and not yet expired at ``now``; expiry itself is not active."""
        return self.revoked_at is None and as_utc(self.expires_at) > as_utc(now)

    def __repr__(self) -> str:
        return (
            f"<AdminAccessSession id={self.id} user_id={self.user_id} "
            f"method={self.auth_method} expires_at={self.expires_at} "
            f"revoked={self.is_revoked}>"
        )


class AdminAccessAttempt(Base, TimestampMixin):
    """One authentication attempt, by outcome — never the credential offered.

    The attempts of one user within the lockout window decide whether the next
    one is even checked (:class:`~app.services.emergency_admin.EmergencyAdminAuthService`),
    and the table is the audit trail of who tried, when, and how it went.
    """

    __tablename__ = "admin_access_attempts"
    __table_args__ = (
        # The lockout question: this user's recent attempts, newest first.
        Index("ix_admin_access_attempts_user_id_created_at", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    auth_method: Mapped[AdminAccessMethod] = mapped_column(
        Enum(
            AdminAccessMethod,
            name="admin_access_method",
            native_enum=False,
            length=32,
            values_callable=enum_values,
        ),
        nullable=False,
    )
    outcome: Mapped[AdminAccessAttemptOutcome] = mapped_column(
        Enum(
            AdminAccessAttemptOutcome,
            name="admin_access_attempt_outcome",
            native_enum=False,
            length=32,
            values_callable=enum_values,
        ),
        nullable=False,
    )

    # An attempt is a fact about the past: nothing on it changes.
    @validates("user_id", "auth_method", "outcome")
    def _set_once(self, key: str, value: object) -> object:
        current = self.__dict__.get(key)
        if current is not None and value != current:
            raise ValueError(f"AdminAccessAttempt.{key} never changes once set")
        return value

    def __repr__(self) -> str:
        return (
            f"<AdminAccessAttempt id={self.id} user_id={self.user_id} "
            f"method={self.auth_method} outcome={self.outcome}>"
        )
