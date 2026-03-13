"""OAuth provider configuration for HelmLog (#268).

Uses Authlib to configure OAuth/OIDC providers (Apple, Google, GitHub).
Only providers with configured env vars are enabled.
"""

from __future__ import annotations

import os
import time
from typing import Any

from authlib.integrations.starlette_client import OAuth
from loguru import logger

oauth = OAuth()


def _configure_providers() -> None:
    """Register OAuth providers that have env vars configured."""
    # Google
    if os.getenv("OAUTH_GOOGLE_CLIENT_ID"):
        oauth.register(
            name="google",
            client_id=os.environ["OAUTH_GOOGLE_CLIENT_ID"],
            client_secret=os.environ.get("OAUTH_GOOGLE_CLIENT_SECRET", ""),
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )
        logger.info("OAuth provider registered: google")

    # Apple
    if os.getenv("OAUTH_APPLE_CLIENT_ID"):
        oauth.register(
            name="apple",
            client_id=os.environ["OAUTH_APPLE_CLIENT_ID"],
            client_secret=_apple_client_secret(),
            authorize_url="https://appleid.apple.com/auth/authorize",
            access_token_url="https://appleid.apple.com/auth/token",
            client_kwargs={"scope": "name email", "response_mode": "form_post"},
        )
        logger.info("OAuth provider registered: apple")

    # GitHub
    if os.getenv("OAUTH_GITHUB_CLIENT_ID"):
        oauth.register(
            name="github",
            client_id=os.environ["OAUTH_GITHUB_CLIENT_ID"],
            client_secret=os.environ.get("OAUTH_GITHUB_CLIENT_SECRET", ""),
            authorize_url="https://github.com/login/oauth/authorize",
            access_token_url="https://github.com/login/oauth/access_token",
            api_base_url="https://api.github.com/",
            client_kwargs={"scope": "user:email"},
        )
        logger.info("OAuth provider registered: github")


def _apple_client_secret() -> str:
    """Generate JWT client secret for Apple Sign In."""
    key_path = os.environ.get("OAUTH_APPLE_PRIVATE_KEY_PATH", "")
    if not key_path or not os.path.exists(key_path):
        return ""
    with open(key_path) as f:
        private_key = f.read()
    now = int(time.time())
    headers = {"kid": os.environ.get("OAUTH_APPLE_KEY_ID", ""), "alg": "ES256"}
    payload = {
        "iss": os.environ.get("OAUTH_APPLE_TEAM_ID", ""),
        "iat": now,
        "exp": now + 86400 * 180,
        "aud": "https://appleid.apple.com",
        "sub": os.environ["OAUTH_APPLE_CLIENT_ID"],
    }
    from authlib.jose import jwt as authlib_jwt

    token: str = authlib_jwt.encode(headers, payload, private_key).decode("utf-8")
    return token


def enabled_providers() -> list[str]:
    """Return list of enabled OAuth provider names."""
    providers: list[str] = []
    if os.getenv("OAUTH_GOOGLE_CLIENT_ID"):
        providers.append("google")
    if os.getenv("OAUTH_APPLE_CLIENT_ID"):
        providers.append("apple")
    if os.getenv("OAUTH_GITHUB_CLIENT_ID"):
        providers.append("github")
    return providers


def init_oauth(app: Any) -> None:  # noqa: ANN401
    """Initialize OAuth with the FastAPI/Starlette app."""
    _configure_providers()
    oauth.init_app(app)
