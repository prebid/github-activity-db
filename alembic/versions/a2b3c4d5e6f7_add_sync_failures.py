"""add sync failures table

Revision ID: a2b3c4d5e6f7
Revises: 01421d8dfaeb
Create Date: 2026-01-17 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a2b3c4d5e6f7'
down_revision: Union[str, Sequence[str], None] = '01421d8dfaeb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add sync_failures table for tracking failed PR ingestion attempts."""
    op.create_table('sync_failures',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('repository_id', sa.Integer(), nullable=False),
        sa.Column('pr_number', sa.Integer(), nullable=False),
        sa.Column('error_message', sa.Text(), nullable=False),
        sa.Column('error_type', sa.String(length=100), nullable=False),
        sa.Column('retry_count', sa.Integer(), nullable=False),
        sa.Column('status', sa.Enum('PENDING', 'RESOLVED', 'PERMANENT', name='syncfailurestatus'), nullable=False),
        sa.Column('failed_at', sa.DateTime(), nullable=False),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['repository_id'], ['repositories.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('repository_id', 'pr_number', 'status', name='uq_repo_pr_pending_status')
    )
    # Add index for common query patterns
    op.create_index('ix_sync_failures_status', 'sync_failures', ['status'])
    op.create_index('ix_sync_failures_repository_id', 'sync_failures', ['repository_id'])


def downgrade() -> None:
    """Remove sync_failures table."""
    op.drop_index('ix_sync_failures_repository_id', table_name='sync_failures')
    op.drop_index('ix_sync_failures_status', table_name='sync_failures')
    op.drop_table('sync_failures')
    # Drop the enum type
    sa.Enum(name='syncfailurestatus').drop(op.get_bind(), checkfirst=True)
