"""add_meeting_minutes_and_action_items

Revision ID: a3c7e91b5d20
Revises: 8d2f5a9e3b1c
Create Date: 2026-11-13 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a3c7e91b5d20'
down_revision: Union[str, Sequence[str], None] = '8d2f5a9e3b1c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'meeting_minutes',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('meeting_id', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=40), nullable=False, server_default='WAITING_FOR_TRANSCRIPT'),
        sa.Column('transcript_text', sa.Text(), server_default='', nullable=True),
        sa.Column('transcript_meta', sa.Text(), server_default='{}', nullable=True),
        sa.Column('retrieval_attempts', sa.Integer(), server_default='0', nullable=True),
        sa.Column('next_attempt_at', sa.DateTime(), nullable=True),
        sa.Column('last_error', sa.Text(), server_default='', nullable=True),
        sa.Column('mom_draft', sa.Text(), server_default='{}', nullable=True),
        sa.Column('generated_at', sa.DateTime(), nullable=True),
        sa.Column('approved_at', sa.DateTime(), nullable=True),
        sa.Column('approved_by', sa.Integer(), nullable=True),
        sa.Column('distributed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['approved_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_meeting_minutes_meeting_id', 'meeting_minutes', ['meeting_id'], unique=True)
    op.create_index('ix_meeting_minutes_status', 'meeting_minutes', ['status'], unique=False)
    op.create_index('ix_meeting_minutes_next_attempt_at', 'meeting_minutes', ['next_attempt_at'], unique=False)

    op.create_table(
        'action_items',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('meeting_id', sa.String(length=64), nullable=False),
        sa.Column('organizer_id', sa.Integer(), nullable=True),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('owner_name', sa.String(length=150), server_default='', nullable=True),
        sa.Column('owner_email', sa.String(length=150), nullable=True),
        sa.Column('due_date', sa.String(length=10), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='NOT_STARTED'),
        sa.Column('project_topic', sa.String(length=255), server_default='', nullable=True),
        sa.Column('source_quote', sa.Text(), server_default='', nullable=True),
        sa.Column('finalized', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['organizer_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_action_items_meeting_id', 'action_items', ['meeting_id'], unique=False)
    op.create_index('ix_action_items_organizer_id', 'action_items', ['organizer_id'], unique=False)
    op.create_index('ix_action_items_owner_email', 'action_items', ['owner_email'], unique=False)
    op.create_index('ix_action_items_status', 'action_items', ['status'], unique=False)
    op.create_index('ix_action_items_finalized', 'action_items', ['finalized'], unique=False)


def downgrade() -> None:
    for index in ('ix_action_items_finalized', 'ix_action_items_status', 'ix_action_items_owner_email',
                  'ix_action_items_organizer_id', 'ix_action_items_meeting_id'):
        op.drop_index(index, table_name='action_items')
    op.drop_table('action_items')
    for index in ('ix_meeting_minutes_next_attempt_at', 'ix_meeting_minutes_status', 'ix_meeting_minutes_meeting_id'):
        op.drop_index(index, table_name='meeting_minutes')
    op.drop_table('meeting_minutes')
