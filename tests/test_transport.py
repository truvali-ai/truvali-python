"""Tests of authentication, token caching, retries and error mapping."""

from __future__ import annotations

import logging

import pytest
import requests
from urllib3 import HTTPSConnectionPool
from urllib3.connection import HTTPSConnection
from urllib3.exceptions import MaxRetryError, NewConnectionError

from truvali import (
    AuthenticationError,
    BadRequestError,
    ErrorCode,
    MemoryTokenStore,
    NetworkError,
    PayloadTooLargeError,
    PermissionDeniedError,
    RateLimitError,
    RetryPolicy,
    ServerError,
    UnexpectedResponseError,
)
from truvali._version import __version__
from truvali.transport import TOKEN_PATH, AccessToken, TokenManager, user_agent

from .fakes import (
    API_KEY,
    API_SECRET,
    START_TIME,
    envelope,
    error_document,
    login_document,
    make_harness,
)

CHECK_PATH = "/api/merchant/auth/check/"
CHECK_DATA = {"client": "Example Source", "session_id": "session-0001"}


def test_login_happens_lazily_and_the_token_is_reused() -> None:
    """The first call logs in; later calls reuse the cached token."""
    harness = make_harness()
    harness.adapter.reply(200, login_document("token-a"))
    harness.adapter.reply(200, envelope(CHECK_DATA))
    harness.adapter.reply(200, envelope(CHECK_DATA))

    assert harness.adapter.sent == []
    harness.client.check_auth()
    harness.client.check_auth()

    assert harness.adapter.paths() == [TOKEN_PATH, CHECK_PATH, CHECK_PATH]
    assert harness.adapter.body(0) == {"api_key": API_KEY, "api_secret": API_SECRET}
    assert "Authorization" not in harness.adapter.sent[0].headers
    assert harness.adapter.sent[1].headers["Authorization"] == "Bearer token-a"
    assert harness.adapter.sent[2].headers["Authorization"] == "Bearer token-a"


def test_token_is_renewed_before_expiry() -> None:
    """The token is renewed once a tenth of its lifetime (at most 300 s) remains."""
    harness = make_harness()
    harness.adapter.reply(200, login_document("token-a", expires_in=1000))
    harness.adapter.reply(200, envelope(CHECK_DATA))
    harness.adapter.reply(200, envelope(CHECK_DATA))
    harness.adapter.reply(200, login_document("token-b", expires_in=1000))
    harness.adapter.reply(200, envelope(CHECK_DATA))

    harness.client.check_auth()
    harness.clock.now = START_TIME + 899
    harness.client.check_auth()
    harness.clock.now = START_TIME + 901
    harness.client.check_auth()

    assert harness.adapter.paths() == [TOKEN_PATH, CHECK_PATH, CHECK_PATH, TOKEN_PATH, CHECK_PATH]
    assert harness.adapter.sent[4].headers["Authorization"] == "Bearer token-b"


def test_long_lived_token_renews_five_minutes_early() -> None:
    """For a 12 hour token the safety margin is capped at 300 seconds."""
    token = AccessToken("token-a", issued_at=0.0, expires_at=43200.0)
    assert token.renew_at == 42900.0
    assert "token-a" not in repr(token)


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "token_expired"),
        (401, "token_invalid"),
        (401, "session_invalid"),
        (403, "ip_mismatch"),
    ],
)
def test_rejected_token_triggers_one_login_and_one_retry(status: int, code: str) -> None:
    """Token and IP binding errors log in again and repeat the call once.

    Args:
        status: HTTP status of the rejection.
        code: The ``data.error`` code of the rejection.
    """
    harness = make_harness()
    harness.adapter.reply(200, login_document("token-a"))
    harness.adapter.reply(status, error_document("Rejected.", code))
    harness.adapter.reply(200, login_document("token-b"))
    harness.adapter.reply(200, envelope(CHECK_DATA))

    check = harness.client.check_auth()

    assert check.client == "Example Source"
    assert harness.adapter.paths() == [TOKEN_PATH, CHECK_PATH, TOKEN_PATH, CHECK_PATH]
    assert harness.adapter.sent[3].headers["Authorization"] == "Bearer token-b"
    assert harness.clock.sleeps == []


def test_persistent_token_error_is_raised_after_exactly_one_retry() -> None:
    """A second token rejection is raised instead of looping."""
    harness = make_harness()
    harness.adapter.reply(200, login_document("token-a"))
    harness.adapter.reply(401, error_document("Token is invalid or expired.", "token_expired"))
    harness.adapter.reply(200, login_document("token-b"))
    harness.adapter.reply(401, error_document("Token is invalid or expired.", "token_expired"))

    with pytest.raises(AuthenticationError) as caught:
        harness.client.check_auth()

    assert caught.value.code == ErrorCode.TOKEN_EXPIRED
    assert caught.value.status_code == 401
    assert caught.value.request_id == "req-error"
    assert len(harness.adapter.sent) == 4


