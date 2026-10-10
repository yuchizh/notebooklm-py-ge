"""Tests for NotebookLM runtime endpoint configuration."""

import asyncio

import pytest

from notebooklm._env import (
    _ALLOWED_BASE_HOSTS,
    ENTERPRISE_BASE_HOST,
    ENTERPRISE_REGIONS,
    PERSONAL_APP_HOSTS,
    PERSONAL_BASE_HOST,
    PERSONAL_LEGACY_HOST,
    get_app_shell_url,
    get_base_host,
    get_base_url,
    get_enterprise_project,
)
from notebooklm._web.rows.sharing import decode_share_status
from notebooklm._web.sources import WebSourcesAPI
from notebooklm._web.sources.upload import SourceUploadPipeline
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient
from notebooklm.rpc import RPCMethod, get_batchexecute_url, get_query_url, get_upload_url
from notebooklm.types import RpcTelemetryEvent, ShareStatus
from tests._helpers.client_factory import build_client_shell_for_tests


def test_default_base_url_is_personal(monkeypatch):
    monkeypatch.delenv("NOTEBOOKLM_BASE_URL", raising=False)

    assert get_base_url() == "https://notebook.google.com"
    assert get_base_host() == "notebook.google.com"


def test_enterprise_base_url_via_env(monkeypatch):
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "https://notebook.cloud.google.com/us/")

    assert get_base_url() == "https://notebook.cloud.google.com/us"
    assert get_base_host() == "notebook.cloud.google.com"


def test_rebrand_alias_base_url_via_env(monkeypatch):
    """The post-rebrand personal host is selectable (undocumented, on purpose).

    Without a selectable rebrand host the login-landing and upload-host seams
    that must cope with *either* personal host cannot be exercised at all.
    """
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", f"https://{PERSONAL_LEGACY_HOST}/")

    assert get_base_url() == f"https://{PERSONAL_LEGACY_HOST}"
    assert get_base_host() == PERSONAL_LEGACY_HOST


def test_personal_app_hosts_holds_both_personal_hosts():
    """Both literals, not one.

    Deriving this set from ``PERSONAL_BASE_HOST`` alone collapses it to a
    single element and silently reverts #2015/#2020/#2038, whose whole point is
    that the app answers on two hosts at once.
    """
    assert {PERSONAL_BASE_HOST, PERSONAL_LEGACY_HOST} == PERSONAL_APP_HOSTS
    assert len(PERSONAL_APP_HOSTS) == 2
    assert ENTERPRISE_BASE_HOST not in PERSONAL_APP_HOSTS


def test_allowed_base_hosts_is_personal_app_hosts_plus_enterprise():
    assert PERSONAL_APP_HOSTS | {ENTERPRISE_BASE_HOST} == _ALLOWED_BASE_HOSTS
    assert isinstance(_ALLOWED_BASE_HOSTS, frozenset)


def test_base_url_normalizes_mixed_case_and_whitespace(monkeypatch):
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "  https://Notebook.Cloud.Google.com/US/  ")

    assert get_base_url() == "https://notebook.cloud.google.com/us"


def test_empty_base_url_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "")

    assert get_base_url() == "https://notebook.google.com"


@pytest.mark.parametrize(
    "value",
    [
        "http://notebooklm.google.com",
        "https://evil.example.com",
        "https://notebooklm.google.com:443",
        "https://user:notsecret@notebooklm.google.com",
        "https://notebooklm.google.com/path",
        "https://notebooklm.google.com?x=1",
        "https://notebooklm.google.com/#fragment",
        # The newly accepted alias host is subject to the identical rules --
        # widening the host set must not weaken any of them.
        "http://notebook.google.com",
        "https://notebook.google.com:443",
        "https://user:notsecret@notebook.google.com",
        "https://notebook.google.com/path",
        "https://notebook.google.com?x=1",
        "https://notebook.google.com/#fragment",
        # Neither may it accept lookalikes of the alias.
        "https://notebook.google.com.evil.example.com",
        "https://evil-notebook.google.com",
    ],
)
def test_base_url_validation_rejects_unsafe_values(monkeypatch, value):
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", value)

    with pytest.raises(ValueError, match="NOTEBOOKLM_BASE_URL"):
        get_base_url()


