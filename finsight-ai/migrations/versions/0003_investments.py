"""Investment accounts, securities, holdings snapshots, broker fund ledgers, investment transactions.

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'investment_accounts',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(64), nullable=False, index=True),
        sa.Column('platform', sa.String(30), nullable=False),
        sa.Column('account_mask', sa.String(16), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('user_id', 'platform', 'account_mask'),
    )
    op.create_table(
        'securities',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('isin', sa.String(12), unique=True),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('asset_type', sa.String(20), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        'investment_imports',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(64), nullable=False, index=True),
        sa.Column('document_id', sa.Text, sa.ForeignKey('documents.id'), nullable=False, unique=True),
        sa.Column('account_id', sa.String(36), sa.ForeignKey('investment_accounts.id')),
        sa.Column('parser', sa.String(40), nullable=False),
        sa.Column('kind', sa.String(24), nullable=False),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('reconciliation_status', sa.String(20), nullable=False),
        sa.Column('reconciliation_json', sa.Text, nullable=False),
        sa.Column('warnings_json', sa.Text, nullable=False),
        sa.Column('as_of', sa.Date),
        sa.Column('period_start', sa.Date),
        sa.Column('period_end', sa.Date),
        sa.Column('stated_total_value', sa.BigInteger),
        sa.Column('rows_found', sa.Integer, nullable=False),
        sa.Column('rows_inserted', sa.Integer, nullable=False),
        sa.Column('duplicates_skipped', sa.Integer, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        'holding_positions',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(64), nullable=False, index=True),
        sa.Column('import_id', sa.String(36), sa.ForeignKey('investment_imports.id'), nullable=False, index=True),
        sa.Column('account_id', sa.String(36), sa.ForeignKey('investment_accounts.id'), nullable=False, index=True),
        sa.Column('security_id', sa.String(36), sa.ForeignKey('securities.id'), nullable=False),
        sa.Column('as_of', sa.Date, nullable=False),
        sa.Column('quantity', sa.BigInteger, nullable=False),
        sa.Column('free_quantity', sa.BigInteger),
        sa.Column('pledged_quantity', sa.BigInteger),
        sa.Column('locked_quantity', sa.BigInteger),
        sa.Column('reported_rate', sa.BigInteger),
        sa.Column('reported_value', sa.BigInteger),
        sa.Column('source_page', sa.Integer),
        sa.Column('source_row', sa.Integer),
    )
    op.create_table(
        'broker_ledger_entries',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(64), nullable=False, index=True),
        sa.Column('account_id', sa.String(36), sa.ForeignKey('investment_accounts.id'), nullable=False, index=True),
        sa.Column('document_id', sa.Text, sa.ForeignKey('documents.id'), nullable=False),
        sa.Column('import_id', sa.String(36), sa.ForeignKey('investment_imports.id'), nullable=False),
        sa.Column('entry_date', sa.Date, nullable=False, index=True),
        sa.Column('settlement_date', sa.Date),
        sa.Column('segment_type', sa.String(40), nullable=False),
        sa.Column('transaction_type', sa.String(40), nullable=False),
        sa.Column('kind', sa.String(20), nullable=False),
        sa.Column('reference', sa.String(64)),
        sa.Column('debit', sa.BigInteger, nullable=False),
        sa.Column('credit', sa.BigInteger, nullable=False),
        sa.Column('balance', sa.BigInteger),
        sa.Column('source_row', sa.Integer),
        sa.Column('fingerprint', sa.String(64), nullable=False),
        sa.UniqueConstraint('account_id', 'fingerprint'),
    )
    op.create_table(
        'investment_transactions',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(64), nullable=False, index=True),
        sa.Column('account_id', sa.String(36), sa.ForeignKey('investment_accounts.id'), nullable=False, index=True),
        sa.Column('document_id', sa.Text, sa.ForeignKey('documents.id'), nullable=False),
        sa.Column('import_id', sa.String(36), sa.ForeignKey('investment_imports.id'), nullable=False),
        sa.Column('security_id', sa.String(36), sa.ForeignKey('securities.id'), nullable=False),
        sa.Column('trade_date', sa.Date, nullable=False, index=True),
        sa.Column('transaction_type', sa.String(20), nullable=False),
        sa.Column('quantity', sa.BigInteger),
        sa.Column('price', sa.BigInteger),
        sa.Column('amount', sa.BigInteger, nullable=False),
        sa.Column('source_row', sa.Integer),
        sa.Column('fingerprint', sa.String(64), nullable=False),
        sa.UniqueConstraint('account_id', 'fingerprint'),
    )


def downgrade() -> None:
    for t in ('investment_transactions', 'broker_ledger_entries', 'holding_positions', 'investment_imports',
              'securities', 'investment_accounts'):
        op.drop_table(t)
