import os
from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    DEMO_MODE: bool = True
    OPENAI_API_KEY: Optional[str] = None
    OPENAI_MODEL: str = "gpt-4o-mini"
    
    # Database
    DATABASE_URL: str = "sqlite:///./meeting_organizer.db"
    
    # Google OAuth credentials
    GOOGLE_CLIENT_ID: Optional[str] = None
    GOOGLE_CLIENT_SECRET: Optional[str] = None
    GOOGLE_REDIRECT_URI: str = "http://localhost:8000/api/auth/google/callback"
    GOOGLE_CLOUD_PROJECT_ID: Optional[str] = None
    AUDITORIUM_BOOKING_EMAIL: str = "auditorium.booking@company.com"
    GOOGLE_CALENDAR_OWNER_EMAIL: Optional[str] = None
    
    SECRET_KEY: Optional[str] = None
    BACKEND_HOST: str = "0.0.0.0"
    BACKEND_PORT: int = 8000
    FRONTEND_EMPLOYEE_PORT: int = 5173
    FRONTEND_MEETING_PORT: int = 5174
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"

settings = Settings()
