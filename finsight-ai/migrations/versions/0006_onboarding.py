"""Track when a user finished (or skipped) first-login onboarding.

Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('onboarded_at', sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column('users', 'onboarded_at')