def test_rpc_endpoint_helpers_are_lazy(monkeypatch):
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "https://notebook.cloud.google.com/us")

    assert (
        get_batchexecute_url()
        == "https://notebook.cloud.google.com/us/_/CloudNotebookLmUi/data/batchexecute"
    )
    assert get_query_url().startswith("https://notebook.cloud.google.com/us/_/")
    assert get_upload_url() == "https://notebook.cloud.google.com/us/upload/_/"


def test_core_build_url_uses_enterprise_base_url(monkeypatch):
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "https://notebook.cloud.google.com/us")
    core = build_client_shell_for_tests(AuthTokens(cookies={}, csrf_token="csrf", session_id="sid"))

    # ``RpcExecutor.build_url`` consumes an ``AuthSnapshot`` so direct callers
    # outside the shared transport path must build one inline.
    from notebooklm._web.transport.request_types import AuthSnapshot

    snapshot = AuthSnapshot(
        csrf_token=core._auth.csrf_token,
        session_id=core._auth.session_id,
        authuser=core._auth.authuser,
        account_email=core._auth.account_email,
    )
    url = core._web_runtime.executor.build_url(RPCMethod.LIST_NOTEBOOKS, snapshot)

    assert url.startswith("https://notebook.cloud.google.com/us/_/CloudNotebookLmUi/data/")


@pytest.mark.asyncio
async def test_invalid_rpc_base_url_keeps_pre_chain_accounting(monkeypatch) -> None:
    """Request-build validation stays outside terminal metrics and queue timing."""
    events: list[RpcTelemetryEvent] = []
    client = build_client_shell_for_tests(
        AuthTokens(cookies={}, csrf_token="csrf", session_id="sid"),
        on_rpc_event=events.append,
    )
    await client.__aenter__()
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "https://evil.example")
    try:
        with pytest.raises(ValueError, match="NOTEBOOKLM_BASE_URL"):
            await client.raw.call(RPCMethod.LIST_NOTEBOOKS, [])
    finally:
        await client.close(drain=False)

    snapshot = client.metrics_snapshot()
    assert snapshot.rpc_calls_started == 1
    assert snapshot.rpc_calls_succeeded == 0
    assert snapshot.rpc_calls_failed == 0
    assert snapshot.rpc_queue_wait_seconds_total == 0.0
    assert events == []


@pytest.mark.asyncio
async def test_upload_start_uses_enterprise_url_and_headers(monkeypatch, httpx_mock):
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "https://notebook.cloud.google.com/us")
    auth = AuthTokens(cookies={"SID": "test"}, csrf_token="csrf", session_id="sid")
    upload_url = "https://notebook.cloud.google.com/us/upload/_/?upload_id=test"
    httpx_mock.add_response(
        method="POST",
        url="https://notebook.cloud.google.com/us/upload/_/?authuser=0",
        headers={"x-goog-upload-url": upload_url},
    )

    core = build_client_shell_for_tests(auth)
    await core.__aenter__()
    uploader = SourceUploadPipeline(
        rpc=core,
        supervisor=core._collaborators.call_supervisor,
        kernel=core._web_runtime.kernel,
        auth=core._auth,
        record_upload_queue_wait=core._collaborators.metrics.record_upload_queue_wait,
    )
    await uploader.open(asyncio.get_running_loop(), 1)
    try:
        api = WebSourcesAPI(
            core,
            supervisor=core._collaborators.call_supervisor,
            uploader=uploader,
        )
        result = await api._start_resumable_upload(
            "nb_123",
            "file.txt",
            12,
            "src_123",
            "text/plain",
        )
    finally:
        await uploader.prepare_close()
        await uploader.close_resources()
        await core.close()

    request = httpx_mock.get_request()
    assert result == upload_url
    assert request is not None
    assert str(request.url) == "https://notebook.cloud.google.com/us/upload/_/?authuser=0"
    assert request.headers["origin"] == "https://notebook.cloud.google.com"
    assert request.headers["referer"] == "https://notebook.cloud.google.com/"


@pytest.mark.asyncio
async def test_client_refresh_auth_uses_enterprise_base_url(monkeypatch, httpx_mock, tmp_path):
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "https://notebook.cloud.google.com/us")
    httpx_mock.add_response(
        url="https://notebook.cloud.google.com/us/",
        text='{"SNlM0e":"fresh_csrf","FdrFJe":"fresh_sid"}',
    )
    auth = AuthTokens(cookies={"SID": "test"}, csrf_token="old", session_id="old_sid")

    async with NotebookLMClient(auth, storage_path=tmp_path / "storage.json") as client:
        refreshed = await client.refresh_auth()

    request = httpx_mock.get_request()
    assert request is not None
    assert str(request.url) == "https://notebook.cloud.google.com/us/"
    assert refreshed.csrf_token == "fresh_csrf"
    assert refreshed.session_id == "fresh_sid"


