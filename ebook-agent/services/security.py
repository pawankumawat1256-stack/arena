"""Token hashing, HMAC signing, and request authentication. Every comparison is constant-time."""

from __future__ import annotations

import hashlib
import hmac
import secrets

from fastapi import Header, HTTPException, status

from config import settings


class SecurityConfigError(RuntimeError):
    """A secret needed for signing or verification is missing or too short."""


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _signing_key() -> bytes:
    key = settings.app_secret_key
    if len(key) < 32:
        raise SecurityConfigError("APP_SECRET_KEY must be set to at least 32 random characters")
    return key.encode("utf-8")


def sign(message: str) -> str:
    return hmac.new(_signing_key(), message.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_signature(message: str, signature: str | None) -> bool:
    try:
        expected = sign(message)
    except SecurityConfigError:
        return False
    return bool(signature) and hmac.compare_digest(expected, signature)


def unsubscribe_message(email: str) -> str:
    return f"unsubscribe:{email.strip().lower()}"


def unsubscribe_signature(email: str) -> str:
    return sign(unsubscribe_message(email))


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """FastAPI dependency for protected endpoints. Fails closed when API_KEY is not set."""
    if not settings.api_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "API_KEY is not configured on the server")
    if not x_api_key or not constant_time_equals(x_api_key, settings.api_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or invalid X-API-Key header")


def verify_gumroad_token(token: str | None) -> None:
    """Gumroad Pings are unsigned, so the webhook URL carries a secret token (?token=...)."""
    if not settings.gumroad_webhook_token:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "GUMROAD_WEBHOOK_TOKEN is not configured")
    if not token or not constant_time_equals(token, settings.gumroad_webhook_token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid webhook token")
