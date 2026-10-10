"""Access tokens for the official Gemini Enterprise API, from ADC.

Only ``authorized_user`` credentials work. Gemini Enterprise's notebook methods
resolve the *end-user* identity that holds the seat license and reject service
accounts with ``401 Caller identity could not be resolved`` -- this was verified
against a service account whose identity was resolvable and whose seat was
assigned, including one impersonated through IAM Credentials with a
``userinfo.email`` scope. So this module deliberately does not fall back to the
GCE metadata server or to service-account key files: both would produce a token
that is guaranteed to be refused, and a confusing 401 is worse than a clear
error up front.

``google-auth`` is not a dependency of this package, and an ``authorized_user``
credential is just an OAuth refresh token, so the exchange is done directly.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Final

import httpx

from ..exceptions import AuthError

_TOKEN_URI: Final[str] = "https://oauth2.googleapis.com/token"
_USERINFO_URI: Final[str] = "https://openidconnect.googleapis.com/v1/userinfo"
# Refresh a little early: a token that expires mid-flight surfaces as an opaque
# 401 from an unrelated call.
_EXPIRY_SKEW_SECONDS: Final[int] = 60
_AUTHORIZED_USER: Final[str] = "authorized_user"

_SETUP_HINT: Final[str] = (
    "Run: gcloud auth application-default login "
    "--scopes=https://www.googleapis.com/auth/cloud-platform,"
    "https://www.googleapis.com/auth/userinfo.email"
)


def adc_path() -> Path:
    """Return the path ADC is read from, honoring the standard override."""
    override = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if override:
        return Path(override)
    config_root = os.environ.get("CLOUDSDK_CONFIG")
    base = Path(config_root) if config_root else Path.home() / ".config" / "gcloud"
    return base / "application_default_credentials.json"


def load_adc() -> dict[str, Any]:
    """Load and validate the ADC file."""
    path = adc_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise AuthError(f"No application default credentials at {path}. {_SETUP_HINT}") from None
    except OSError as exc:
        raise AuthError(f"Could not read {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AuthError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AuthError(f"{path} does not contain a credential object")

    kind = data.get("type")
    if kind != _AUTHORIZED_USER:
        raise AuthError(
            f"{path} holds {kind!r} credentials, but Gemini Enterprise notebooks "
            f"require an end-user identity -- service accounts are rejected with "
            f"'Caller identity could not be resolved'. {_SETUP_HINT}"
        )
    missing = [k for k in ("client_id", "client_secret", "refresh_token") if not data.get(k)]
    if missing:
        raise AuthError(f"{path} is missing {', '.join(missing)}. {_SETUP_HINT}")
    return data


def adc_quota_project() -> str | None:
    """Return the ADC quota project, or ``None`` when ADC is unusable.

    Used as a last-resort default for the enterprise project, so a missing or
    malformed credential file is not an error here -- the caller reports the
    absence of a project, which is the more actionable message.
    """
    try:
        return load_adc().get("quota_project_id") or None
    except AuthError:
        return None


class EnterpriseCredentials:
    """Mints and caches access tokens from an ADC ``authorized_user`` file."""

    def __init__(self, info: dict[str, Any] | None = None) -> None:
        self._info = info if info is not None else load_adc()
        self._token: str | None = None
        self._expires_at: float = 0.0

    @property
    def account(self) -> str | None:
        """The signed-in account, when the credential file records one.

        ``gcloud`` often writes this key empty, so a ``None`` here means
        unknown, not anonymous. Use :meth:`resolve_account` to ask Google.
        """
        value = self._info.get("account")
        return value if isinstance(value, str) and value else None

    async def resolve_account(self, client: httpx.AsyncClient) -> str | None:
        """Return the email this credential acts as, asking Google if needed.

        Which identity is in play decides whether Gemini Enterprise answers at
        all -- the seat license is attached to a person -- so it is worth a call
        when the credential file does not say. Requires the ``userinfo.email``
        scope; returns ``None`` rather than raising when it was not granted.
        """
        cached = self.account
        if cached:
            return cached
        try:
            token = await self.token(client)
            response = await client.get(_USERINFO_URI, headers={"Authorization": f"Bearer {token}"})
            if response.status_code != 200:
                return None
            email = response.json().get("email")
        except (httpx.HTTPError, ValueError, AuthError):
            return None
        if isinstance(email, str) and email:
            self._info["account"] = email
            return email
        return None

    @property
    def quota_project(self) -> str | None:
        """The quota project to bill and authorize the call against."""
        value = self._info.get("quota_project_id")
        return value if isinstance(value, str) and value else None

    async def token(self, client: httpx.AsyncClient) -> str:
        """Return a valid access token, refreshing it when near expiry."""
        if self._token is not None and time.monotonic() < self._expires_at:
            return self._token
        try:
            response = await client.post(
                _TOKEN_URI,
                data={
                    "grant_type": "refresh_token",
                    "client_id": self._info["client_id"],
                    "client_secret": self._info["client_secret"],
                    "refresh_token": self._info["refresh_token"],
                },
            )
        except httpx.HTTPError as exc:
            raise AuthError(f"Token refresh failed: {exc}") from exc
        if response.status_code != 200:
            # The body carries Google's own error/error_description, but it can
            # echo request material, so only the machine-readable code is shown.
            code = ""
            try:
                code = str(json.loads(response.text).get("error", ""))
            except (ValueError, AttributeError):
                pass
            detail = f" ({code})" if code else ""
            raise AuthError(
                f"Token refresh rejected with HTTP {response.status_code}{detail}. {_SETUP_HINT}"
            )
        payload = response.json()
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise AuthError("Token endpoint returned no access_token")
        lifetime = int(payload.get("expires_in", 3600))
        self._token = token
        self._expires_at = time.monotonic() + max(lifetime - _EXPIRY_SKEW_SECONDS, 0)
        return token
