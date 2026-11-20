import os
from pydantic_settings import BaseSettings
from typing import List, Optional

DEFAULT_GOOGLE_SCOPES = (
    "openid "
    "https://www.googleapis.com/auth/userinfo.email "
    "https://www.googleapis.com/auth/calendar "
    "https://www.googleapis.com/auth/gmail.send "
    "https://www.googleapis.com/auth/gmail.readonly "
    # Google Meet transcripts (post-meeting minutes). Accounts connected before this scope existed must reconnect.
    "https://www.googleapis.com/auth/meetings.space.readonly"
)

class Settings(BaseSettings):
    # DEMO_MODE=true swaps the Google adapters for offline mock adapters (tests / offline dev only).
    # The live application uses the real Google integrations (DEMO_MODE=false).
    DEMO_MODE: bool = False
    # AI provider: Groq (LLM and speech-to-text). The key is read from .env only; it is never logged or returned.
    AI_PROVIDER: str = "groq"
    GROQ_API_KEY: Optional[str] = None
    GROQ_BASE_URL: str = "https://api.groq.com/openai/v1"
    GROQ_LLM_MODEL: str = "openai/gpt-oss-120b"
    GROQ_TRANSCRIPTION_MODEL: str = "whisper-large-v3"
    # Wall-clock time zone used to interpret times typed in meeting requests ("10 AM"); stored times are UTC.
    MEETING_TIMEZONE: str = "UTC"

    # Database (PostgreSQL with psycopg driver)
    DATABASE_URL: str = "postgresql+psycopg://postgres:1234@localhost:5432/meeting_organizer"

    # Google OAuth credentials (development/test credentials today, organization credentials later —
    # only these values change, never the agent/workflow code).
    GOOGLE_CLIENT_ID: Optional[str] = None
    GOOGLE_CLIENT_SECRET: Optional[str] = None
    GOOGLE_REDIRECT_URI: str = "http://localhost:8000/api/auth/google/callback"
    GOOGLE_CLOUD_PROJECT_ID: Optional[str] = None
    GOOGLE_OAUTH_SCOPES: str = DEFAULT_GOOGLE_SCOPES  # space- or comma-separated
    GOOGLE_AUTH_URI: str = "https://accounts.google.com/o/oauth2/auth"
    GOOGLE_TOKEN_URI: str = "https://oauth2.googleapis.com/token"
    GOOGLE_OAUTH_STATE_TTL_SECONDS: int = 600
    # Optional: restrict sign-in to one Google Workspace domain (e.g. "yourcompany.com").
    GOOGLE_HOSTED_DOMAIN: Optional[str] = None
    # Optional: the connected employee whose Google account organizes events and sends mail.
    GOOGLE_CALENDAR_OWNER_EMAIL: Optional[str] = None

    # Minutes of Meeting: how long after a meeting ends we keep retrying for its Meet transcript, and the shared
    # secret that a Pub/Sub push subscription (Google Workspace Events) must present to /api/meet/events.
    MINUTES_MAX_WAIT_HOURS: int = 48
    MEET_EVENTS_TOKEN: Optional[str] = None

    # Browser-recorded audio (alternative to Google's own Meet transcripts, e.g. personal Gmail accounts).
    # Raw audio lives on disk (never in PostgreSQL); only metadata and the transcript are stored in the database.
    RECORDINGS_DIR: str = "data/recordings"
    RECORDING_RETENTION_DAYS: int = 7              # raw audio is deleted this long after a successful transcription; 0 = at once
    RECORDING_MAX_SEGMENT_MB: int = 24             # Groq's upload limit is 25 MB per file (free tier)
    TRANSCRIPTION_MAX_ATTEMPTS: int = 5

    # Runtime governance (Lab 6): budgets for ONE governed run (a scheduling workflow run or a minutes generation).
    RUNTIME_MAX_STEPS: int = 50         # workflow nodes per run (also LangGraph's recursion limit)
    RUNTIME_MAX_LLM_CALLS: int = 20     # actual LLM requests per run, retries included
    RUNTIME_MAX_SECONDS: float = 300.0  # wall-clock seconds per run
    # An approved meeting stuck in EXECUTING for this long (a crashed run) is resumed by the worker.
    EXECUTION_STUCK_MINUTES: int = 10

    SECRET_KEY: Optional[str] = None
    BACKEND_HOST: str = "0.0.0.0"
    BACKEND_PORT: int = 8000
    # Single unified frontend (employee + organizer views).
    FRONTEND_URL: str = "http://localhost:5173"
    CORS_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173"
    # Kept for backwards compatibility with older .env files; FRONTEND_URL is used instead.
    FRONTEND_EMPLOYEE_PORT: int = 5173
    FRONTEND_MEETING_PORT: int = 5174

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"

    @property
    def google_scopes(self) -> List[str]:
        return [s for s in self.GOOGLE_OAUTH_SCOPES.replace(",", " ").split() if s]

    @property
    def cors_origins(self) -> List[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

settings = Settings()
