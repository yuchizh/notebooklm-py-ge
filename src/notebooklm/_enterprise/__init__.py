"""Gemini Enterprise support built on Google's official notebook API.

Enterprise tenants are reachable two ways, and this package deliberately splits
along the seam between them:

* **Notebooks and sources** go through the documented Discovery Engine
  ``v1alpha`` API with Application Default Credentials. No browser, no cookies,
  and no obfuscated RPC IDs -- this is a supported contract.
* **Chat** has no official counterpart. ``v1alpha`` exposes no notebook-scoped
  answer method, and ``v1``/``v1beta`` expose no notebook methods at all, so
  asking a question still requires the batchexecute transport in ``_web`` with
  browser session cookies.
"""

from __future__ import annotations

from .api import (
    EnterpriseAPI,
    EnterpriseNotebook,
    EnterpriseSource,
    drive_content,
    text_content,
    web_content,
    youtube_content,
)
from .config import api_base, get_location, get_project, parent_path
from .credentials import EnterpriseCredentials

__all__ = [
    "EnterpriseAPI",
    "EnterpriseCredentials",
    "EnterpriseNotebook",
    "EnterpriseSource",
    "api_base",
    "drive_content",
    "get_location",
    "get_project",
    "parent_path",
    "text_content",
    "web_content",
    "youtube_content",
]
