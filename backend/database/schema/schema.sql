-- ===============================================================
-- AI MEETING ORGANIZER AGENT - POSTGRESQL + PGVECTOR SCHEMA
-- ===============================================================

CREATE EXTENSION IF NOT EXISTS vector;

-- Table: employees (Dashboard 1 & Long-term Memory)
CREATE TABLE IF NOT EXISTS employees (
    id SERIAL PRIMARY KEY,
    employee_id VARCHAR(50) UNIQUE NOT NULL,
    name VARCHAR(150) NOT NULL,
    email VARCHAR(150) UNIQUE NOT NULL,
    designation VARCHAR(150) NOT NULL,
    department VARCHAR(100) NOT NULL,
    working_days VARCHAR(100) DEFAULT 'Monday,Tuesday,Wednesday,Thursday,Friday',
    working_hours_start TIME DEFAULT '09:00:00',
    working_hours_end TIME DEFAULT '17:00:00',
    timezone VARCHAR(50) DEFAULT 'UTC',
    meeting_preferences TEXT DEFAULT 'Prefers 30-min meetings, online preferred',
    preferred_duration INT DEFAULT 30,
    mode_preference VARCHAR(30) DEFAULT 'Online',
    location VARCHAR(200) DEFAULT 'HQ Tech Park, Building A',
    other_info TEXT DEFAULT '',
    google_calendar_connected BOOLEAN DEFAULT FALSE,
    google_tokens JSONB DEFAULT '{}'::jsonb,
    embedding vector(1536),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_employees_email_lower ON employees (LOWER(email));

CREATE INDEX IF NOT EXISTS employees_email_idx ON employees(email);
CREATE INDEX IF NOT EXISTS employees_emp_id_idx ON employees(employee_id);

-- Table: meetings (Workflow State & Orchestration)
CREATE TABLE IF NOT EXISTS meetings (
    id VARCHAR(64) PRIMARY KEY,
    title VARCHAR(255) NOT NULL,
    raw_request TEXT NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'DRAFT',
    mode VARCHAR(30) NOT NULL DEFAULT 'ONLINE',
    scheduled_start TIMESTAMP WITH TIME ZONE,
    scheduled_end TIMESTAMP WITH TIME ZONE,
    duration_minutes INT DEFAULT 30,
    purpose TEXT DEFAULT '',
    agenda TEXT DEFAULT '',
    room_name VARCHAR(100) DEFAULT '',
    meet_url VARCHAR(255) DEFAULT '',
    calendar_event_id VARCHAR(255) DEFAULT '',
    approval_status VARCHAR(30) DEFAULT 'PENDING',
    approval_notes TEXT DEFAULT '',
    parsed_details JSONB DEFAULT '{}'::jsonb,
    conflicts JSONB DEFAULT '[]'::jsonb,
    critique_notes TEXT DEFAULT '',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS meetings_status_idx ON meetings(status);

-- Table: meeting_participants
CREATE TABLE IF NOT EXISTS meeting_participants (
    id SERIAL PRIMARY KEY,
    meeting_id VARCHAR(64) REFERENCES meetings(id) ON DELETE CASCADE,
    employee_id VARCHAR(50),
    name VARCHAR(150) NOT NULL,
    email VARCHAR(150) NOT NULL,
    is_external BOOLEAN DEFAULT FALSE,
    calendar_status VARCHAR(30) DEFAULT 'UNVERIFIED', -- AVAILABLE, BUSY, UNVERIFIED
    response_status VARCHAR(30) DEFAULT 'PENDING',    -- PENDING, ACCEPTED, REJECTED
    response_time TIMESTAMP WITH TIME ZONE,
    notes TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS mp_meeting_id_idx ON meeting_participants(meeting_id);

-- Table: room_bookings
CREATE TABLE IF NOT EXISTS room_bookings (
    id SERIAL PRIMARY KEY,
    meeting_id VARCHAR(64) REFERENCES meetings(id) ON DELETE CASCADE,
    room_name VARCHAR(100) NOT NULL,
    capacity INT DEFAULT 10,
    equipment_requirements TEXT DEFAULT '',
    status VARCHAR(30) DEFAULT 'PENDING', -- PENDING, CONFIRMED, REJECTED
    response_time TIMESTAMP WITH TIME ZONE,
    notes TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS rb_meeting_id_idx ON room_bookings(meeting_id);

-- Table: audit_logs (Audit Memory & Lab 6/7/8 Execution Traces)
CREATE TABLE IF NOT EXISTS audit_logs (
    id SERIAL PRIMARY KEY,
    meeting_id VARCHAR(64),
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    agent VARCHAR(100) NOT NULL,
    action VARCHAR(100) NOT NULL,
    tool VARCHAR(100) DEFAULT '',
    status VARCHAR(30) NOT NULL, -- SUCCESS, WARNING, ERROR, WAITING
    error TEXT DEFAULT '',
    approval_status VARCHAR(30) DEFAULT '',
    details JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS audit_logs_meeting_idx ON audit_logs(meeting_id);
CREATE INDEX IF NOT EXISTS audit_logs_timestamp_idx ON audit_logs(timestamp);
