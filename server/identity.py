"""Per-request user identity extraction for Databricks Apps."""

import base64
import json
import logging
import os

from fastapi import Request

logger = logging.getLogger(__name__)

_LOCAL_USER = os.environ.get("DEV_USER_EMAIL", "dev_user")


def _decode_jwt_payload(token: str) -> dict:
    """Decode JWT payload without verifying signature.

    The token is injected by the Databricks Apps runtime and is already
    trusted — we only need the payload to read the 'sub' / 'email' claim.
    """
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        return json.loads(payload_bytes)
    except Exception as e:
        logger.warning("JWT decode failed: %s", e)
        return {}


def get_current_user(request: Request) -> str:
    """FastAPI dependency — returns the calling user's email/username.

    Reads X-Forwarded-Access-Token (injected by Databricks Apps runtime).
    Falls back to DEV_USER_EMAIL env var or 'dev_user' when running locally.
    """
    token = request.headers.get("X-Forwarded-Access-Token", "")
    if not token:
        return _LOCAL_USER

    payload = _decode_jwt_payload(token)
    user = payload.get("email") or payload.get("sub") or _LOCAL_USER
    return str(user)


def get_forwarded_token(request: Request) -> str | None:
    """Return the raw forwarded OAuth token, or None if absent."""
    return request.headers.get("X-Forwarded-Access-Token") or None