def test_share_status_uses_enterprise_base_url(monkeypatch):
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "https://notebook.cloud.google.com/us")

    status = decode_share_status(ShareStatus, [[["owner@example.com"]], [True], 1000], "nb_123")

    assert status.share_url == "https://notebook.cloud.google.com/us/notebook/nb_123"


# --- Gemini Enterprise region-scoped base URL -------------------------------
#
# Gemini Enterprise serves the app from ``/<region>/``, never from the root of
# the host. Until this was supported, ``validate_base_url`` dropped any path it
# was given, so an operator who correctly configured
# ``https://notebook.cloud.google.com/us`` silently got RPCs aimed at the bare
# host -- which answers every path with a generic 404.


@pytest.mark.parametrize("region", sorted(ENTERPRISE_REGIONS))
def test_enterprise_base_url_accepts_each_region(monkeypatch, region):
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", f"https://{ENTERPRISE_BASE_HOST}/{region}")

    assert get_base_url() == f"https://{ENTERPRISE_BASE_HOST}/{region}"
    # The host is what cookie scoping and the login accept-set key on; it must
    # stay path-free.
    assert get_base_host() == ENTERPRISE_BASE_HOST


def test_enterprise_region_reaches_every_endpoint(monkeypatch):
    """The region must survive into all three endpoint builders, not just the base."""
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", f"https://{ENTERPRISE_BASE_HOST}/eu")
    prefix = f"https://{ENTERPRISE_BASE_HOST}/eu"

    assert get_batchexecute_url() == f"{prefix}/_/CloudNotebookLmUi/data/batchexecute"
    assert get_upload_url() == f"{prefix}/upload/_/"
    assert get_query_url().startswith(f"{prefix}/_/CloudNotebookLmUi/data/")


@pytest.mark.parametrize(
    "raw",
    [
        "https://notebook.cloud.google.com",  # bare host: not served
        "https://notebook.cloud.google.com/",
        "https://notebook.cloud.google.com/usa",  # not a region
        "https://notebook.cloud.google.com/us/extra",  # more than one segment
        "https://notebook.cloud.google.com/us?project=1",  # project is not part of the base
        "https://notebooklm.cloud.google.com/us",  # the old, unserved host
    ],
)
def test_enterprise_base_url_rejects_non_region_paths(monkeypatch, raw):
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", raw)

    with pytest.raises(ValueError):
        get_base_url()


@pytest.mark.parametrize("host", sorted(PERSONAL_APP_HOSTS))
def test_personal_hosts_still_reject_any_path(monkeypatch, host):
    """The region carve-out is enterprise-only; personal hosts serve from the root."""
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", f"https://{host}/us")

    with pytest.raises(ValueError):
        get_base_url()


def test_app_shell_url_carries_project_on_enterprise(monkeypatch):
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", f"https://{ENTERPRISE_BASE_HOST}/us")
    monkeypatch.setenv("NOTEBOOKLM_PROJECT_NUMBER", "260295163676")

    assert get_enterprise_project() == "260295163676"
    assert get_app_shell_url() == (f"https://{ENTERPRISE_BASE_HOST}/us/?project=260295163676")
    # ...and must not leak into the base URL, which is joined with suffixes.
    assert get_base_url() == f"https://{ENTERPRISE_BASE_HOST}/us"


def test_app_shell_url_without_project(monkeypatch):
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", f"https://{ENTERPRISE_BASE_HOST}/us")
    monkeypatch.delenv("NOTEBOOKLM_PROJECT_NUMBER", raising=False)

    assert get_enterprise_project() is None
    assert get_app_shell_url() == f"https://{ENTERPRISE_BASE_HOST}/us/"


def test_app_shell_url_ignores_project_on_personal_host(monkeypatch):
    monkeypatch.delenv("NOTEBOOKLM_BASE_URL", raising=False)
    monkeypatch.setenv("NOTEBOOKLM_PROJECT_NUMBER", "260295163676")

    assert get_app_shell_url() == f"https://{PERSONAL_BASE_HOST}/"


def test_enterprise_project_must_be_numeric(monkeypatch):
    monkeypatch.setenv("NOTEBOOKLM_PROJECT_NUMBER", "my-project-id")

    with pytest.raises(ValueError):
        get_enterprise_project()
