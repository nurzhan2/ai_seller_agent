"""bookings — брони, поставленные агентом

Revision ID: c3a91e5f7b02
Revises: b1f4c7d20a3e
Create Date: 2026-08-27 16:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c3a91e5f7b02'
down_revision: Union[str, Sequence[str], None] = 'b1f4c7d20a3e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # occupied_hours и billable_hours — обе колонки намеренно: при акции
    # «6-й час в подарок» гость занимает 6 часов, платит за 5. В YCLIENTS
    # блокируются занятые, в деньгах считаются оплаченные.
    op.create_table(
        'bookings',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('chat_id', sa.String(length=128), nullable=True),
        sa.Column('record_id', sa.String(length=128), nullable=True),
        sa.Column('zone_id', sa.String(length=64), nullable=True),
        sa.Column('booking_date', sa.Date(), nullable=True),
        sa.Column('start_time', sa.String(length=8), nullable=True),
        sa.Column('occupied_hours', sa.Integer(), nullable=True),
        sa.Column('billable_hours', sa.Integer(), nullable=True),
        sa.Column('guests', sa.Integer(), nullable=True),
        sa.Column('total', sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column('client_name', sa.String(length=255), nullable=True),
        sa.Column('client_phone', sa.String(length=64), nullable=True),
        sa.Column('applied_promo', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_bookings_chat_id'), 'bookings', ['chat_id'], unique=False)
    op.create_index(op.f('ix_bookings_record_id'), 'bookings', ['record_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_bookings_record_id'), table_name='bookings')
    op.drop_index(op.f('ix_bookings_chat_id'), table_name='bookings')
    op.drop_table('bookings')
