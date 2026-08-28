"""item_scope — item_id -> allow/deny, классификация по заголовку

Revision ID: f3a6c9b1d8e4
Revises: c9e5a3d1f708
Create Date: 2026-08-28 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f3a6c9b1d8e4'
down_revision: Union[str, Sequence[str], None] = 'c9e5a3d1f708'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'item_scope',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('item_id', sa.String(length=128), nullable=False),
        sa.Column('title', sa.String(length=512), nullable=True),
        sa.Column('decision', sa.String(length=8), nullable=False),
        sa.Column('reason', sa.String(length=64), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_item_scope_item_id'), 'item_scope', ['item_id'], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_item_scope_item_id'), table_name='item_scope')
    op.drop_table('item_scope')
