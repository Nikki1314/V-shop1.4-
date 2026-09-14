"""loyalty stamp adjustments: who credited a customer's stamps by hand

NON-DESTRUCTIVE. One new table, ``loyalty_stamp_adjustments``; no existing
table, column or row is touched, and nothing is backfilled.

A manual stamp credit is an ordinary ``loyalty_transactions`` row of kind
``adjustment``. This table is its author: the operator (a registered user), the
authority they acted under (``configured`` — listed in ``ADMIN_IDS`` — or
``break_glass``, naming the ``admin_access_sessions`` row), and the operation id
that makes a repeated request the same adjustment. One row per ledger row and
one per operation id, both unique; an operator can never be the customer.

Downgrade drops the table and with it the record of who credited what, so it
refuses while any row exists unless told explicitly::

    alembic -x allow_loyalty_data_loss=true downgrade e8b2c4d6f1a3

Revision ID: f3c7a1d9e2b5
Revises: e8b2c4d6f1a3
Create Date: 2026-09-14 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "f3c7a1d9e2b5"
down_revision: str | None = "e8b2c4d6f1a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _refuse_to_forget_who_credited_stamps() -> None:
    """Stop a downgrade that would erase the authors of manual stamp credits."""
    if context.is_offline_mode():
        return  # emitting SQL for review: there is no database to inspect
    flag = context.get_x_argument(as_dictionary=True).get("allow_loyalty_data_loss", "")
    allowed = str(flag).strip().lower() in {"1", "true", "yes"}
    rows = int(
        op.get_bind().execute(sa.text("SELECT count(*) FROM loyalty_stamp_adjustments")).scalar()
        or 0
    )
    if rows and not allowed:
        raise RuntimeError(
            f"Refusing to downgrade f3c7a1d9e2b5: {rows} manual stamp credit(s) would lose the "
            "record of who made them. Take a pg_dump first, then re-run with "
            "`alembic -x allow_loyalty_data_loss=true downgrade <revision>`."
        )


def upgrade() -> None:
    op.create_table(
        "loyalty_stamp_adjustments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("transaction_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=False),
        sa.Column(
            "actor_kind",
            sa.Enum(
                "configured",
                "break_glass",
                name="admin_access_kind",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("access_session_id", sa.Integer(), nullable=True),
        sa.Column("operation_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(actor_kind = 'break_glass') = (access_session_id IS NOT NULL)",
            name="ck_loyalty_stamp_adjustments_session_matches_actor",
        ),
        sa.CheckConstraint(
            "user_id <> actor_user_id",
            name="ck_loyalty_stamp_adjustments_not_self",
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"], ["loyalty_transactions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["access_session_id"], ["admin_access_sessions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("transaction_id", name="uq_loyalty_stamp_adjustments_transaction_id"),
        sa.UniqueConstraint("operation_id", name="uq_loyalty_stamp_adjustments_operation_id"),
    )
    op.create_index(
        "ix_loyalty_stamp_adjustments_user_id_id",
        "loyalty_stamp_adjustments",
        ["user_id", "id"],
        unique=False,
    )
    op.create_index(
        "ix_loyalty_stamp_adjustments_actor_user_id_id",
        "loyalty_stamp_adjustments",
        ["actor_user_id", "id"],
        unique=False,
    )


def downgrade() -> None:
    _refuse_to_forget_who_credited_stamps()
    op.drop_index(
        "ix_loyalty_stamp_adjustments_actor_user_id_id", table_name="loyalty_stamp_adjustments"
    )
    op.drop_index("ix_loyalty_stamp_adjustments_user_id_id", table_name="loyalty_stamp_adjustments")
    op.drop_table("loyalty_stamp_adjustments")