def test_persistent_ip_mismatch_raises_permission_denied() -> None:
    """A second ip_mismatch is raised as PermissionDeniedError."""
    harness = make_harness()
    harness.adapter.reply(200, login_document("token-a"))
    harness.adapter.reply(403, error_document("Other IP.", "ip_mismatch"))
    harness.adapter.reply(200, login_document("token-b"))
    harness.adapter.reply(403, error_document("Other IP.", "ip_mismatch"))

    with pytest.raises(PermissionDeniedError) as caught:
        harness.client.check_auth()

    assert caught.value.code == "ip_mismatch"
    assert len(harness.adapter.sent) == 4


def test_invalid_credentials_do_not_loop() -> None:
    """A rejected login is raised at once, without retrying."""
    harness = make_harness()
    harness.adapter.reply(
        401, error_document("Invalid credentials provided.", "invalid_credentials", request_id=None)
    )

    with pytest.raises(AuthenticationError) as caught:
        harness.client.check_auth()

    assert caught.value.code == ErrorCode.INVALID_CREDENTIALS
    assert caught.value.request_id is None
    assert harness.adapter.paths() == [TOKEN_PATH]
    assert harness.clock.sleeps == []


@pytest.mark.parametrize("code", ["ip_not_allowed", "credential_inactive", "blocked"])
def test_operator_action_codes_are_not_retried(code: str) -> None:
    """Codes that need an operator raise PermissionDeniedError without a new login.

    Args:
        code: The ``data.error`` code.
    """
    harness = make_harness()
    harness.adapter.reply(200, login_document())
    harness.adapter.reply(403, error_document("Not allowed.", code))

    with pytest.raises(PermissionDeniedError) as caught:
        harness.client.check_auth()

    assert caught.value.code == code
    assert harness.adapter.paths() == [TOKEN_PATH, CHECK_PATH]


def test_rate_limit_waits_for_retry_after() -> None:
    """A 429 waits max(Retry-After, backoff) and then retries."""
    harness = make_harness()
    harness.adapter.reply(200, login_document())
    harness.adapter.reply(
        429,
        {
            "status": False,
            "message": "Too many requests; retry after the indicated delay.",
            "error": "rate_limited",
            "data": {"error": "rate_limited", "retry_after": 3},
            "request_id": "req-limited",
        },
        headers={"Retry-After": "3"},
    )
    harness.adapter.reply(200, envelope(CHECK_DATA))

    harness.client.check_auth()

    assert harness.clock.sleeps == [3.0]
    assert harness.adapter.paths() == [TOKEN_PATH, CHECK_PATH, CHECK_PATH]


def test_rate_limit_beyond_backoff_max_is_raised_at_once() -> None:
    """A Retry-After longer than backoff_max is not waited for."""
    harness = make_harness()
    harness.adapter.reply(200, login_document())
    harness.adapter.reply(
        429,
        error_document("Too many requests.", "rate_limited", request_id="req-limited"),
        headers={"Retry-After": "42"},
    )

    with pytest.raises(RateLimitError) as caught:
        harness.client.check_auth()

    assert caught.value.retry_after == 42.0
    assert caught.value.request_id == "req-limited"
    assert harness.clock.sleeps == []


def test_rate_limit_without_header_uses_data_retry_after() -> None:
    """data.retry_after is used when the header is absent."""
    harness = make_harness(retry=RetryPolicy(max_retries=0))
    harness.adapter.reply(200, login_document())
    harness.adapter.reply(
        429,
        {
            "status": False,
            "message": "Slow down.",
            "data": {"error": "rate_limited", "retry_after": 7},
        },
    )

    with pytest.raises(RateLimitError) as caught:
        harness.client.check_auth()

    assert caught.value.retry_after == 7.0


def test_html_413_maps_to_payload_too_large() -> None:
    """The proxy's HTML 413 page becomes PayloadTooLargeError."""
    harness = make_harness()
    harness.adapter.reply(200, login_document())
    harness.adapter.reply(
        413, raw=b"<html><body><h1>413 Request Entity Too Large</h1></body></html>"
    )

    with pytest.raises(PayloadTooLargeError) as caught:
        harness.client.check_auth()

    assert caught.value.status_code == 413
    assert caught.value.code is None
    assert harness.clock.sleeps == []


