"""Gemini Enterprise CLI commands — thin Click-handler layer (ADR-0008).

Each command resolves CLI inputs, delegates to the transport-neutral
:mod:`notebooklm._app.enterprise` core, and renders the result.

These commands deliberately bypass the batchexecute transport the rest of the
CLI uses. Enterprise notebooks are reachable through Google's documented
Discovery Engine ``v1alpha`` API with Application Default Credentials, which
needs no browser, no cookies, and no obfuscated RPC IDs.

Chat is not here, and cannot be: no API version exposes a notebook-scoped
answer method. Asking questions against an enterprise notebook still requires
the cookie-backed transport, the same way it does on the personal hosts.

Commands:
    whoami    Show the identity, project, and location in effect
    list      List recently viewed notebooks
    create    Create an empty notebook
    show      Show a notebook and its sources
    add       Attach text, file, URL, or YouTube sources to a notebook
    delete    Delete notebooks
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, NoReturn

import click

from .._app.enterprise import (
    SourceRequest,
    execute_enterprise_add_sources,
    execute_enterprise_create,
    execute_enterprise_delete,
    execute_enterprise_list,
    execute_enterprise_show,
    execute_enterprise_whoami,
)
from ..enterprise import EnterpriseNotebook, EnterpriseSource
from ..exceptions import NotebookLMError
from .error_handler import output_error
from .options import json_option
from .rendering import cli_print, json_output_response


def _fail(message: str, code: str, json_output: bool) -> NoReturn:
    output_error(message, code, json_output=json_output, exit_code=1)
    raise AssertionError("unreachable")  # pragma: no cover


def _run(coro: Any, json_output: bool) -> Any:
    """Drive one async enterprise workflow, mapping failures to CLI errors."""
    try:
        return asyncio.run(coro)
    except NotebookLMError as exc:
        _fail(str(exc), type(exc).__name__, json_output)
    except ValueError as exc:
        # Misconfiguration (unknown region, no project) surfaces as ValueError
        # from the config resolvers rather than as a NotebookLMError.
        _fail(str(exc), "ConfigurationError", json_output)


def _source_dict(source: EnterpriseSource) -> dict[str, Any]:
    return {
        "id": source.id,
        "title": source.title,
        "status": source.status,
        "word_count": source.word_count,
        "token_count": source.token_count,
        "failure": source.failure,
    }


def _notebook_dict(notebook: EnterpriseNotebook) -> dict[str, Any]:
    return {
        "id": notebook.id,
        "title": notebook.title,
        "emoji": notebook.emoji,
        "create_time": notebook.create_time,
        "last_viewed": notebook.last_viewed,
        "is_shared": notebook.is_shared,
        "sources": [_source_dict(s) for s in notebook.sources],
    }


def _print_sources(sources: list[EnterpriseSource]) -> None:
    for source in sources:
        if source.is_complete:
            suffix = f" [dim]{source.word_count} words[/dim]" if source.word_count else ""
            cli_print(f"  [green]OK[/green]   {source.title}{suffix}")
        elif source.is_failed:
            cli_print(f"  [red]FAIL[/red] {source.title} [red]{source.failure or ''}[/red]")
        else:
            label = source.status.replace("SOURCE_STATUS_", "").lower()
            cli_print(f"  [yellow]..[/yellow]   {source.title} [dim]{label}[/dim]")


@click.group(name="ge")
def enterprise() -> None:
    """Manage Gemini Enterprise notebooks via Google's official API.

    Requires Application Default Credentials for the end user holding the
    Gemini Enterprise seat -- service accounts are refused by the service:

    \b
      gcloud auth application-default login \\
        --scopes=https://www.googleapis.com/auth/cloud-platform,\\
    https://www.googleapis.com/auth/userinfo.email

    Configure the tenant with NOTEBOOKLM_ENTERPRISE_PROJECT and
    NOTEBOOKLM_ENTERPRISE_LOCATION (default: us).
    """


@enterprise.command(name="whoami")
@json_option
def whoami_cmd(json_output: bool) -> None:
    """Show the identity, project, and location in effect."""
    info = _run(execute_enterprise_whoami(), json_output)
    if json_output:
        json_output_response(info)
        return
    cli_print(f"account:  {info['account'] or '[dim]unknown[/dim]'}")
    cli_print(f"project:  {info['project']}")
    cli_print(f"location: {info['location']}")


@enterprise.command(name="list")
@click.option("--limit", type=click.IntRange(1, 100), default=50, help="Maximum notebooks.")
@json_option
def list_cmd(limit: int, json_output: bool) -> None:
    """List recently viewed notebooks.

    The API offers no plain list method, so this is recency-ordered and omits
    notebooks this account has never opened.
    """
    notebooks = _run(execute_enterprise_list(limit), json_output)
    if json_output:
        json_output_response({"notebooks": [_notebook_dict(n) for n in notebooks]})
        return
    if not notebooks:
        cli_print("No notebooks found.")
        return
    for notebook in notebooks:
        title = notebook.title or "[dim](untitled)[/dim]"
        count = len(notebook.sources)
        plural = "" if count == 1 else "s"
        # no_wrap: ids are long enough that reflowing splits a row across lines
        # and makes the listing unreadable in a narrow terminal.
        cli_print(
            f"{notebook.id}  {title}  [dim]{count} source{plural}[/dim]",
            no_wrap=True,
            overflow="ellipsis",
        )


@enterprise.command(name="create")
@click.argument("title")
@json_option
def create_cmd(title: str, json_output: bool) -> None:
    """Create an empty notebook."""
    notebook = _run(execute_enterprise_create(title), json_output)
    if json_output:
        json_output_response(_notebook_dict(notebook))
        return
    cli_print(f"Created [bold]{notebook.title}[/bold]")
    cli_print(f"  id: {notebook.id}")


@enterprise.command(name="show")
@click.argument("notebook_id")
@json_option
def show_cmd(notebook_id: str, json_output: bool) -> None:
    """Show a notebook and the ingest status of its sources."""
    notebook = _run(execute_enterprise_show(notebook_id), json_output)
    if json_output:
        json_output_response(_notebook_dict(notebook))
        return
    cli_print(f"[bold]{notebook.title or '(untitled)'}[/bold]  [dim]{notebook.id}[/dim]")
    if notebook.create_time:
        cli_print(f"created: {notebook.create_time}")
    if not notebook.sources:
        cli_print("No sources.")
        return
    cli_print(f"sources ({len(notebook.sources)}):")
    _print_sources(list(notebook.sources))


@enterprise.command(name="add")
@click.argument("notebook_id")
@click.option("--text", "texts", multiple=True, help="Inline text to add as a source.")
@click.option(
    "--file",
    "files",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Text file to add as a source (sent inline; the API takes no binary upload).",
)
@click.option("--url", "urls", multiple=True, help="Web page for the backend to fetch.")
@click.option("--youtube", "youtube_urls", multiple=True, help="YouTube video URL.")
@click.option("--title", "titles", multiple=True, help="Title for the Nth --text/--file, in order.")
@click.option("--wait/--no-wait", default=True, help="Wait for ingestion to settle.")
@click.option("--timeout", type=float, default=300.0, help="Seconds to wait for ingestion.")
@json_option
def add_cmd(
    notebook_id: str,
    texts: tuple[str, ...],
    files: tuple[Path, ...],
    urls: tuple[str, ...],
    youtube_urls: tuple[str, ...],
    titles: tuple[str, ...],
    wait: bool,
    timeout: float,
    json_output: bool,
) -> None:
    """Attach sources to a notebook.

    Sources ingest asynchronously and independently: one unreachable URL does
    not fail the others, so this reports per-source status and exits non-zero
    only when every source failed.
    """
    request = SourceRequest(
        texts=texts, files=files, urls=urls, youtube_urls=youtube_urls, titles=titles
    )
    sources = _run(
        execute_enterprise_add_sources(notebook_id, request, wait=wait, timeout=timeout),
        json_output,
    )
    if json_output:
        json_output_response({"sources": [_source_dict(s) for s in sources]})
    else:
        _print_sources(sources)
    if wait and sources and all(s.is_failed for s in sources):
        # Each failure is already rendered per-source above; output_error would
        # add a second, less specific message.
        raise SystemExit(1)  # cli-raw-exit: per-source failures already reported


@enterprise.command(name="delete")
@click.argument("notebook_ids", nargs=-1, required=True)
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@json_option
def delete_cmd(notebook_ids: tuple[str, ...], yes: bool, json_output: bool) -> None:
    """Delete notebooks. Irreversible."""
    if not yes:
        if json_output:
            _fail("Refusing to delete without --yes in JSON mode", "ValidationError", json_output)
        click.confirm(f"Delete {len(notebook_ids)} notebook(s)?", abort=True)
    _run(execute_enterprise_delete(list(notebook_ids)), json_output)
    if json_output:
        json_output_response({"deleted": list(notebook_ids)})
        return
    cli_print(f"Deleted {len(notebook_ids)} notebook(s).")


def register_enterprise_commands(cli: click.Group) -> None:
    """Attach the ``ge`` group to the root CLI."""
    cli.add_command(enterprise)
