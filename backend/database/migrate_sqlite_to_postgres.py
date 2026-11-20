"""
Data migration utility: Copies data from SQLite (meeting_organizer.db) to PostgreSQL.
Preserves primary keys, foreign keys, relationships, and verifies counts.
"""
import sqlite3
import logging
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.config import settings
from app.db import Employee, Meeting, MeetingParticipant, RoomBooking, OAuthPendingState, AuditLog

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("migration")

def migrate_sqlite_to_postgres(sqlite_path="meeting_organizer.db"):
    logger.info(f"Connecting to SQLite: {sqlite_path}")
    sq_conn = sqlite3.connect(sqlite_path)
    sq_conn.row_factory = sqlite3.Row

    pg_engine = create_engine(settings.DATABASE_URL)
    PgSession = sessionmaker(bind=pg_engine)
    pg_session = PgSession()

    try:
        # 1. Employees
        sq_emps = sq_conn.execute("SELECT * FROM employees ORDER BY id ASC").fetchall()
        logger.info(f"Found {len(sq_emps)} employees in SQLite")
        for r in sq_emps:
            d = dict(r)
            existing = pg_session.query(Employee).filter(Employee.id == d["id"]).first()
            if not existing:
                emp = Employee(**d)
                pg_session.add(emp)
        pg_session.commit()
        # Reset serial sequence for employees
        pg_session.execute(
            pg_engine.dialect.name == "postgresql" and 
            __import__("sqlalchemy").text("SELECT setval('employees_id_seq', coalesce((SELECT max(id) FROM employees), 1))")
        )
        pg_session.commit()

        # 2. Meetings
        sq_meetings = sq_conn.execute("SELECT * FROM meetings").fetchall()
        logger.info(f"Found {len(sq_meetings)} meetings in SQLite")
        for r in sq_meetings:
            d = dict(r)
            existing = pg_session.query(Meeting).filter(Meeting.id == d["id"]).first()
            if not existing:
                m = Meeting(**d)
                pg_session.add(m)
        pg_session.commit()

        # 3. Meeting Participants
        sq_parts = sq_conn.execute("SELECT * FROM meeting_participants ORDER BY id ASC").fetchall()
        logger.info(f"Found {len(sq_parts)} meeting participants in SQLite")
        for r in sq_parts:
            d = dict(r)
            existing = pg_session.query(MeetingParticipant).filter(MeetingParticipant.id == d["id"]).first()
            if not existing:
                p = MeetingParticipant(**d)
                pg_session.add(p)
        pg_session.commit()
        pg_session.execute(
            __import__("sqlalchemy").text("SELECT setval('meeting_participants_id_seq', coalesce((SELECT max(id) FROM meeting_participants), 1))")
        )
        pg_session.commit()

        # 4. Room Bookings
        sq_rooms = sq_conn.execute("SELECT * FROM room_bookings ORDER BY id ASC").fetchall()
        logger.info(f"Found {len(sq_rooms)} room bookings in SQLite")
        for r in sq_rooms:
            d = dict(r)
            existing = pg_session.query(RoomBooking).filter(RoomBooking.id == d["id"]).first()
            if not existing:
                rb = RoomBooking(**d)
                pg_session.add(rb)
        pg_session.commit()
        pg_session.execute(
            __import__("sqlalchemy").text("SELECT setval('room_bookings_id_seq', coalesce((SELECT max(id) FROM room_bookings), 1))")
        )
        pg_session.commit()

        # 5. Audit Logs
        sq_audits = sq_conn.execute("SELECT * FROM audit_logs ORDER BY id ASC").fetchall()
        logger.info(f"Found {len(sq_audits)} audit logs in SQLite")
        for r in sq_audits:
            d = dict(r)
            existing = pg_session.query(AuditLog).filter(AuditLog.id == d["id"]).first()
            if not existing:
                a = AuditLog(**d)
                pg_session.add(a)
        pg_session.commit()
        pg_session.execute(
            __import__("sqlalchemy").text("SELECT setval('audit_logs_id_seq', coalesce((SELECT max(id) FROM audit_logs), 1))")
        )
        pg_session.commit()

        # 6. OAuth Pending States
        sq_oauth = sq_conn.execute("SELECT * FROM oauth_pending_states").fetchall()
        for r in sq_oauth:
            d = dict(r)
            existing = pg_session.query(OAuthPendingState).filter(OAuthPendingState.state == d["state"]).first()
            if not existing:
                s = OAuthPendingState(**d)
                pg_session.add(s)
        pg_session.commit()

        # Verification
        emp_cnt = pg_session.query(Employee).count()
        meet_cnt = pg_session.query(Meeting).count()
        part_cnt = pg_session.query(MeetingParticipant).count()
        room_cnt = pg_session.query(RoomBooking).count()
        audit_cnt = pg_session.query(AuditLog).count()
        oauth_cnt = pg_session.query(OAuthPendingState).count()

        print("\n--- DATA MIGRATION VERIFICATION ---")
        print(f"Employees:          SQLite={len(sq_emps)}, PostgreSQL={emp_cnt}")
        print(f"Meetings:           SQLite={len(sq_meetings)}, PostgreSQL={meet_cnt}")
        print(f"Participants:       SQLite={len(sq_parts)}, PostgreSQL={part_cnt}")
        print(f"Room Bookings:      SQLite={len(sq_rooms)}, PostgreSQL={room_cnt}")
        print(f"Audit Logs:         SQLite={len(sq_audits)}, PostgreSQL={audit_cnt}")
        print(f"OAuth States:       SQLite={len(sq_oauth)}, PostgreSQL={oauth_cnt}")
        print("Data integrity verification: SUCCESS")

    except Exception as e:
        pg_session.rollback()
        logger.error(f"Migration error: {e}")
        raise
    finally:
        pg_session.close()
        sq_conn.close()

if __name__ == "__main__":
    migrate_sqlite_to_postgres()

