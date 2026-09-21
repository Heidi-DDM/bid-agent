"""Correct the P0-03 deadline column comment without changing deadline facts.

`as_of` is a matching-rule determination time and must never be used to infer a
real bid submission deadline. Revision 0014's database comment incorrectly
suggested otherwise. This migration changes metadata only: no project row is
backfilled and no time component is created.

Revision ID: 0016_adr004_deadline_comment
Revises: 0015_adr004_deadline_timestamp
Create Date: 2026-09-16
"""
from alembic import op

revision = "0016_adr004_deadline_comment"
down_revision = "0015_adr004_deadline_timestamp"
branch_labels = None
depends_on = None

_COMMENT = "投标截止的日期级原文事实；仅有日期时保留日期级不确定性，缺失=NULL=待补，不得由 as_of 推断；已过→overdue"


def upgrade() -> None:
    op.execute("COMMENT ON COLUMN public_data.projects.bid_deadline IS " + repr(_COMMENT))


def downgrade() -> None:
    # Keep the corrected safety wording even when a local development database is
    # downgraded for debugging; reverting to the misleading 0014 text would violate
    # the documented no-inference rule. This migration has no data/schema effect.
    op.execute("COMMENT ON COLUMN public_data.projects.bid_deadline IS " + repr(_COMMENT))
