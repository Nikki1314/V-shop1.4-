"""admin access attempts: the record of every emergency login attempt

NON-DESTRUCTIVE. One new table, ``admin_access_attempts``; no existing table,
column or row is touched, and nothing is backfilled.

A row is one authentication attempt by a registered user: the method
(``break_glass``), the outcome (``succeeded``, ``failed``, ``locked_out``) and
when. The credential offered is never stored — not the password, not a hash of
it. The rows of the last ``EMERGENCY_ADMIN_LOCKOUT_MINUTES`` decide whether a
user's next attempt is checked at all, and the table is the audit trail of who
tried and how it went.

Downgrade drops the table and with it that record, so it refuses while any
attempt row exists unless told explicitly::

    alembic -x allow_admin_access_data_loss=true downgrade d7a3f9c2e8b1

Revision ID: e8b2c4d6f1a3
Revises: d7a3f9c2e8b1
Create Date: 2026-09-13 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "e8b2c4d6f1a3"
down_revision: str | None = "d7a3f9c2e8b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _refuse_to_forget_login_attempts() -> None:
    """Stop a downgrade that would erase the record of emergency login attempts."""
    if context.is_offline_mode():
        return  # emitting SQL for review: there is no database to inspect
    flag = context.get_x_argument(as_dictionary=True).get("allow_admin_access_data_loss", "")
    allowed = str(flag).strip().lower() in {"1", "true", "yes"}
    rows = int(
        op.get_bind().execute(sa.text("SELECT count(*) FROM admin_access_attempts")).scalar() or 0
    )
    if rows and not allowed:
        raise RuntimeError(
            f"Refusing to downgrade e8b2c4d6f1a3: {rows} emergency login attempt(s) — the "
            "record of who tried to obtain admin rights — would be lost. Take a pg_dump first, "
            "then re-run with `alembic -x allow_admin_access_data_loss=true downgrade <revision>`."
        )


def upgrade() -> None:
    op.create_table(
        "admin_access_attempts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "auth_method",
            sa.Enum("break_glass", name="admin_access_method", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column(
            "outcome",
            sa.Enum(
                "succeeded",
                "failed",
                "locked_out",
                name="admin_access_attempt_outcome",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_admin_access_attempts_user_id_created_at",
        "admin_access_attempts",
        ["user_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    _refuse_to_forget_login_attempts()
    op.drop_index("ix_admin_access_attempts_user_id_created_at", table_name="admin_access_attempts")
    op.drop_table("admin_access_attempts")
