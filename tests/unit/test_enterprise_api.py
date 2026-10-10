"""Offline tests for the Gemini Enterprise official-API backend."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from notebooklm._enterprise import api as api_mod
from notebooklm._enterprise import config as config_mod
from notebooklm._enterprise.api import (
    EnterpriseAPI,
    EnterpriseNotebook,
    EnterpriseSource,
    drive_content,
    text_content,
    web_content,
    youtube_content,
)
from notebooklm._enterprise.credentials import EnterpriseCredentials, load_adc
from notebooklm.exceptions import (
    AuthError,
    NotFoundError,
    ServerError,
    ValidationError,
)

_ADC = {
    "type": "authorized_user",
    "client_id": "cid.apps.googleusercontent.com",
    "client_secret": "secret",
    "refresh_token": "refresh",
    "quota_project_id": "proj-from-adc",
    "account": "",
}


class _Recorder:
    """Collects the requests a test's stub transport served."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []


def _transport(
    recorder: _Recorder, handler: Any
) -> httpx.MockTransport:
    def dispatch(request: httpx.Request) -> httpx.Response:
        recorder.requests.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if request.url.host == "openidconnect.googleapis.com":
            return httpx.Response(200, json={"email": "seat@example.com"})
        return handler(request)

    return httpx.MockTransport(dispatch)


def _make_api(
    recorder: _Recorder, handler: Any, **kwargs: Any
) -> EnterpriseAPI:
    client = httpx.AsyncClient(transport=_transport(recorder, handler))
    return EnterpriseAPI(
        project=kwargs.pop("project", "proj"),
        location=kwargs.pop("location", "us"),
        credentials=EnterpriseCredentials(dict(_ADC)),
        client=client,
        **kwargs,
    )


# --------------------------------------------------------------------------
# config


def test_location_defaults_to_us(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTEBOOKLM_ENTERPRISE_LOCATION", raising=False)
    assert config_mod.get_location() == "us"


def test_location_rejects_unknown_region(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTEBOOKLM_ENTERPRISE_LOCATION", "antarctica")
    with pytest.raises(ValueError, match="must be one of"):
        config_mod.get_location()


@pytest.mark.parametrize(
    ("location", "host"),
    [
        ("us", "us-discoveryengine.googleapis.com"),
        ("eu", "eu-discoveryengine.googleapis.com"),
        # The global location is the one served by the unprefixed host; using a
        # regional prefix there is a hard 400 from the API.
        ("global", "discoveryengine.googleapis.com"),
    ],
)
def test_api_host_is_regional(location: str, host: str) -> None:
    assert config_mod.get_api_host(location) == host


def test_project_prefers_env_over_adc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTEBOOKLM_ENTERPRISE_PROJECT", "proj-from-env")
    assert config_mod.get_project() == "proj-from-env"


def test_project_errors_without_any_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.delenv("NOTEBOOKLM_ENTERPRISE_PROJECT", raising=False)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "absent.json"))
    with pytest.raises(ValueError, match="No enterprise project configured"):
        config_mod.get_project()


# --------------------------------------------------------------------------
# credentials


def test_load_adc_rejects_service_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    path = tmp_path / "sa.json"
    path.write_text(json.dumps({"type": "service_account", "client_email": "x@y.iam"}))
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(path))
    # Enterprise refuses service accounts outright, so failing here with an
    # explanation beats a 401 from an unrelated call later.
    with pytest.raises(AuthError, match="service accounts are rejected"):
        load_adc()


def test_load_adc_reports_missing_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    path = tmp_path / "partial.json"
    path.write_text(json.dumps({"type": "authorized_user", "client_id": "cid"}))
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(path))
    with pytest.raises(AuthError, match="client_secret, refresh_token"):
        load_adc()


@pytest.mark.anyio
async def test_token_is_cached_across_calls() -> None:
    recorder = _Recorder()
    api = _make_api(recorder, lambda r: httpx.Response(200, json={"notebooks": []}))
    async with api:
        await api.list_notebooks()
        await api.list_notebooks()
    token_calls = [r for r in recorder.requests if r.url.host == "oauth2.googleapis.com"]
    assert len(token_calls) == 1


@pytest.mark.anyio
async def test_whoami_falls_back_to_userinfo() -> None:
    recorder = _Recorder()
    api = _make_api(recorder, lambda r: httpx.Response(200, json={}))
    async with api:
        # ADC wrote an empty ``account``, which means unknown, not anonymous.
        assert api.account is None
        assert await api.whoami() == "seat@example.com"


# --------------------------------------------------------------------------
# request shaping


@pytest.mark.anyio
async def test_calls_regional_host_with_quota_project() -> None:
    recorder = _Recorder()
    api = _make_api(recorder, lambda r: httpx.Response(200, json={"notebooks": []}))
    async with api:
        await api.list_notebooks()
    call = recorder.requests[-1]
    assert call.url.host == "us-discoveryengine.googleapis.com"
    assert call.url.path == (
        "/v1alpha/projects/proj/locations/us/notebooks:listRecentlyViewed"
    )
    assert call.headers["authorization"] == "Bearer tok"
    assert call.headers["x-goog-user-project"] == "proj-from-adc"


@pytest.mark.anyio
async def test_create_posts_title() -> None:
    recorder = _Recorder()
    api = _make_api(
        recorder,
        lambda r: httpx.Response(200, json={"notebookId": "nb1", "title": "T"}),
    )
    async with api:
        notebook = await api.create_notebook("T")
    call = recorder.requests[-1]
    assert call.method == "POST"
    assert json.loads(call.content) == {"title": "T"}
    assert notebook.id == "nb1"


