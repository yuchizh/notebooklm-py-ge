"""Official Discovery Engine ``v1alpha`` client for Gemini Enterprise notebooks.

Covers the notebook and source surfaces: create, list, get, delete, and the
five ``UserContent`` kinds the API accepts. Method names and request bodies
come from the published discovery document, so unlike the batchexecute path
nothing here depends on obfuscated RPC IDs that rotate without notice.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Final

import httpx

from ..exceptions import (
    AuthError,
    NetworkError,
    NotFoundError,
    RPCError,
    ServerError,
    ValidationError,
)
from .config import api_base, get_location, get_project, parent_path
from .credentials import EnterpriseCredentials

_TIMEOUT: Final[float] = 60.0
# The API caps a page at this; asking for more is a 400 rather than a clamp.
_MAX_PAGE_SIZE: Final[int] = 100


# ``FailureReason`` is a oneof whose *field name* carries the diagnosis; several
# arms are empty messages, so the name is often the entire signal.
_FAILURE_LABELS: Final[dict[str, str]] = {
    "sourceUnreachable": "source unreachable",
    "sourceEmpty": "source is empty",
    "sourceTooLong": "source exceeds the word limit",
    "sourceLimitExceeded": "notebook source limit exceeded",
    "policyCheckFailed": "blocked by policy",
    "domainBlocked": "domain is blocked",
    "mimeTypeBlocked": "MIME type is blocked",
    "paywallError": "source is paywalled",
    "uploadError": "upload failed",
    "ingestionError": "ingestion failed",
    "googleDriveError": "Google Drive error",
    "youtubeError": "YouTube error",
    "audioTranscriptionError": "audio transcription failed",
    "unknown": "unknown error",
}


def _describe_failure(reason: dict[str, Any]) -> str | None:
    """Summarise a ``FailureReason`` oneof as human-readable text."""
    for field_name, detail in reason.items():
        label = _FAILURE_LABELS.get(field_name, field_name)
        extra = ""
        if isinstance(detail, dict):
            parts = [f"{k}={v}" for k, v in detail.items() if not isinstance(v, dict | list)]
            extra = f" ({', '.join(parts)})" if parts else ""
        return f"{label}{extra}"
    return None


@dataclass(frozen=True, slots=True)
class EnterpriseSource:
    """A source attached to an enterprise notebook."""

    id: str
    title: str
    status: str
    word_count: int | None = None
    token_count: int | None = None
    failure: str | None = None

    @property
    def is_complete(self) -> bool:
        """True once the source has finished ingesting successfully."""
        return self.status == "SOURCE_STATUS_COMPLETE"

    @property
    def is_failed(self) -> bool:
        """True when ingestion failed; :attr:`failure` explains why."""
        return self.status == "SOURCE_STATUS_ERROR"

    @property
    def is_settled(self) -> bool:
        """True when the source will not change state without further action."""
        return self.is_complete or self.is_failed

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> EnterpriseSource:
        """Build from a ``Source`` message."""
        metadata = payload.get("metadata") or {}
        settings = payload.get("settings") or {}
        source_id = (payload.get("sourceId") or {}).get("id")
        if not source_id:
            source_id = str(payload.get("name", "")).rsplit("/", 1)[-1]
        return cls(
            id=source_id,
            title=payload.get("title") or "",
            status=settings.get("status") or "SOURCE_STATUS_UNSPECIFIED",
            word_count=metadata.get("wordCount"),
            token_count=metadata.get("tokenCount"),
            failure=_describe_failure(settings.get("failureReason") or {}),
        )


@dataclass(frozen=True, slots=True)
class EnterpriseNotebook:
    """An enterprise notebook as returned by the official API."""

    id: str
    title: str
    name: str
    emoji: str = ""
    create_time: str | None = None
    last_viewed: str | None = None
    is_shared: bool = False
    sources: tuple[EnterpriseSource, ...] = field(default_factory=tuple)

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> EnterpriseNotebook:
        """Build from a ``Notebook`` message."""
        metadata = payload.get("metadata") or {}
        name = payload.get("name") or ""
        return cls(
            id=payload.get("notebookId") or name.rsplit("/", 1)[-1],
            title=payload.get("title") or "",
            name=name,
            emoji=payload.get("emoji") or "",
            create_time=metadata.get("createTime"),
            last_viewed=metadata.get("lastViewed"),
            is_shared=bool(metadata.get("isShared")),
            sources=tuple(EnterpriseSource.from_api(s) for s in payload.get("sources") or []),
        )


def text_content(content: str, source_name: str) -> dict[str, Any]:
    """A ``UserContent`` carrying inline text."""
    return {"textContent": {"content": content, "sourceName": source_name}}


def web_content(url: str, source_name: str | None = None) -> dict[str, Any]:
    """A ``UserContent`` the backend fetches from ``url``.

    VPC Service Controls blocks this kind on perimeter-protected tenants; the
    resulting source lands with a failure status rather than raising here.
    """
    body: dict[str, Any] = {"url": url}
    if source_name:
        body["sourceName"] = source_name
    return {"webContent": body}


def youtube_content(url: str) -> dict[str, Any]:
    """A ``UserContent`` referencing a YouTube video."""
    return {"videoContent": {"youtubeUrl": url}}


def drive_content(
    document_id: str, mime_type: str, source_name: str | None = None
) -> dict[str, Any]:
    """A ``UserContent`` referencing a Google Drive document."""
    body: dict[str, Any] = {"documentId": document_id, "mimeType": mime_type}
    if source_name:
        body["sourceName"] = source_name
    return {"googleDriveContent": body}


class EnterpriseAPI:
    """Async client for the official Gemini Enterprise notebook API."""

    def __init__(
        self,
        *,
        project: str | None = None,
        location: str | None = None,
        credentials: EnterpriseCredentials | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._location = location or get_location()
        self._project = project or get_project()
        self._credentials = credentials or EnterpriseCredentials()
        self._client = client
        self._owns_client = client is None
        self._base = api_base(self._location)
        self._parent = parent_path(self._project, self._location)

    @property
    def project(self) -> str:
        """The project whose notebooks this client addresses."""
        return self._project

    @property
    def location(self) -> str:
        """The location whose notebooks this client addresses."""
        return self._location

    @property
    def account(self) -> str | None:
        """The end-user account the requests are made as, if already known."""
        return self._credentials.account

    async def whoami(self) -> str | None:
        """Return the end-user account, querying Google when ADC omits it."""
        if self._client is None:  # pragma: no cover - misuse guard
            raise RuntimeError("EnterpriseAPI must be used as an async context manager")
        return await self._credentials.resolve_account(self._client)

    async def __aenter__(self) -> EnterpriseAPI:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=_TIMEOUT)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Issue one authenticated call and decode the JSON body."""
        if self._client is None:  # pragma: no cover - misuse guard
            raise RuntimeError("EnterpriseAPI must be used as an async context manager")
        token = await self._credentials.token(self._client)
        headers = {"Authorization": f"Bearer {token}"}
        # Without a billing/quota project the API answers 403 for a caller whose
        # own project is not the resource's, even when the IAM grant is present.
        quota_project = self._credentials.quota_project or self._project
        headers["X-Goog-User-Project"] = quota_project
        try:
            response = await self._client.request(
                method, f"{self._base}/{path}", headers=headers, **kwargs
            )
        except httpx.HTTPError as exc:
            raise NetworkError(f"{method} {path} failed: {exc}") from exc
        if response.status_code >= 400:
            raise _error_for(response, method, path)
        if not response.content:
            return {}
        try:
            body = response.json()
        except ValueError as exc:
            raise ServerError(f"{method} {path} returned a non-JSON body") from exc
        return body if isinstance(body, dict) else {"response": body}

    async def list_notebooks(self, *, page_size: int = 50) -> list[EnterpriseNotebook]:
        """Return recently viewed notebooks.

        The API has no plain list-collection method, so recency is the only
        ordering available and notebooks never opened by this user may be
        absent even when readable.
        """
        if not 1 <= page_size <= _MAX_PAGE_SIZE:
            raise ValidationError(f"page_size must be between 1 and {_MAX_PAGE_SIZE}")
        body = await self._request(
            "GET",
            f"{self._parent}/notebooks:listRecentlyViewed",
            params={"pageSize": page_size},
        )
        return [EnterpriseNotebook.from_api(n) for n in body.get("notebooks") or []]

    async def create_notebook(self, title: str) -> EnterpriseNotebook:
        """Create an empty notebook."""
        if not title.strip():
            raise ValidationError("Notebook title must not be empty")
        body = await self._request("POST", f"{self._parent}/notebooks", json={"title": title})
        return EnterpriseNotebook.from_api(body)

    async def get_notebook(self, notebook_id: str) -> EnterpriseNotebook:
        """Fetch one notebook, including its sources and their ingest status."""
        body = await self._request("GET", f"{self._parent}/notebooks/{notebook_id}")
        return EnterpriseNotebook.from_api(body)

    async def delete_notebooks(self, notebook_ids: list[str]) -> None:
        """Delete notebooks by id. Irreversible."""
        if not notebook_ids:
            raise ValidationError("No notebook ids supplied")
        names = [f"{self._parent}/notebooks/{nb}" for nb in notebook_ids]
        await self._request("POST", f"{self._parent}/notebooks:batchDelete", json={"names": names})

    async def add_sources(
        self, notebook_id: str, user_contents: list[dict[str, Any]]
    ) -> list[EnterpriseSource]:
        """Attach sources to a notebook.

        Returns as soon as the sources are registered, and ingestion continues
        in the background. Two consequences worth knowing:

        * The response can list *fewer* sources than were requested -- observed
          with a ``webContent`` alongside a ``textContent``, where only the text
          source came back while both appeared in a subsequent read.
        * Statuses here are provisional.

        Follow with :meth:`wait_for_sources` for the settled picture.
        """
        if not user_contents:
            raise ValidationError("No sources supplied")
        body = await self._request(
            "POST",
            f"{self._parent}/notebooks/{notebook_id}/sources:batchCreate",
            json={"userContents": user_contents},
        )
        return [EnterpriseSource.from_api(s) for s in body.get("sources") or []]

    async def wait_for_sources(
        self,
        notebook_id: str,
        *,
        timeout: float = 300.0,
        poll_interval: float = 5.0,
    ) -> list[EnterpriseSource]:
        """Poll until every source has settled, or ``timeout`` elapses.

        Returns the sources as last seen. Failures are reported through each
        source's status rather than raised: a batch where one URL was
        unreachable and the rest ingested cleanly is a partial success, and the
        caller is better placed to decide whether that is fatal.
        """
        deadline = time.monotonic() + timeout
        sources = (await self.get_notebook(notebook_id)).sources
        while not all(s.is_settled for s in sources):
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(poll_interval)
            sources = (await self.get_notebook(notebook_id)).sources
        return list(sources)

    async def delete_sources(self, notebook_id: str, source_ids: list[str]) -> None:
        """Detach sources from a notebook. Irreversible."""
        if not source_ids:
            raise ValidationError("No source ids supplied")
        names = [f"{self._parent}/notebooks/{notebook_id}/sources/{s}" for s in source_ids]
        await self._request(
            "POST",
            f"{self._parent}/notebooks/{notebook_id}/sources:batchDelete",
            json={"names": names},
        )


def _error_for(response: httpx.Response, method: str, path: str) -> Exception:
    """Map an HTTP failure onto this package's exception hierarchy."""
    message = ""
    try:
        payload = json.loads(response.text)
        if isinstance(payload, dict):
            message = str((payload.get("error") or {}).get("message") or "")
    except ValueError:
        pass
    detail = f": {message}" if message else ""
    status = response.status_code
    context = f"{method} {path} -> HTTP {status}{detail}"
    if status in (401, 403):
        return AuthError(context)
    if status == 404:
        return NotFoundError(context)
    if status >= 500:
        return ServerError(context)
    if status == 400:
        return ValidationError(context)
    return RPCError(context)
