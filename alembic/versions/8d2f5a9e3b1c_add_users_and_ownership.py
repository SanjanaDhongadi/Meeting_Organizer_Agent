"""add_users_and_ownership

Revision ID: 8d2f5a9e3b1c
Revises: 041dc4762b8d
Create Date: 2026-11-09 19:55:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8d2f5a9e3b1c'
down_revision: Union[str, Sequence[str], None] = '041dc4762b8d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create users table
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('email', sa.String(length=150), nullable=False),
        sa.Column('name', sa.String(length=150), nullable=False),
        sa.Column('password_hash', sa.String(length=255), nullable=True),
        sa.Column('google_id', sa.String(length=100), nullable=True),
        sa.Column('google_calendar_connected', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('google_tokens', sa.Text(), server_default='{}', nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('email'),
        sa.UniqueConstraint('google_id')
    )
    op.create_index('ix_users_email', 'users', ['email'], unique=False)
    op.create_index('uq_users_email_lower', 'users', [sa.text('lower(email)')], unique=True)

    # 2. Add organizer_id to meetings table
    op.add_column('meetings', sa.Column('organizer_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_meetings_organizer_id_users',
        'meetings', 'users',
        ['organizer_id'], ['id'],
        ondelete='SET NULL'
    )
    op.create_index('ix_meetings_organizer_id', 'meetings', ['organizer_id'], unique=False)

    # 3. Add attended and attendance_recorded to meeting_participants
    op.add_column('meeting_participants', sa.Column('attended', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.add_column('meeting_participants', sa.Column('attendance_recorded', sa.Boolean(), server_default=sa.text('false'), nullable=False))

    # 4. Modify oauth_pending_states table to support user_id and flow_type, make employee_id nullable
    op.alter_column('oauth_pending_states', 'employee_id', existing_type=sa.Integer(), nullable=True)
    op.add_column('oauth_pending_states', sa.Column('user_id', sa.Integer(), nullable=True))
    op.add_column('oauth_pending_states', sa.Column('flow_type', sa.String(length=50), server_default='calendar', nullable=True))
    op.create_foreign_key(
        'fk_oauth_pending_states_user_id_users',
        'oauth_pending_states', 'users',
        ['user_id'], ['id'],
        ondelete='CASCADE'
    )


def downgrade() -> None:
    # 4. Revert oauth_pending_states
    op.drop_constraint('fk_oauth_pending_states_user_id_users', 'oauth_pending_states', type_='foreignkey')
    op.drop_column('oauth_pending_states', 'flow_type')
    op.drop_column('oauth_pending_states', 'user_id')
    op.alter_column('oauth_pending_states', 'employee_id', existing_type=sa.Integer(), nullable=False)

    # 3. Revert meeting_participants columns
    op.drop_column('meeting_participants', 'attendance_recorded')
    op.drop_column('meeting_participants', 'attended')

    # 2. Revert meetings organizer_id
    op.drop_index('ix_meetings_organizer_id', table_name='meetings')
    op.drop_constraint('fk_meetings_organizer_id_users', 'meetings', type_='foreignkey')
    op.drop_column('meetings', 'organizer_id')

    # 1. Revert users table
    op.drop_index('uq_users_email_lower', table_name='users')
    op.drop_index('ix_users_email', table_name='users')
    op.drop_table('users')

