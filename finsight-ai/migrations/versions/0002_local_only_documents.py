"""Track whether a document was embedded in Pinecone (local-only uploads are not).

Uses a plain ADD COLUMN (supported by SQLite and PostgreSQL) instead of batch mode: batch mode
rebuilds the table by dropping it, which SQLite refuses while other tables reference its rows.

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    # Clean up a temp table left by an earlier failed batch-mode attempt of this migration.
    if '_alembic_tmp_documents' in inspector.get_table_names():
        op.drop_table('_alembic_tmp_documents')
    if 'indexed_in_rag' not in {c['name'] for c in inspector.get_columns('documents')}:
        # Every document uploaded before this migration was indexed.
        op.add_column('documents', sa.Column('indexed_in_rag', sa.Boolean, nullable=False, server_default=sa.true()))


def downgrade() -> None:
    op.drop_column('documents', 'indexed_in_rag')