def test_json_413_keeps_the_server_message() -> None:
    """The JSON 413 for too many records keeps its message and request id."""
    harness = make_harness()
    harness.adapter.reply(200, login_document())
    harness.adapter.reply(
        413, error_document("Request limit exceeded.", None, request_id="req-413")
    )

    with pytest.raises(PayloadTooLargeError) as caught:
        harness.client.check_auth()

    assert caught.value.message == "Request limit exceeded."
    assert caught.value.request_id == "req-413"


def test_server_errors_are_retried_until_success() -> None:
    """HTTP 5xx is retried with exponential backoff."""
    harness = make_harness()
    harness.adapter.reply(200, login_document())
    harness.adapter.reply(502, raw=b"<html>Bad Gateway</html>")
    harness.adapter.reply(503, raw=b"<html>Unavailable</html>")
    harness.adapter.reply(200, envelope(CHECK_DATA))

    harness.client.check_auth()

    assert harness.clock.sleeps == [0.5, 1.0]


def test_server_errors_give_up_after_max_retries() -> None:
    """After max_retries the last 5xx is raised as ServerError."""
    harness = make_harness()
    harness.adapter.reply(200, login_document())
    for _ in range(3):
        harness.adapter.reply(500, raw=b"<html>Server Error</html>")

    with pytest.raises(ServerError) as caught:
        harness.client.check_auth()

    assert caught.value.status_code == 500
    assert harness.clock.sleeps == [0.5, 1.0]
    assert len(harness.adapter.sent) == 4


def test_internal_error_code_maps_to_server_error() -> None:
    """data.error internal_error maps to ServerError whatever the status."""
    harness = make_harness(retry=RetryPolicy(max_retries=0))
    harness.adapter.reply(500, error_document("Unexpected.", "internal_error", request_id=None))

    with pytest.raises(ServerError) as caught:
        harness.client.check_auth()

    assert caught.value.code == ErrorCode.INTERNAL_ERROR


def test_client_errors_are_not_retried() -> None:
    """A 400 is final and keeps the server message."""
    harness = make_harness()
    harness.adapter.reply(200, login_document())
    harness.adapter.reply(400, error_document("Payload contains no records.", None))

    with pytest.raises(BadRequestError) as caught:
        harness.client.check_auth()

    assert str(caught.value) == "Payload contains no records. (status=400, request_id=req-error)"
    assert harness.clock.sleeps == []


def test_non_json_success_is_unexpected() -> None:
    """A 200 without a JSON envelope raises UnexpectedResponseError."""
    harness = make_harness()
    harness.adapter.reply(200, login_document())
    harness.adapter.reply(200, raw=b"<html>maintenance</html>")

    with pytest.raises(UnexpectedResponseError):
        harness.client.check_auth()


def test_redirects_are_not_followed() -> None:
    """A 3xx is reported instead of being followed."""
    harness = make_harness()
    harness.adapter.reply(200, login_document())
    harness.adapter.reply(302, raw=b"", headers={"Location": "https://elsewhere.example.com/"})

    with pytest.raises(UnexpectedResponseError) as caught:
        harness.client.check_auth()

    assert caught.value.status_code == 302
    assert len(harness.adapter.sent) == 2


def test_login_response_without_expiry_is_unexpected() -> None:
    """A login answer without a positive expires_in is rejected."""
    harness = make_harness()
    harness.adapter.reply(
        200, {"status": True, "message": "Login successful.", "data": {"access_token": "t"}}
    )

    with pytest.raises(UnexpectedResponseError):
        harness.client.check_auth()


def test_connect_timeout_is_retried_and_flagged_as_not_reached() -> None:
    """A connect timeout never reached the server and is retried."""
    harness = make_harness(retry=RetryPolicy(max_retries=1, retry_on_timeout=False))
    harness.adapter.fail(requests.ConnectTimeout("connect timed out"))
    harness.adapter.fail(requests.ConnectTimeout("connect timed out"))

    with pytest.raises(NetworkError) as caught:
        harness.client.check_auth()

    assert caught.value.may_have_reached_server is False
    assert harness.clock.sleeps == [0.5]
    assert len(harness.adapter.sent) == 2


def test_refused_connection_is_flagged_as_not_reached() -> None:
    """A refused connection is mapped to NetworkError without reaching the server."""
    harness = make_harness(retry=RetryPolicy(max_retries=0))
    pool = HTTPSConnectionPool("truvali.example.com")
    refused = NewConnectionError(HTTPSConnection("truvali.example.com"), "refused")
    harness.adapter.fail(requests.ConnectionError(MaxRetryError(pool, "/", refused)))

    with pytest.raises(NetworkError) as caught:
        harness.client.check_auth()

    assert caught.value.may_have_reached_server is False


