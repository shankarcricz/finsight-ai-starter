"""Users/preferences, security tickers, portfolio reviews, notifications and deliveries.

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('securities', sa.Column('symbol', sa.String(32)))
    op.create_table(
        'users',
        sa.Column('id', sa.String(128), primary_key=True),
        sa.Column('email', sa.String(254)),
        sa.Column('email_verified', sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column('email_enabled', sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column('weekly_review_enabled', sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column('price_alerts_enabled', sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column('price_move_threshold_pct', sa.Float, nullable=False, server_default='5.0'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        'portfolio_reviews',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(128), nullable=False, index=True),
        sa.Column('period_key', sa.String(40), nullable=False),
        sa.Column('status', sa.String(12), nullable=False),
        sa.Column('model', sa.String(60), nullable=False),
        sa.Column('inputs_json', sa.Text, nullable=False),
        sa.Column('report_json', sa.Text),
        sa.Column('error', sa.String(300)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('user_id', 'period_key'),
    )
    op.create_table(
        'notifications',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(128), nullable=False, index=True),
        sa.Column('kind', sa.String(24), nullable=False),
        sa.Column('title', sa.String(200), nullable=False),
        sa.Column('body', sa.String(1000), nullable=False),
        sa.Column('payload_json', sa.Text, nullable=False),
        sa.Column('dedupe_key', sa.String(120), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('read_at', sa.DateTime(timezone=True)),
        sa.UniqueConstraint('user_id', 'dedupe_key'),
    )
    op.create_table(
        'notification_deliveries',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('notification_id', sa.String(36), sa.ForeignKey('notifications.id'), nullable=False, index=True),
        sa.Column('channel', sa.String(12), nullable=False),
        sa.Column('status', sa.String(12), nullable=False),
        sa.Column('attempts', sa.Integer, nullable=False),
        sa.Column('last_error', sa.String(300)),
        sa.Column('sent_at', sa.DateTime(timezone=True)),
        sa.UniqueConstraint('notification_id', 'channel'),
    )


def downgrade() -> None:
    for t in ('notification_deliveries', 'notifications', 'portfolio_reviews', 'users'):
        op.drop_table(t)
    op.drop_column('securities', 'symbol')
