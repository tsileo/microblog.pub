"""Enable WAL mode

Revision ID: 6286262bb466
Revises: 9bc69ed947e2
Create Date: 2022-08-14 20:59:26.427796+00:00

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = '6286262bb466'
down_revision = '9bc69ed947e2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("PRAGMA journal_mode=WAL")


def downgrade() -> None:
    op.execute("PRAGMA journal_mode=DELETE")
