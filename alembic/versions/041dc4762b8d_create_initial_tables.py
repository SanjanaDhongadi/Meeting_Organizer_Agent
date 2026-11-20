"""create_initial_tables

Revision ID: 041dc4762b8d
Revises: 
Create Date: 2026-11-09 18:15:35.883928

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '041dc4762b8d'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # employees
    op.create_table(
        'employees',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('employee_id', sa.String(length=50), nullable=False),
        sa.Column('name', sa.String(length=150), nullable=False),
        sa.Column('email', sa.String(length=150), nullable=False),
        sa.Column('designation', sa.String(length=150), nullable=False),
        sa.Column('department', sa.String(length=100), nullable=False),
        sa.Column('working_days', sa.String(length=100), server_default='Monday,Tuesday,Wednesday,Thursday,Friday', nullable=True),
        sa.Column('working_hours_start', sa.String(length=10), server_default='09:00:00', nullable=True),
        sa.Column('working_hours_end', sa.String(length=10), server_default='17:00:00', nullable=True),
        sa.Column('timezone', sa.String(length=50), server_default='America/New_York', nullable=True),
        sa.Column('meeting_preferences', sa.Text(), server_default='Prefers 30-min meetings, online preferred', nullable=True),
        sa.Column('preferred_duration', sa.Integer(), server_default='30', nullable=True),
        sa.Column('mode_preference', sa.String(length=30), server_default='Online', nullable=True),
        sa.Column('location', sa.String(length=200), server_default='HQ Tech Park, Building A', nullable=True),
        sa.Column('other_info', sa.Text(), server_default='', nullable=True),
        sa.Column('google_calendar_connected', sa.Boolean(), server_default=sa.text('false'), nullable=True),
        sa.Column('google_tokens', sa.Text(), server_default='{}', nullable=True),
        sa.Column('embedding', sa.Text(), server_default='[]', nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('email'),
        sa.UniqueConstraint('employee_id')
    )
    op.create_index('ix_employees_email', 'employees', ['email'], unique=False)
    op.create_index('ix_employees_employee_id', 'employees', ['employee_id'], unique=False)
    op.create_index('uq_employees_email_lower', 'employees', [sa.text('lower(email)')], unique=True)

    # meetings
    op.create_table(
        'meetings',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('raw_request', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=50), server_default='DRAFT', nullable=True),
        sa.Column('mode', sa.String(length=30), server_default='ONLINE', nullable=True),
        sa.Column('scheduled_start', sa.String(length=50), nullable=True),
        sa.Column('scheduled_end', sa.String(length=50), nullable=True),
        sa.Column('duration_minutes', sa.Integer(), server_default='30', nullable=True),
        sa.Column('purpose', sa.Text(), server_default='', nullable=True),
        sa.Column('agenda', sa.Text(), server_default='', nullable=True),
        sa.Column('room_name', sa.String(length=100), server_default='', nullable=True),
        sa.Column('meet_url', sa.String(length=255), server_default='', nullable=True),
        sa.Column('calendar_event_id', sa.String(length=255), server_default='', nullable=True),
        sa.Column('approval_status', sa.String(length=30), server_default='PENDING', nullable=True),
        sa.Column('approval_notes', sa.Text(), server_default='', nullable=True),
        sa.Column('parsed_details', sa.Text(), server_default='{}', nullable=True),
        sa.Column('conflicts', sa.Text(), server_default='[]', nullable=True),
        sa.Column('critique_notes', sa.Text(), server_default='', nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_meetings_status', 'meetings', ['status'], unique=False)

    # meeting_participants
    op.create_table(
        'meeting_participants',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('meeting_id', sa.String(length=64), nullable=True),
        sa.Column('employee_id', sa.String(length=50), nullable=True),
        sa.Column('name', sa.String(length=150), nullable=False),
        sa.Column('email', sa.String(length=150), nullable=False),
        sa.Column('is_external', sa.Boolean(), server_default=sa.text('false'), nullable=True),
        sa.Column('calendar_status', sa.String(length=30), server_default='UNVERIFIED', nullable=True),
        sa.Column('response_status', sa.String(length=30), server_default='PENDING', nullable=True),
        sa.Column('response_time', sa.String(length=50), nullable=True),
        sa.Column('notes', sa.Text(), server_default='', nullable=True),
        sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_meeting_participants_meeting_id', 'meeting_participants', ['meeting_id'], unique=False)

    # room_bookings
    op.create_table(
        'room_bookings',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('meeting_id', sa.String(length=64), nullable=True),
        sa.Column('room_name', sa.String(length=100), nullable=False),
        sa.Column('capacity', sa.Integer(), server_default='10', nullable=True),
        sa.Column('equipment_requirements', sa.Text(), server_default='', nullable=True),
        sa.Column('status', sa.String(length=30), server_default='PENDING', nullable=True),
        sa.Column('response_time', sa.String(length=50), nullable=True),
        sa.Column('notes', sa.Text(), server_default='', nullable=True),
        sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_room_bookings_meeting_id', 'room_bookings', ['meeting_id'], unique=False)

    # oauth_pending_states
    op.create_table(
        'oauth_pending_states',
        sa.Column('state', sa.String(length=512), nullable=False),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('code_verifier', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('state')
    )
    op.create_index('ix_oauth_pending_states_created_at', 'oauth_pending_states', ['created_at'], unique=False)

    # audit_logs
    op.create_table(
        'audit_logs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('meeting_id', sa.String(length=64), nullable=True),
        sa.Column('timestamp', sa.DateTime(), nullable=True),
        sa.Column('agent', sa.String(length=100), nullable=False),
        sa.Column('action', sa.String(length=100), nullable=False),
        sa.Column('tool', sa.String(length=100), server_default='', nullable=True),
        sa.Column('status', sa.String(length=30), server_default='SUCCESS', nullable=False),
        sa.Column('error', sa.Text(), server_default='', nullable=True),
        sa.Column('approval_status', sa.String(length=30), server_default='', nullable=True),
        sa.Column('details', sa.Text(), server_default='{}', nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_audit_logs_meeting_id', 'audit_logs', ['meeting_id'], unique=False)
    op.create_index('ix_audit_logs_timestamp', 'audit_logs', ['timestamp'], unique=False)


def downgrade() -> None:
    op.drop_table('audit_logs')
    op.drop_table('oauth_pending_states')
    op.drop_table('room_bookings')
    op.drop_table('meeting_participants')
    op.drop_table('meetings')
    op.drop_table('employees')
