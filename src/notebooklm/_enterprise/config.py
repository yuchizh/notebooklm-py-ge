"""Resolution of the Gemini Enterprise project, location, and API endpoint.

Gemini Enterprise exposes notebooks through the *official* Discovery Engine
``v1alpha`` API in addition to the batchexecute transport the rest of this
package speaks. The official API is a documented contract with stable method
names, so the notebook and source surfaces are driven through it rather than
through obfuscated RPC IDs that Google rotates.

Chat is the exception: ``v1alpha`` has no notebook-scoped answer method (every
``:answer`` / ``:converse`` hangs off ``dataStores`` or ``engines``), and the
same holds for ``v1`` and ``v1beta``, which expose no notebook methods at all.
"""

from __future__ import annotations

from typing import Final

from .._env import ENTERPRISE_REGIONS, policy_env

# Discovery Engine rejects a regional resource addressed through the global
# endpoint with an explicit "Incorrect API endpoint used" 400, so the location
# in the resource path and the hostname have to be chosen together.
_GLOBAL_LOCATION: Final[str] = "global"
_API_HOST_TEMPLATE: Final[str] = "{prefix}discoveryengine.googleapis.com"
_API_VERSION: Final[str] = "v1alpha"

DEFAULT_LOCATION: Final[str] = "us"


def get_location() -> str:
    """Return the configured Gemini Enterprise location.

    Shares the region vocabulary with ``NOTEBOOKLM_BASE_URL``'s required region
    path segment so a tenant is described one way, not two.
    """
    raw = (policy_env("NOTEBOOKLM_ENTERPRISE_LOCATION") or "").strip().lower()
    if not raw:
        return DEFAULT_LOCATION
    if raw not in ENTERPRISE_REGIONS:
        allowed = "|".join(sorted(ENTERPRISE_REGIONS))
        raise ValueError(f"NOTEBOOKLM_ENTERPRISE_LOCATION must be one of: {allowed}")
    return raw


def get_api_host(location: str | None = None) -> str:
    """Return the Discovery Engine host serving ``location``."""
    resolved = location or get_location()
    prefix = "" if resolved == _GLOBAL_LOCATION else f"{resolved}-"
    return _API_HOST_TEMPLATE.format(prefix=prefix)


def get_project() -> str:
    """Return the project owning the enterprise notebooks.

    Accepts a project ID or a project number -- the Discovery Engine resource
    path takes either. ``NOTEBOOKLM_PROJECT_NUMBER`` is *not* consulted as a
    fallback: it is numbers-only because it is interpolated into the browser
    app-shell URL, and requiring callers to duplicate it here would be worse
    than letting the ADC file's own quota project answer.
    """
    raw = (policy_env("NOTEBOOKLM_ENTERPRISE_PROJECT") or "").strip()
    if raw:
        return raw
    from .credentials import adc_quota_project

    project = adc_quota_project()
    if project:
        return project
    raise ValueError(
        "No enterprise project configured. Set NOTEBOOKLM_ENTERPRISE_PROJECT, "
        "or run: gcloud auth application-default set-quota-project <PROJECT>"
    )


def parent_path(project: str | None = None, location: str | None = None) -> str:
    """Return the ``projects/.../locations/...`` parent for notebook calls."""
    return f"projects/{project or get_project()}/locations/{location or get_location()}"


def api_base(location: str | None = None) -> str:
    """Return the versioned API root URL for ``location``."""
    return f"https://{get_api_host(location)}/{_API_VERSION}"
