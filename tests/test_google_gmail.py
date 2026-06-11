"""Gmail v1 client tests.

HTTP is mocked at the httpx.Client boundary. These cover the contract
the bot tool (Slice 1) depends on:

  - list_thread_ids
  - get_thread parses headers + text/plain body + UNREAD label
  - body truncation
  - 401 raises GmailUnauthorizedError (token expired)
  - 403 raises GmailNotConnectedError (missing scope)
  - other 4xx/5xx + transport errors raise GmailError
  - per-thread failures inside list_recent_threads skip the bad row
  - access tokens, subjects, bodies NEVER appear in log records
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx
import pytest

from app.integrations import google_gmail

# ---------------------------------------------------------------------------
# httpx test fixtures
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status: int, payload: Any):
        self.status_code = status
        self._payload = payload

    def json(self) -> Any:
        if isinstance(self._payload, ValueError):
            raise self._payload
        return self._payload


class _FakeClient:
    """Stand-in for httpx.Client. Pops responses off a queue keyed
    loosely by URL substring so list+thread can interleave."""

    def __init__(self, responses: list[tuple[str, _Resp]]):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def get(self, url, headers=None, params=None):
        self.calls.append(
            {"url": url, "headers": dict(headers or {}), "params": dict(params or {})}
        )
        for i, (marker, resp) in enumerate(self._responses):
            if marker in url:
                self._responses.pop(i)
                return resp
        raise AssertionError(f"unexpected GET to {url}")


@pytest.fixture
def install_fake_client(monkeypatch):
    fakes: dict[str, _FakeClient] = {}

    def _install(responses: list[tuple[str, _Resp]]) -> _FakeClient:
        fake = _FakeClient(responses)
        fakes["current"] = fake
        monkeypatch.setattr(
            google_gmail.httpx, "Client", lambda *_a, **_kw: fake
        )
        return fake

    return _install


def _b64url(text: str) -> str:
    encoded = base64.urlsafe_b64encode(text.encode("utf-8")).rstrip(b"=")
    return encoded.decode("ascii")


# ---------------------------------------------------------------------------
# list_thread_ids
# ---------------------------------------------------------------------------


def test_list_thread_ids_happy_path(install_fake_client):
    fake = install_fake_client(
        [
            (
                "/threads",
                _Resp(200, {"threads": [{"id": "t1"}, {"id": "t2"}, {"id": "t3"}]}),
            )
        ]
    )

    ids = google_gmail.list_thread_ids("tok", limit=3)
    assert ids == ["t1", "t2", "t3"]
    call = fake.calls[0]
    assert call["headers"]["Authorization"] == "Bearer tok"
    assert call["params"]["maxResults"] == "3"
    assert call["params"]["labelIds"] == "INBOX"


def test_list_thread_ids_clamps_to_hard_max(install_fake_client):
    fake = install_fake_client(
        [("/threads", _Resp(200, {"threads": []}))]
    )
    google_gmail.list_thread_ids("tok", limit=9999)
    assert fake.calls[0]["params"]["maxResults"] == str(google_gmail.THREADS_HARD_MAX)


def test_list_thread_ids_empty_inbox(install_fake_client):
    install_fake_client([("/threads", _Resp(200, {}))])
    assert google_gmail.list_thread_ids("tok") == []


# ---------------------------------------------------------------------------
# get_thread parsing
# ---------------------------------------------------------------------------


def _thread_payload(thread_id: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {"id": thread_id, "snippet": "preview", "messages": messages}


def _msg(
    *,
    mid: str,
    subject: str = "hello",
    from_: str = "Acme <hi@acme.test>",
    to: str = "creator@example.com",
    body: str = "real body content",
    unread: bool = False,
    internal_date: str = "1717891200000",
) -> dict[str, Any]:
    return {
        "id": mid,
        "snippet": "snippet",
        "internalDate": internal_date,
        "labelIds": (["UNREAD"] if unread else []) + ["INBOX"],
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": from_},
                {"name": "To", "value": to},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": "Sun, 09 Jun 2024 00:00:00 +0000"},
            ],
            "body": {"data": _b64url(body)},
        },
    }


def test_get_thread_parses_headers_body_and_unread(install_fake_client):
    install_fake_client(
        [
            (
                "/threads/t1",
                _Resp(
                    200,
                    _thread_payload(
                        "t1",
                        [_msg(mid="m1", subject="brand deal", body="hey", unread=True)],
                    ),
                ),
            )
        ]
    )

    thread = google_gmail.get_thread("tok", thread_id="t1")
    assert thread.thread_id == "t1"
    assert thread.is_unread is True
    assert len(thread.messages) == 1
    m = thread.messages[0]
    assert m.subject == "brand deal"
    assert m.from_ == "Acme <hi@acme.test>"
    assert m.body_text == "hey"
    assert m.is_unread is True
    assert m.internal_date is not None
    assert m.internal_date.startswith("2024-")


def test_get_thread_walks_multipart_for_text_plain(install_fake_client):
    multipart = {
        "id": "m1",
        "labelIds": [],
        "internalDate": "1700000000000",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [{"name": "Subject", "value": "mixed"}],
            "parts": [
                {
                    "mimeType": "text/html",
                    "body": {"data": _b64url("<p>html version</p>")},
                },
                {
                    "mimeType": "text/plain",
                    "body": {"data": _b64url("plain version wins")},
                },
            ],
        },
    }
    install_fake_client(
        [("/threads/t2", _Resp(200, _thread_payload("t2", [multipart])))]
    )

    thread = google_gmail.get_thread("tok", thread_id="t2")
    assert thread.messages[0].body_text == "plain version wins"
    assert "html" not in (thread.messages[0].body_text or "")


def test_get_thread_caps_messages_per_thread(install_fake_client):
    many = [_msg(mid=f"m{i}", body=f"body {i}") for i in range(10)]
    install_fake_client(
        [("/threads/t3", _Resp(200, _thread_payload("t3", many)))]
    )

    thread = google_gmail.get_thread("tok", thread_id="t3")
    assert len(thread.messages) == google_gmail.MESSAGES_PER_THREAD
    # Newest messages are kept (Gmail returns oldest first).
    assert thread.messages[-1].message_id == "m9"


def test_get_thread_truncates_long_body(install_fake_client):
    huge = "x" * (google_gmail.BODY_MAX_CHARS + 500)
    install_fake_client(
        [
            (
                "/threads/t4",
                _Resp(200, _thread_payload("t4", [_msg(mid="m1", body=huge)])),
            )
        ]
    )

    thread = google_gmail.get_thread("tok", thread_id="t4")
    body = thread.messages[0].body_text or ""
    assert len(body) <= google_gmail.BODY_MAX_CHARS + 1  # +1 for ellipsis
    assert body.endswith("…")


def test_get_thread_strips_control_chars(install_fake_client):
    body_with_nulls = "real text\x00\x07\x1bmore text"
    install_fake_client(
        [
            (
                "/threads/t5",
                _Resp(
                    200,
                    _thread_payload(
                        "t5", [_msg(mid="m1", body=body_with_nulls)]
                    ),
                ),
            )
        ]
    )

    thread = google_gmail.get_thread("tok", thread_id="t5")
    assert thread.messages[0].body_text == "real textmore text"


# ---------------------------------------------------------------------------
# error mapping
# ---------------------------------------------------------------------------


def test_401_raises_unauthorized(install_fake_client):
    install_fake_client([("/threads", _Resp(401, {"error": {"message": "invalid"}}))])

    with pytest.raises(google_gmail.GmailUnauthorizedError):
        google_gmail.list_thread_ids("tok")


def test_403_raises_not_connected(install_fake_client):
    install_fake_client([("/threads", _Resp(403, {"error": {"message": "scope"}}))])

    with pytest.raises(google_gmail.GmailNotConnectedError):
        google_gmail.list_thread_ids("tok")


def test_500_raises_generic_gmail_error(install_fake_client):
    install_fake_client([("/threads", _Resp(500, {"error": "oops"}))])

    with pytest.raises(google_gmail.GmailError) as exc:
        google_gmail.list_thread_ids("tok")
    # And it's NOT one of the subclasses.
    assert not isinstance(exc.value, google_gmail.GmailUnauthorizedError)
    assert not isinstance(exc.value, google_gmail.GmailNotConnectedError)


def test_transport_failure_raises_generic_gmail_error(monkeypatch):
    def _explode(*_a, **_kw):
        raise httpx.ConnectError("network down")

    class _BadClient:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def get(self, *_a, **_kw):
            _explode()

    monkeypatch.setattr(google_gmail.httpx, "Client", lambda *_a, **_kw: _BadClient())

    with pytest.raises(google_gmail.GmailError):
        google_gmail.list_thread_ids("tok")


def test_non_json_response_raises_gmail_error(install_fake_client):
    install_fake_client(
        [("/threads", _Resp(200, ValueError("not json")))]
    )
    with pytest.raises(google_gmail.GmailError):
        google_gmail.list_thread_ids("tok")


# ---------------------------------------------------------------------------
# list_recent_threads — per-thread failures don't kill the others
# ---------------------------------------------------------------------------


def test_list_recent_threads_skips_one_failing_thread(install_fake_client):
    install_fake_client(
        [
            ("/threads", _Resp(200, {"threads": [{"id": "good"}, {"id": "bad"}]})),
            (
                "/threads/good",
                _Resp(
                    200, _thread_payload("good", [_msg(mid="m1", body="ok")])
                ),
            ),
            ("/threads/bad", _Resp(500, {"error": "oops"})),
        ]
    )

    threads = google_gmail.list_recent_threads("tok", limit=2)
    assert len(threads) == 1
    assert threads[0].thread_id == "good"


# ---------------------------------------------------------------------------
# log hygiene — no token, no subject, no body in any record
# ---------------------------------------------------------------------------


def test_no_token_subject_or_body_in_logs(install_fake_client, caplog):
    caplog.set_level(logging.DEBUG, logger=google_gmail.logger.name)
    secret_token = "very-secret-token-xyz"
    secret_subject = "TOP-SECRET-SUBJECT-LINE"
    secret_body = "TOP-SECRET-BODY-CONTENT"

    install_fake_client(
        [
            ("/threads", _Resp(200, {"threads": [{"id": "t1"}]})),
            (
                "/threads/t1",
                _Resp(
                    200,
                    _thread_payload(
                        "t1",
                        [_msg(mid="m1", subject=secret_subject, body=secret_body)],
                    ),
                ),
            ),
        ]
    )
    google_gmail.list_recent_threads(secret_token, limit=1)

    for record in caplog.records:
        text = record.getMessage()
        assert secret_token not in text
        assert secret_subject not in text
        assert secret_body not in text


def test_no_secrets_in_logs_on_error(install_fake_client, caplog):
    caplog.set_level(logging.DEBUG, logger=google_gmail.logger.name)
    secret_token = "another-secret-token"
    install_fake_client([("/threads", _Resp(500, {"error": "oops"}))])
    with pytest.raises(google_gmail.GmailError):
        google_gmail.list_thread_ids(secret_token)
    for record in caplog.records:
        assert secret_token not in record.getMessage()
