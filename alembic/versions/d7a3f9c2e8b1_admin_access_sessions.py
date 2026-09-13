"""admin access sessions: temporary, revocable admin grants

NON-DESTRUCTIVE. One new table, ``admin_access_sessions``; no existing table,
column or row is touched, and nothing is backfilled.

A row grants one registered user admin rights until ``expires_at`` unless
``revoked_at`` is set first. It names how the grant was obtained
(``auth_method``, ``break_glass``) and holds no credential of any kind — no
password, hash or token. It does not add anyone to ``ADMIN_IDS``; nothing in the
bot's authorization reads it until a later revision wires it in.

Rows are never deleted by the application: revocation is a timestamp, so the
table is the audit trail of who held temporary admin rights and when.

Downgrade drops the table and with it that record, so it refuses while any
session row exists unless told explicitly::

    alembic -x allow_admin_access_data_loss=true downgrade c5d2e8f1a6b3

Revision ID: d7a3f9c2e8b1
Revises: c5d2e8f1a6b3
Create Date: 2026-09-13 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "d7a3f9c2e8b1"
down_revision: str | None = "c5d2e8f1a6b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _refuse_to_forget_admin_access_history() -> None:
    """Stop a downgrade that would erase the record of temporary admin grants."""
    if context.is_offline_mode():
        return  # emitting SQL for review: there is no database to inspect
    flag = context.get_x_argument(as_dictionary=True).get("allow_admin_access_data_loss", "")
    allowed = str(flag).strip().lower() in {"1", "true", "yes"}
    rows = int(
        op.get_bind().execute(sa.text("SELECT count(*) FROM admin_access_sessions")).scalar() or 0
    )
    if rows and not allowed:
        raise RuntimeError(
            f"Refusing to downgrade d7a3f9c2e8b1: {rows} admin access session(s) — the record "
            "of who held temporary admin rights — would be lost. Take a pg_dump first, then "
            "re-run with `alembic -x allow_admin_access_data_loss=true downgrade <revision>`."
        )


def upgrade() -> None:
    op.create_table(
        "admin_access_sessions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "auth_method",
            sa.Enum("break_glass", name="admin_access_method", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_admin_access_sessions_expires_after_creation",
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="ck_admin_access_sessions_revoked_after_creation",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_admin_access_sessions_user_id_expires_at",
        "admin_access_sessions",
        ["user_id", "expires_at"],
        unique=False,
        postgresql_where=sa.text("revoked_at IS NULL"),
        sqlite_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    _refuse_to_forget_admin_access_history()
    op.drop_index("ix_admin_access_sessions_user_id_expires_at", table_name="admin_access_sessions")
    op.drop_table("admin_access_sessions")