@pytest.mark.anyio
async def test_batch_delete_sends_full_resource_names() -> None:
    recorder = _Recorder()
    api = _make_api(recorder, lambda r: httpx.Response(200, content=b""))
    async with api:
        await api.delete_notebooks(["a", "b"])
    assert json.loads(recorder.requests[-1].content) == {
        "names": [
            "projects/proj/locations/us/notebooks/a",
            "projects/proj/locations/us/notebooks/b",
        ]
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    "call",
    [
        lambda api: api.delete_notebooks([]),
        lambda api: api.add_sources("nb", []),
        lambda api: api.delete_sources("nb", []),
        lambda api: api.create_notebook("   "),
        lambda api: api.list_notebooks(page_size=0),
        lambda api: api.list_notebooks(page_size=101),
    ],
)
async def test_empty_and_out_of_range_inputs_are_rejected_locally(call: Any) -> None:
    recorder = _Recorder()
    api = _make_api(recorder, lambda r: httpx.Response(200, json={}))
    async with api:
        with pytest.raises(ValidationError):
            await call(api)
    # Nothing beyond the token exchange should have reached the network.
    assert [r.url.host for r in recorder.requests] == []


# --------------------------------------------------------------------------
# UserContent builders


def test_user_content_builders() -> None:
    assert text_content("body", "name") == {
        "textContent": {"content": "body", "sourceName": "name"}
    }
    assert web_content("https://e.test") == {"webContent": {"url": "https://e.test"}}
    assert web_content("https://e.test", "N") == {
        "webContent": {"url": "https://e.test", "sourceName": "N"}
    }
    assert youtube_content("https://yt.test/v") == {
        "videoContent": {"youtubeUrl": "https://yt.test/v"}
    }
    assert drive_content("docid", "application/pdf") == {
        "googleDriveContent": {"documentId": "docid", "mimeType": "application/pdf"}
    }


# --------------------------------------------------------------------------
# response parsing


def test_source_parsing_reports_failure_reason() -> None:
    source = EnterpriseSource.from_api(
        {
            "name": "projects/p/locations/us/notebooks/n/sources/s1",
            "title": "https://x.test",
            "settings": {
                "status": "SOURCE_STATUS_ERROR",
                "failureReason": {"sourceUnreachable": {"errorDetails": "ERROR_REASON_INVALID_URL"}},
            },
        }
    )
    assert source.id == "s1"
    assert source.is_failed and source.is_settled and not source.is_complete
    assert source.failure == "source unreachable (errorDetails=ERROR_REASON_INVALID_URL)"


def test_pending_source_is_not_settled() -> None:
    source = EnterpriseSource.from_api({"settings": {"status": "SOURCE_STATUS_PENDING"}})
    assert not source.is_settled
    assert source.failure is None


def test_notebook_parsing_reads_metadata_and_sources() -> None:
    notebook = EnterpriseNotebook.from_api(
        {
            "name": "projects/p/locations/us/notebooks/nb9",
            "title": "KB",
            "emoji": "*",
            "metadata": {"createTime": "2026-01-01T00:00:00Z", "isShared": True},
            "sources": [
                {
                    "sourceId": {"id": "s1"},
                    "title": "doc",
                    "settings": {"status": "SOURCE_STATUS_COMPLETE"},
                    "metadata": {"wordCount": 12, "tokenCount": 20},
                }
            ],
        }
    )
    assert notebook.id == "nb9"
    assert notebook.is_shared
    assert notebook.sources[0].word_count == 12
    assert notebook.sources[0].is_complete


@pytest.mark.anyio
async def test_wait_for_sources_polls_until_settled(monkeypatch: pytest.MonkeyPatch) -> None:
    states = [
        {"settings": {"status": "SOURCE_STATUS_PENDING"}},
        {"settings": {"status": "SOURCE_STATUS_COMPLETE"}},
    ]
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        index = min(calls["n"], len(states) - 1)
        calls["n"] += 1
        return httpx.Response(200, json={"notebookId": "nb", "sources": [states[index]]})

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(api_mod.asyncio, "sleep", fake_sleep)
    recorder = _Recorder()
    api = _make_api(recorder, handler)
    async with api:
        sources = await api.wait_for_sources("nb", poll_interval=0.25)
    assert [s.status for s in sources] == ["SOURCE_STATUS_COMPLETE"]
    assert sleeps == [0.25]


@pytest.mark.anyio
async def test_wait_for_sources_returns_last_seen_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"sources": [{"settings": {"status": "SOURCE_STATUS_PENDING"}}]}
        )

    recorder = _Recorder()
    api = _make_api(recorder, handler)
    async with api:
        # A zero budget must still yield one read rather than hanging or raising:
        # a stuck ingest is the caller's call to make, not this method's.
        sources = await api.wait_for_sources("nb", timeout=0.0)
    assert [s.status for s in sources] == ["SOURCE_STATUS_PENDING"]


# --------------------------------------------------------------------------
# error mapping


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, AuthError),
        (403, AuthError),
        (404, NotFoundError),
        (400, ValidationError),
        (500, ServerError),
        (503, ServerError),
    ],
)
async def test_http_failures_map_to_typed_errors(status: int, expected: type) -> None:
    recorder = _Recorder()
    api = _make_api(
        recorder,
        lambda r: httpx.Response(status, json={"error": {"message": "boom"}}),
    )
    async with api:
        with pytest.raises(expected, match="boom"):
            await api.get_notebook("nb")
