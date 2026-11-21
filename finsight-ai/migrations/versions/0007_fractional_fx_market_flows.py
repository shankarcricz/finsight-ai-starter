"""Fractional/foreign holdings and market flows.

* Quantity scale 4 -> 8 and Price scale 4 -> 6. Values are stored as scaled integers, so existing rows
  are rescaled (x10^4 and x10^2) in the same transaction - their Decimal values are unchanged.
* Currency on securities and holding positions (USD for US stocks).
* market_flows: daily FII/DII activity.

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None

QTY = {'holding_positions': ('quantity', 'free_quantity', 'pledged_quantity', 'locked_quantity'),
       'investment_transactions': ('quantity',)}
PRICE = {'holding_positions': ('reported_rate', 'average_cost'), 'investment_transactions': ('price',)}


def _rescale(mapping, factor: int, divide: bool = False) -> None:
    op_ = '/' if divide else '*'
    for table, cols in mapping.items():
        sets = ', '.join(f'{c} = {c} {op_} {factor}' for c in cols)
        op.execute(f'UPDATE {table} SET {sets}')


def upgrade() -> None:
    _rescale(QTY, 10_000)
    _rescale(PRICE, 100)
    op.add_column('securities', sa.Column('currency', sa.String(3), nullable=False, server_default='INR'))
    op.add_column('holding_positions', sa.Column('currency', sa.String(3), nullable=False, server_default='INR'))
    op.create_table(
        'market_flows',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('flow_date', sa.Date, nullable=False, index=True),
        sa.Column('category', sa.String(8), nullable=False),
        sa.Column('buy_value', sa.BigInteger),
        sa.Column('sell_value', sa.BigInteger),
        sa.Column('net_value', sa.BigInteger, nullable=False),
        sa.Column('source', sa.String(40), nullable=False),
        sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('flow_date', 'category'),
    )


def downgrade() -> None:
    op.drop_table('market_flows')
    op.drop_column('holding_positions', 'currency')
    op.drop_column('securities', 'currency')
    _rescale(PRICE, 100, divide=True)
    _rescale(QTY, 10_000, divide=True)
