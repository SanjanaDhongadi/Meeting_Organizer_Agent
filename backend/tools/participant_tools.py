from typing import List, Optional
from pydantic import BaseModel, Field
from backend.tools.base import BaseTool, ToolResult
from backend.app.db import SessionLocal, Employee, compute_simple_embedding, cosine_similarity
import json
import logging

logger = logging.getLogger("participant_tools")

# 1. find_participant
class FindParticipantInput(BaseModel):
    query: str = Field(..., description="Name, partial name, or email of the participant to find")

class FindParticipantTool(BaseTool):
    name: str = "find_participant"
    description: str = "Search for an employee participant by name or email. Returns matches, or flags UNKNOWN or AMBIGUOUS."
    args_schema = FindParticipantInput

    def _run(self, args: FindParticipantInput) -> ToolResult:
        query_text = args.query.strip().lower()
        if not query_text:
            return ToolResult(
                success=False,
                error="Search query cannot be empty",
                tool_name=self.name
            )

        db = SessionLocal()
        try:
            # Email queries resolve only on an exact, canonical email address.
            if "@" in query_text:
                emp_by_email = db.query(Employee).filter(Employee.email.ilike(query_text)).all()
            else:
                emp_by_email = []
            if emp_by_email:
                return ToolResult(
                    success=True,
                    data={
                        "match_type": "EXACT_EMAIL" if len(emp_by_email) == 1 else "AMBIGUOUS",
                        "count": len(emp_by_email),
                        "participants": [e.to_dict() for e in emp_by_email]
                    },
                    tool_name=self.name
                )
            if "@" in query_text:
                return ToolResult(
                    success=True,
                    data={
                        "match_type": "UNKNOWN",
                        "count": 0,
                        "query": args.query,
                        "message": "Participant not found. Please register this person or provide their email address.",
                        "participants": [],
                    },
                    tool_name=self.name,
                )

            # Check name matches
            emp_by_name = db.query(Employee).filter(Employee.name.ilike(f"%{query_text}%")).all()
            if len(emp_by_name) == 1:
                return ToolResult(
                    success=True,
                    data={
                        "match_type": "EXACT_MATCH",
                        "count": 1,
                        "participants": [emp_by_name[0].to_dict()]
                    },
                    tool_name=self.name
                )
            elif len(emp_by_name) > 1:
                # Multiple people with same partial name (e.g., 'David') -> AMBIGUOUS
                return ToolResult(
                    success=True,
                    data={
                        "match_type": "AMBIGUOUS",
                        "count": len(emp_by_name),
                        "message": f"Multiple employees matched '{args.query}'. Please select the specific person.",
                        "participants": [e.to_dict() for e in emp_by_name]
                    },
                    tool_name=self.name
                )

            # Try semantic search via vector embeddings for role/preference queries
            all_emps = db.query(Employee).all()
            q_vec = compute_simple_embedding(query_text)
            scored = []
            for e in all_emps:
                if e.embedding and e.embedding != "[]":
                    try:
                        e_vec = json.loads(e.embedding)
                        sim = cosine_similarity(q_vec, e_vec)
                        if sim > 0.4:
                            scored.append((sim, e))
                    except Exception:
                        pass
            
            scored.sort(key=lambda x: x[0], reverse=True)
            if scored:
                return ToolResult(
                    success=True,
                    data={
                        "match_type": "SEMANTIC_SIMILARITY",
                        "count": len(scored),
                        "participants": [s[1].to_dict() for s in scored[:2]]
                    },
                    tool_name=self.name
                )

            # Not found in database -> UNKNOWN
            return ToolResult(
                success=True,
                data={
                    "match_type": "UNKNOWN",
                    "count": 0,
                    "query": args.query,
                    "message": f"Participant '{args.query}' is not in the employee directory. Availability cannot be verified.",
                    "participants": []
                },
                tool_name=self.name
            )
        finally:
            db.close()

# 2. retrieve_participant_profile
class RetrieveParticipantProfileInput(BaseModel):
    identifier: str = Field(..., description="Employee ID or exact email address")

class RetrieveParticipantProfileTool(BaseTool):
    name: str = "retrieve_participant_profile"
    description: str = "Retrieve full profile, working hours, and meeting preferences for a known participant."
    args_schema = RetrieveParticipantProfileInput

    def _run(self, args: RetrieveParticipantProfileInput) -> ToolResult:
        ident = args.identifier.strip()
        db = SessionLocal()
        try:
            emp = db.query(Employee).filter(
                (Employee.employee_id == ident) | (Employee.email.ilike(ident))
            ).first()
            if not emp:
                return ToolResult(
                    success=False,
                    error=f"No profile found for identifier '{args.identifier}'. Will not fabricate.",
                    tool_name=self.name
                )
            return ToolResult(
                success=True,
                data=emp.to_dict(),
                tool_name=self.name
            )
        finally:
            db.close()
