"""Broker-reported cost basis on holding positions; summary (realised P&L, charges) on imports.

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('holding_positions', sa.Column('average_cost', sa.BigInteger))
    op.add_column('holding_positions', sa.Column('cost_value', sa.BigInteger))
    op.add_column('investment_imports', sa.Column('summary_json', sa.Text, nullable=False, server_default='{}'))


def downgrade() -> None:
    op.drop_column('investment_imports', 'summary_json')
    op.drop_column('holding_positions', 'cost_value')
    op.drop_column('holding_positions', 'average_cost')
