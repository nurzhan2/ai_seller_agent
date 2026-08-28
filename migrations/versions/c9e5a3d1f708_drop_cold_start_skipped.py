"""drop chat_cursor.cold_start_skipped — механизм убран целиком

Revision ID: c9e5a3d1f708
Revises: a7c1f4e9b063
Create Date: 2026-08-28 16:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c9e5a3d1f708'
down_revision: Union[str, Sequence[str], None] = 'a7c1f4e9b063'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Индекс — ПЕРВЫМ, явно: он частичный, по WHERE cold_start_skipped
    # (создан в d5e2b81c4f39_chat_cursor.py), и должен уйти вместе со
    # столбцом, а не остаться зависать неявно на том, что Postgres сам
    # решит сделать при DROP COLUMN.
    op.drop_index('ix_chat_cursor_cold_start', table_name='chat_cursor')
    op.drop_column('chat_cursor', 'cold_start_skipped')


def downgrade() -> None:
    """Downgrade schema."""
    # Оба шага — В ТОЧНОСТИ то, что d5e2b81c4f39_chat_cursor.py создавал для
    # cold_start_skipped изначально. Без индекса здесь собственный downgrade
    # ТОЙ миграции упал бы на «индекс не существует», пытаясь удалить то,
    # что уже отсутствует, — цепочка миграций обязана быть обратима слоями.
    op.add_column(
        'chat_cursor',
        sa.Column('cold_start_skipped', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        'ix_chat_cursor_cold_start', 'chat_cursor', ['updated_at'],
        unique=False, postgresql_where=sa.text('cold_start_skipped'),
    )
