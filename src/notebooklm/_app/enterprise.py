"""Transport-neutral workflows for Gemini Enterprise notebooks (ADR-0008).

Holds the logic that would otherwise sit in the CLI handler: turning a mixed
bag of user-supplied sources into ``UserContent`` messages, and pairing a
create with the wait that makes its result meaningful. Adapters render; this
module decides.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..enterprise import (
    EnterpriseAPI,
    EnterpriseNotebook,
    EnterpriseSource,
    text_content,
    web_content,
    youtube_content,
)
from ..exceptions import ValidationError

# The API accepts text inline and has no binary upload method, so a file source
# is read and sent as text. The ceiling is a guard against sending something
# absurd, not a documented service limit.
MAX_INLINE_FILE_BYTES = 10 * 1024 * 1024
_FILE_ENCODING = "utf-8"


@dataclass(frozen=True, slots=True)
class SourceRequest:
    """The sources one ``add`` invocation should create."""

    texts: tuple[str, ...] = ()
    files: tuple[Path, ...] = ()
    urls: tuple[str, ...] = ()
    youtube_urls: tuple[str, ...] = ()
    titles: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        """True when nothing was requested."""
        return not (self.texts or self.files or self.urls or self.youtube_urls)


def build_user_contents(request: SourceRequest) -> list[dict[str, Any]]:
    """Turn a :class:`SourceRequest` into ``UserContent`` messages.

    ``titles`` are consumed in order by the text and file sources, which are
    the only kinds whose display name the caller chooses -- a web page and a
    YouTube video are named by the backend from the fetched document.
    """
    if request.is_empty():
        raise ValidationError("Nothing to add: pass text, file, url, or youtube sources")

    contents: list[dict[str, Any]] = []
    remaining = list(request.titles)

    def next_title(default: str) -> str:
        return remaining.pop(0) if remaining else default

    for index, text in enumerate(request.texts, start=1):
        if not text.strip():
            raise ValidationError("Text source is empty")
        contents.append(text_content(text, next_title(f"text-{index}")))

    for path in request.files:
        size = path.stat().st_size
        if size > MAX_INLINE_FILE_BYTES:
            raise ValidationError(
                f"{path} is {size} bytes; the API takes text inline only, "
                f"limit {MAX_INLINE_FILE_BYTES} bytes"
            )
        # errors="replace": one bad byte in a log file should not abort an
        # upload the user asked for.
        body = path.read_text(encoding=_FILE_ENCODING, errors="replace")
        if not body.strip():
            raise ValidationError(f"{path} is empty")
        contents.append(text_content(body, next_title(path.name)))

    contents.extend(web_content(url) for url in request.urls)
    contents.extend(youtube_content(url) for url in request.youtube_urls)
    return contents


async def execute_enterprise_whoami() -> dict[str, Any]:
    """Report the identity and tenant the enterprise commands will act on."""
    async with EnterpriseAPI() as api:
        return {
            "account": await api.whoami(),
            "project": api.project,
            "location": api.location,
        }


async def execute_enterprise_list(limit: int) -> list[EnterpriseNotebook]:
    """List recently viewed notebooks."""
    async with EnterpriseAPI() as api:
        return await api.list_notebooks(page_size=limit)


async def execute_enterprise_create(title: str) -> EnterpriseNotebook:
    """Create an empty notebook."""
    async with EnterpriseAPI() as api:
        return await api.create_notebook(title)


async def execute_enterprise_show(notebook_id: str) -> EnterpriseNotebook:
    """Fetch a notebook with its sources and their ingest status."""
    async with EnterpriseAPI() as api:
        return await api.get_notebook(notebook_id)


async def execute_enterprise_add_sources(
    notebook_id: str,
    request: SourceRequest,
    *,
    wait: bool = True,
    timeout: float = 300.0,
) -> list[EnterpriseSource]:
    """Attach sources to a notebook, optionally waiting for ingestion.

    Ingestion is per-source and independent, so this never raises on a partial
    failure: a batch where one URL was unreachable and the rest succeeded is
    reported through each source's status for the caller to judge.
    """
    contents = build_user_contents(request)
    async with EnterpriseAPI() as api:
        created = await api.add_sources(notebook_id, contents)
        if not wait:
            return created
        return await api.wait_for_sources(notebook_id, timeout=timeout)


async def execute_enterprise_delete(notebook_ids: list[str]) -> None:
    """Delete notebooks. Irreversible."""
    async with EnterpriseAPI() as api:
        await api.delete_notebooks(notebook_ids)
