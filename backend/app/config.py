import os
from pydantic_settings import BaseSettings
from typing import List, Optional

DEFAULT_GOOGLE_SCOPES = (
    "openid "
    "https://www.googleapis.com/auth/userinfo.email "
    "https://www.googleapis.com/auth/calendar "
    "https://www.googleapis.com/auth/gmail.send "
    "https://www.googleapis.com/auth/gmail.readonly"
)

class Settings(BaseSettings):
    # DEMO_MODE=true swaps the Google adapters for offline mock adapters (tests / offline dev only).
    # The live application uses the real Google integrations (DEMO_MODE=false).
    DEMO_MODE: bool = False
    OPENAI_API_KEY: Optional[str] = None
    OPENAI_MODEL: str = "gpt-4o-mini"
    # Wall-clock time zone used to interpret times typed in meeting requests ("10 AM"); stored times are UTC.
    MEETING_TIMEZONE: str = "UTC"

    # Database
    DATABASE_URL: str = "sqlite:///./meeting_organizer.db"

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
    AUDITORIUM_BOOKING_EMAIL: str = "auditorium.booking@company.com"
    # Optional: the connected employee whose Google account organizes events and sends mail.
    GOOGLE_CALENDAR_OWNER_EMAIL: Optional[str] = None

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
