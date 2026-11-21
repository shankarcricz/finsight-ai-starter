"""Baseline Phase 1 tables + structured bank statement tables.

Phase 1 created documents/holdings/alerts with raw SQL, so those are created only if missing.
This lets an existing development database upgrade in place without losing rows.

Revision ID: 0001
Revises:
"""
from alembic import op
import sqlalchemy as sa

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())

    if 'documents' not in existing:
        op.create_table(
            'documents',
            sa.Column('id', sa.Text, primary_key=True),
            sa.Column('user_id', sa.Text, nullable=False),
            sa.Column('category', sa.Text, nullable=False),
            sa.Column('filename', sa.Text, nullable=False),
            sa.Column('sha256', sa.Text, nullable=False),
            sa.Column('pages', sa.Integer, nullable=False),
            sa.Column('created_at', sa.Text, nullable=False, server_default=sa.func.current_timestamp()),
            sa.UniqueConstraint('user_id', 'sha256', 'category'),
        )
    if 'holdings' not in existing:
        op.create_table(
            'holdings',
            sa.Column('user_id', sa.Text, primary_key=True),
            sa.Column('broker', sa.Text, primary_key=True),
            sa.Column('symbol', sa.Text, primary_key=True),
            sa.Column('quantity', sa.Float, nullable=False),
            sa.Column('avg_cost', sa.Float, nullable=False),
            sa.Column('currency', sa.Text, nullable=False, server_default='INR'),
            sa.Column('updated_at', sa.Text, nullable=False, server_default=sa.func.current_timestamp()),
        )
    if 'alerts' not in existing:
        op.create_table(
            'alerts',
            sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
            sa.Column('user_id', sa.Text, nullable=False),
            sa.Column('kind', sa.Text, nullable=False),
            sa.Column('threshold', sa.Float, nullable=False),
            sa.Column('enabled', sa.Integer, nullable=False, server_default='1'),
            sa.Column('last_sent_date', sa.Text),
        )

    op.create_table(
        'financial_accounts',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(64), nullable=False, index=True),
        sa.Column('institution', sa.String(80), nullable=False),
        sa.Column('account_mask', sa.String(16), nullable=False),
        sa.Column('account_type', sa.String(20), nullable=False),
        sa.Column('currency', sa.String(3), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('user_id', 'institution', 'account_mask'),
    )
    op.create_table(
        'statement_imports',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(64), nullable=False, index=True),
        sa.Column('document_id', sa.Text, sa.ForeignKey('documents.id'), nullable=False, unique=True),
        sa.Column('account_id', sa.String(36), sa.ForeignKey('financial_accounts.id')),
        sa.Column('parser', sa.String(40), nullable=False),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('period_start', sa.Date),
        sa.Column('period_end', sa.Date),
        sa.Column('currency', sa.String(3), nullable=False),
        sa.Column('opening_balance', sa.BigInteger),
        sa.Column('closing_balance', sa.BigInteger),
        sa.Column('stated_total_credits', sa.BigInteger),
        sa.Column('stated_total_debits', sa.BigInteger),
        sa.Column('reconciliation_status', sa.String(20), nullable=False),
        sa.Column('reconciliation_json', sa.Text, nullable=False),
        sa.Column('warnings_json', sa.Text, nullable=False),
        sa.Column('transactions_found', sa.Integer, nullable=False),
        sa.Column('transactions_inserted', sa.Integer, nullable=False),
        sa.Column('duplicates_skipped', sa.Integer, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        'bank_transactions',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(64), nullable=False, index=True),
        sa.Column('account_id', sa.String(36), sa.ForeignKey('financial_accounts.id'), nullable=False, index=True),
        sa.Column('document_id', sa.Text, sa.ForeignKey('documents.id'), nullable=False, index=True),
        sa.Column('import_id', sa.String(36), sa.ForeignKey('statement_imports.id'), nullable=False),
        sa.Column('source_page', sa.Integer),
        sa.Column('source_row', sa.Integer),
        sa.Column('transaction_date', sa.Date, nullable=False, index=True),
        sa.Column('description', sa.String(500), nullable=False),
        sa.Column('original_description', sa.String(500), nullable=False),
        sa.Column('merchant', sa.String(120), nullable=False, index=True),
        sa.Column('amount', sa.BigInteger, nullable=False),
        sa.Column('transaction_type', sa.String(6), nullable=False),
        sa.Column('currency', sa.String(3), nullable=False),
        sa.Column('balance_after_transaction', sa.BigInteger),
        sa.Column('reference_number', sa.String(64)),
        sa.Column('category', sa.String(40), nullable=False, index=True),
        sa.Column('category_source', sa.String(10), nullable=False),
        sa.Column('category_confidence', sa.Float, nullable=False),
        sa.Column('extraction_status', sa.String(16), nullable=False),
        sa.Column('extraction_confidence', sa.Float, nullable=False),
        sa.Column('fingerprint', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('account_id', 'fingerprint'),
    )
    op.create_table(
        'merchant_category_overrides',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.String(64), nullable=False),
        sa.Column('merchant', sa.String(120), nullable=False),
        sa.Column('category', sa.String(40), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('user_id', 'merchant'),
    )


def downgrade() -> None:
    # Phase 1 tables are intentionally left in place.
    for table in ('merchant_category_overrides', 'bank_transactions', 'statement_imports', 'financial_accounts'):
        op.drop_table(table)
