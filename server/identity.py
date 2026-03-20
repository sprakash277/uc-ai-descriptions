"""Per-request user identity extraction for Databricks Apps.

Databricks Apps injects the authenticated user's identity as plain HTTP
headers — no JWT decoding required:
  x-forwarded-email             — user's email (preferred)
  x-forwarded-preferred-username — same, always present alongside email
  x-forwarded-user              — numeric "user_id@org_id" (fallback)
"""

import logging
import os

from fastapi import Request

logger = logging.getLogger(__name__)

_LOCAL_USER = os.environ.get("DEV_USER_EMAIL", "dev_user")


def get_current_user(request: Request) -> str:
    """FastAPI dependency — returns the calling user's email/username.

    Reads Databricks Apps identity headers in priority order:
      1. x-forwarded-email
      2. x-forwarded-preferred-username
      3. x-forwarded-user (numeric id@org_id)
      4. DEV_USER_EMAIL env var / 'dev_user' (local development fallback)
    """
    user = (
        request.headers.get("x-forwarded-email")
        or request.headers.get("x-forwarded-preferred-username")
        or request.headers.get("x-forwarded-user")
        or _LOCAL_USER
    )
    return user
