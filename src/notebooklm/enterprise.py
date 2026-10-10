"""Public surface for Gemini Enterprise notebooks.

Enterprise tenants are served by Google's documented Discovery Engine
``v1alpha`` API, which needs only Application Default Credentials -- no
browser, no cookies, and none of the obfuscated RPC IDs the batchexecute
transport depends on. Notebook and source management therefore go through this
module rather than through :class:`~notebooklm.client.NotebookLMClient`.

Chat is the one thing not here. No API version exposes a notebook-scoped answer
method, so asking questions against an enterprise notebook still needs the
cookie-backed transport that the rest of the package implements.

Note: this is the *library* surface. The CLI command group lives at
``notebooklm.cli.enterprise_cmd`` and the workflow layer at
``notebooklm._app.enterprise`` -- different files, different concerns.
"""

from __future__ import annotations

from ._enterprise.api import (
    EnterpriseAPI,
    EnterpriseNotebook,
    EnterpriseSource,
    drive_content,
    text_content,
    web_content,
    youtube_content,
)
from ._enterprise.config import (
    DEFAULT_LOCATION,
    get_api_host,
    get_location,
    get_project,
)
from ._enterprise.credentials import EnterpriseCredentials

__all__ = [
    "DEFAULT_LOCATION",
    "EnterpriseAPI",
    "EnterpriseCredentials",
    "EnterpriseNotebook",
    "EnterpriseSource",
    "drive_content",
    "get_api_host",
    "get_location",
    "get_project",
    "text_content",
    "web_content",
    "youtube_content",
]
