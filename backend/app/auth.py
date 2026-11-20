import hashlib
import os
import hmac
import secrets
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db, User

try:
    from jose import JWTError, jwt
    _HAS_JOSE = True
except ImportError:
    _HAS_JOSE = False

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

# Security settings
SECRET_KEY = settings.SECRET_KEY or "meeting-organizer-secret-key-super-secure-2026"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days

_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="auth-jwt-token")
security_bearer = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    pw_hash = hashlib.pbkdf2_hmac(
        'sha256',
        password.encode('utf-8'),
        salt.encode('utf-8'),
        100000
    ).hex()
    return f"{salt}${pw_hash}"


def verify_password(plain_password: str, hashed_password: Optional[str]) -> bool:
    if not hashed_password or '$' not in hashed_password:
        return False
    try:
        salt, pw_hash = hashed_password.split('$', 1)
        test_hash = hashlib.pbkdf2_hmac(
            'sha256',
            plain_password.encode('utf-8'),
            salt.encode('utf-8'),
            100000
        ).hex()
        return hmac.compare_digest(pw_hash, test_hash)
    except Exception:
        return False


def create_access_token(data: Dict[str, Any], expires_delta: Optional[timedelta] = None) -> str:
    if _HAS_JOSE:
        try:
            to_encode = data.copy()
            expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
            to_encode.update({"exp": expire, "sub": str(data.get("sub", ""))})
            return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
        except Exception:
            pass
    payload = {"sub": str(data.get("sub", "")), "email": str(data.get("email", ""))}
    return _serializer.dumps(payload)


def decode_access_token(token: str) -> Optional[Dict[str, Any]]:
    if _HAS_JOSE:
        try:
            return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        except Exception:
            pass
    try:
        max_age = int(ACCESS_TOKEN_EXPIRE_MINUTES * 60)
        payload = _serializer.loads(token, max_age=max_age)
        if isinstance(payload, dict) and "sub" in payload:
            return payload
    except Exception:
        pass
    return None


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_bearer),
    db: Session = Depends(get_db)
) -> User:
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = credentials.credentials
    payload = decode_access_token(token)
    if not payload or "sub" not in payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        user_id = int(payload["sub"])
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def get_current_user_optional(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_bearer),
    db: Session = Depends(get_db)
) -> Optional[User]:
    if not credentials:
        return None
    token = credentials.credentials
    payload = decode_access_token(token)
    if not payload or "sub" not in payload:
        return None
    try:
        user_id = int(payload["sub"])
        return db.query(User).filter(User.id == user_id).first()
    except Exception:
        return None