def test_read_timeout_may_have_reached_the_server() -> None:
    """A read timeout is retried by default and flagged as possibly processed."""
    harness = make_harness()
    harness.adapter.reply(200, login_document())
    harness.adapter.fail(requests.ReadTimeout("read timed out"))
    harness.adapter.reply(200, envelope(CHECK_DATA))

    harness.client.check_auth()

    assert harness.clock.sleeps == [0.5]


def test_read_timeout_is_final_when_retry_on_timeout_is_off() -> None:
    """With retry_on_timeout disabled a read timeout is raised at once."""
    harness = make_harness(retry=RetryPolicy(retry_on_timeout=False))
    harness.adapter.reply(200, login_document())
    harness.adapter.fail(requests.ReadTimeout("read timed out"))

    with pytest.raises(NetworkError) as caught:
        harness.client.check_auth()

    assert caught.value.may_have_reached_server is True
    assert harness.clock.sleeps == []


def test_headers_and_timeouts_are_sent() -> None:
    """User-Agent, Accept and the configured timeouts reach the adapter."""
    harness = make_harness(user_agent_suffix="ledger-service/2.1", read_timeout=12.0)
    harness.adapter.reply(200, login_document())
    harness.adapter.reply(200, envelope(CHECK_DATA))

    harness.client.check_auth()

    login_headers = harness.adapter.sent[0].headers
    assert login_headers["User-Agent"].startswith(f"truvali-python/{__version__} python/")
    assert login_headers["User-Agent"].endswith(" ledger-service/2.1")
    assert login_headers["Content-Type"] == "application/json"
    assert harness.adapter.sent[1].headers["Accept"] == "application/json"
    assert harness.adapter.timeouts == [(5.0, 12.0), (5.0, 12.0)]


def test_user_agent_format() -> None:
    """The User-Agent names the SDK, Python and requests versions."""
    agent = user_agent()
    parts = agent.split(" ")
    assert parts[0] == f"truvali-python/{__version__}"
    assert parts[1].startswith("python/3.")
    assert parts[2] == f"requests/{requests.__version__}"


def test_logs_never_contain_secrets_tokens_or_bodies(caplog: pytest.LogCaptureFixture) -> None:
    """Debug logging carries method, path, status and request id only.

    Args:
        caplog: The pytest log capture fixture.
    """
    caplog.set_level(logging.DEBUG, logger="truvali")
    harness = make_harness()
    harness.adapter.reply(200, login_document("token-secret-value"))
    harness.adapter.reply(503, raw=b"<html>busy</html>")
    harness.adapter.reply(200, envelope(CHECK_DATA, request_id="req-visible"))

    harness.client.check_auth()

    text = caplog.text
    assert "req-visible" in text
    assert CHECK_PATH in text
    for secret in (API_SECRET, API_KEY, "token-secret-value", "session-0001", "Example Source"):
        assert secret not in text


def test_custom_token_store_is_used() -> None:
    """A token found in the store is used without logging in."""
    store = MemoryTokenStore()
    store.set(AccessToken("token-stored", START_TIME, START_TIME + 3600))
    harness = make_harness(token_store=store)
    harness.adapter.reply(200, envelope(CHECK_DATA))

    harness.client.check_auth()

    assert harness.adapter.paths() == [CHECK_PATH]
    assert harness.adapter.sent[0].headers["Authorization"] == "Bearer token-stored"


def test_failed_renewal_falls_back_to_the_unexpired_token() -> None:
    """A transient renewal failure keeps using the token until it expires."""
    harness = make_harness(retry=RetryPolicy(max_retries=0))
    harness.adapter.reply(200, login_document("token-a", expires_in=1000))
    harness.adapter.reply(200, envelope(CHECK_DATA))
    harness.adapter.reply(503, raw=b"<html>busy</html>")
    harness.adapter.reply(200, envelope(CHECK_DATA))

    harness.client.check_auth()
    harness.clock.now = START_TIME + 950
    harness.client.check_auth()

    assert harness.adapter.paths() == [TOKEN_PATH, CHECK_PATH, TOKEN_PATH, CHECK_PATH]
    assert harness.adapter.sent[3].headers["Authorization"] == "Bearer token-a"


def test_invalidate_keeps_a_token_renewed_meanwhile() -> None:
    """Invalidating a stale value does not drop a newer token."""
    issued = iter(["token-a", "token-b"])
    manager = TokenManager(lambda: AccessToken(next(issued), 0.0, 3600.0), clock=lambda: 10.0)
    assert manager.token() == "token-a"
    manager.invalidate("token-a")
    assert manager.token() == "token-b"
    manager.invalidate("token-a")
    assert manager.token() == "token-b"
    manager.invalidate()
    with pytest.raises(StopIteration):
        manager.token()
