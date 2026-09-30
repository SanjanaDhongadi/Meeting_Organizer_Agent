import json
import logging
from typing import List, Dict, Any, Optional
from backend.app.db import SessionLocal, Employee, Meeting, compute_simple_embedding, cosine_similarity

logger = logging.getLogger("long_term_memory")

class LongTermMemory:
    """
    Lab 4: Long-Term Memory & RAG Retrieval
    Stores and retrieves persistent knowledge across sessions:
    - Employee directory and role profiles
    - Stored meeting preferences and constraints
    - Historical meetings and previous action items
    Uses pgvector / vector embeddings for semantic retrieval.
    NOTE: Never determines calendar availability using RAG.
    """

    def search_context(self, query: str, top_k: int = 3) -> Dict[str, Any]:
        """
        RAG semantic search across employee preferences, roles, and past meetings.
        """
        db = SessionLocal()
        try:
            q_vec = compute_simple_embedding(query)
            
            # 1. Search employees
            employees = db.query(Employee).all()
            scored_employees = []
            for emp in employees:
                score = 0.0
                if emp.embedding and emp.embedding != "[]":
                    try:
                        emb = json.loads(emp.embedding)
                        score = cosine_similarity(q_vec, emb)
                    except Exception:
                        score = 0.0
                
                # Hybrid lexical boost: ensure explicit query terms (name, role, dept) are boosted
                q_words = [w.lower() for w in query.split() if len(w) > 2]
                emp_text = f"{emp.name} {emp.designation} {emp.department} {emp.other_info}".lower()
                for w in q_words:
                    if w in emp_text:
                        score += 0.35

                if score > 0.2:
                    scored_employees.append((score, emp))

            scored_employees.sort(key=lambda x: x[0], reverse=True)
            matched_employees = [
                {
                    "similarity": round(s[0], 3),
                    "profile": s[1].to_dict()
                }
                for s in scored_employees[:top_k] if s[0] > 0.25
            ]

            # 2. Search past meetings
            past_meetings = db.query(Meeting).filter(Meeting.status == "COMPLETED").all()
            matched_meetings = []
            for m in past_meetings:
                m_text = f"{m.title} {m.purpose} {m.agenda}"
                m_vec = compute_simple_embedding(m_text)
                score = cosine_similarity(q_vec, m_vec)
                if score > 0.35:
                    matched_meetings.append({
                        "similarity": round(score, 3),
                        "meeting_id": m.id,
                        "title": m.title,
                        "purpose": m.purpose,
                        "agenda": m.agenda
                    })
            matched_meetings.sort(key=lambda x: x["similarity"], reverse=True)

            return {
                "query": query,
                "relevant_employees": matched_employees,
                "historical_meetings": matched_meetings[:top_k]
            }
        finally:
            db.close()

    def get_employee_preferences(self, email: str) -> Optional[Dict[str, Any]]:
        db = SessionLocal()
        try:
            emp = db.query(Employee).filter(Employee.email.ilike(email)).first()
            if emp:
                return {
                    "name": emp.name,
                    "email": emp.email,
                    "preferences": emp.meeting_preferences,
                    "working_days": emp.working_days,
                    "working_hours": f"{emp.working_hours_start} - {emp.working_hours_end}",
                    "timezone": emp.timezone,
                    "preferred_duration": emp.preferred_duration,
                    "mode_preference": emp.mode_preference
                }
            return None
        finally:
            db.close()

    def record_completed_meeting(self, meeting_id: str, title: str, purpose: str, agenda: str):
        logger.info(f"Indexing completed meeting {meeting_id} into long-term memory")
        # Handled through database persistence

long_term_memory = LongTermMemory()
