"""item_zone_map.title — заголовок объявления для /admin/dialogs

Revision ID: b1f4c7d20a3e
Revises: 27e9554f4946
Create Date: 2026-08-27 13:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b1f4c7d20a3e'
down_revision: Union[str, Sequence[str], None] = '27e9554f4946'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # nullable=True без server_default: колонка чисто справочная (оператору
    # в /admin/dialogs видеть, из какого объявления пришёл клиент), у
    # существующих строк заголовка просто нет, пока не отработает
    # `python -m scripts.export_listings --seed-map`.
    op.add_column('item_zone_map', sa.Column('title', sa.String(length=512), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('item_zone_map', 'title')
