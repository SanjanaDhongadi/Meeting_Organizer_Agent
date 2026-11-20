"""add_recording_segments

Revision ID: b5e2d4f81c36
Revises: a3c7e91b5d20
Create Date: 2026-11-13 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b5e2d4f81c36'
down_revision: Union[str, Sequence[str], None] = 'a3c7e91b5d20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'recording_segments',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('meeting_id', sa.String(length=64), nullable=False),
        sa.Column('segment_index', sa.Integer(), nullable=False),
        sa.Column('offset_seconds', sa.Float(), nullable=True),
        sa.Column('duration_seconds', sa.Float(), nullable=True),
        sa.Column('mime_type', sa.String(length=80), nullable=True),
        sa.Column('size_bytes', sa.Integer(), nullable=True),
        sa.Column('sha256', sa.String(length=64), nullable=True),
        sa.Column('storage_path', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=True),
        sa.Column('transcript_text', sa.Text(), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('uploaded_at', sa.DateTime(), nullable=True),
        sa.Column('transcribed_at', sa.DateTime(), nullable=True),
        sa.Column('audio_deleted_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('meeting_id', 'segment_index', name='uq_recording_segment'),
    )
    op.create_index('ix_recording_segments_meeting_id', 'recording_segments', ['meeting_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_recording_segments_meeting_id', table_name='recording_segments')
    op.drop_table('recording_segments')
