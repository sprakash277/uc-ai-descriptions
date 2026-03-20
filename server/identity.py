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


def get_user_token(request: Request) -> str | None:
    """Return the user's OAuth token forwarded by the Databricks Apps proxy.

    Only present when On-Behalf-Of (OBO) authorization is enabled in the
    Databricks Apps UI and user_api_scopes are declared in databricks.yml.
    Returns None in local dev or when OBO is not yet configured.
    """
    return request.headers.get("x-forwarded-access-token") or None


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
