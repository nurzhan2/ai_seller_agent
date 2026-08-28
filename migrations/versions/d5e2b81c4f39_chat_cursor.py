"""chat_cursor — докуда поллер прочитал каждый чат

Revision ID: d5e2b81c4f39
Revises: c3a91e5f7b02
Create Date: 2026-08-28 14:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd5e2b81c4f39'
down_revision: Union[str, Sequence[str], None] = 'c3a91e5f7b02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # БЕЗ внешнего ключа на chats.chat_id — СОЗНАТЕЛЬНО. Курсор ставится и на
    # чаты, которых в `chats` нет и не будет: пропущенные холодным стартом,
    # по чужим объявлениям, по вакансии. Смысл строки как раз в том, чтобы
    # помнить «этот чат прочитан и трогать его не надо», а внешний ключ
    # требовал бы сперва завести диалог — то есть ровно то, чего мы избегаем.
    op.create_table(
        'chat_cursor',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('chat_id', sa.String(length=128), nullable=False),
        # BigInteger, а не timestamptz: unix-секунды как их отдаёт API Авито.
        # Сравнение целых не зависит от часового пояса и от разбора даты
        # драйвером — см. докстринг app/db/models.py:ChatCursor.
        sa.Column('last_message_created', sa.BigInteger(), nullable=False,
                  server_default='0'),
        # Идентификаторы сообщений ровно на last_message_created: время
        # секундное, и без этого списка второе сообщение той же секунды либо
        # теряется (при курсоре «строго больше»), либо подаётся заново вечно
        # (при «больше или равно»).
        sa.Column('last_message_ids', sa.ARRAY(sa.String()), nullable=True),
        sa.Column('cold_start_skipped', sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column('skipped_reason', sa.String(length=64), nullable=True),
        sa.Column('item_id', sa.String(length=128), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_chat_cursor_chat_id'), 'chat_cursor', ['chat_id'], unique=True)
    op.create_index(op.f('ix_chat_cursor_item_id'), 'chat_cursor', ['item_id'], unique=False)
    # Список для /admin/dialogs: «кого поллер промолчал на холодном старте».
    # Частичный индекс, потому что читают только строки с флагом, а их доля
    # мала — полный индекс по булеву полю здесь платил бы за 99% ненужного.
    op.create_index(
        'ix_chat_cursor_cold_start', 'chat_cursor', ['updated_at'],
        unique=False, postgresql_where=sa.text('cold_start_skipped'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_chat_cursor_cold_start', table_name='chat_cursor')
    op.drop_index(op.f('ix_chat_cursor_item_id'), table_name='chat_cursor')
    op.drop_index(op.f('ix_chat_cursor_chat_id'), table_name='chat_cursor')
    op.drop_table('chat_cursor')
