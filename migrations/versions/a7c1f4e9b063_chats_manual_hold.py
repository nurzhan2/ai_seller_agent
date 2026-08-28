"""chats.manual_hold — ручной hold, отдельный от is_human_takeover

Revision ID: a7c1f4e9b063
Revises: d5e2b81c4f39
Create Date: 2026-08-28 15:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a7c1f4e9b063'
down_revision: Union[str, Sequence[str], None] = 'd5e2b81c4f39'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # server_default=false — существующие чаты не должны внезапно замолчать
    # на миграции; hold ставится явно, точечно, по chat_id.
    op.add_column(
        'chats',
        sa.Column('manual_hold', sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('chats', 'manual_hold')
