"""Test doubles: a scripted HTTP adapter, a fake clock and a client harness.

The adapter is mounted on a real ``requests.Session``, so the SDK runs its normal
code path while every response comes from a queue the test fills in advance.
"""

from __future__ import annotations

import io
import json
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3 import HTTPResponse

from truvali import TruvaliClient, TruvaliConfig
from truvali.config import RetryPolicy
from truvali.transport import TokenStore, Transport

BASE_URL = "https://truvali.example.com"
API_KEY = "key-0000-test"
API_SECRET = "secret-0000-test"
START_TIME = 1_800_000_000.0


@dataclass
class ScriptedReply:
    """A response the fake adapter returns.

    Attributes:
        status: HTTP status.
        body: Raw body bytes.
        headers: Response headers.
    """

    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


class FakeAdapter(HTTPAdapter):
    """Transport adapter that answers from a queue and records every request."""

    def __init__(self) -> None:
        """Initialise an adapter with an empty queue."""
        super().__init__()
        self.queue: deque[ScriptedReply | BaseException] = deque()
        self.sent: list[requests.PreparedRequest] = []
        self.timeouts: list[object] = []

    def reply(
        self,
        status: int,
        document: Any = None,
        *,
        raw: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        """Queue a response.

        Args:
            status: HTTP status.
            document: JSON document of the body; ignored when ``raw`` is given.
            raw: Raw body bytes, for non-JSON bodies.
            headers: Extra response headers.
        """
        if raw is None:
            body = b"" if document is None else json.dumps(document).encode("utf-8")
            content_type = "application/json"
        else:
            body = raw
            content_type = "text/html"
        merged = {"Content-Type": content_type, **(headers or {})}
        self.queue.append(ScriptedReply(status, body, merged))

    def fail(self, error: BaseException) -> None:
        """Queue an exception raised instead of a response.

        Args:
            error: The exception to raise.
        """
        self.queue.append(error)

    def send(
        self,
        request: requests.PreparedRequest,
        stream: bool = False,
        timeout: float | tuple[float, float] | tuple[float, None] | None = None,
        verify: bool | str = True,
        cert: bytes | str | tuple[bytes | str, bytes | str] | None = None,
        proxies: Mapping[str, str] | None = None,
    ) -> requests.Response:
        """Record the request and return the next queued response.

        Args:
            request: The prepared request.
            stream: Ignored.
            timeout: Recorded for assertions.
            verify: Ignored.
            cert: Ignored.
            proxies: Ignored.

        Returns:
            The queued response.

        Raises:
            AssertionError: If no response is queued.
            BaseException: The queued exception, when one is next.
        """
        self.sent.append(request)
        self.timeouts.append(timeout)
        if not self.queue:
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")
        item = self.queue.popleft()
        if isinstance(item, BaseException):
            raise item
        raw = HTTPResponse(
            body=io.BytesIO(item.body),
            headers=item.headers,
            status=item.status,
            preload_content=False,
        )
        return self.build_response(request, raw)

    def paths(self) -> list[str]:
        """List the paths of the recorded requests.

        Returns:
            The URL paths, in order.
        """
        return [urlsplit(request.url or "").path for request in self.sent]

    def query(self, index: int) -> dict[str, list[str]]:
        """Decode the query string of a recorded request.

        Args:
            index: Position of the request.

        Returns:
            The query parameters.
        """
        return parse_qs(urlsplit(self.sent[index].url or "").query)

    def body(self, index: int) -> Any:
        """Decode the JSON body of a recorded request.

        Args:
            index: Position of the request.

        Returns:
            The decoded body.
        """
        raw = self.sent[index].body
        assert isinstance(raw, bytes)
        return json.loads(raw)


class FakeClock:
    """Controllable Unix clock whose ``sleep`` only records and advances time."""

    def __init__(self, now: float = START_TIME) -> None:
        """Initialise the clock.

        Args:
            now: The initial Unix time.
        """
        self.now = now
        self.sleeps: list[float] = []

    def time(self) -> float:
        """Return the current fake time.

        Returns:
            The Unix time.
        """
        return self.now

    def sleep(self, seconds: float) -> None:
        """Record a sleep and advance the clock.

        Args:
            seconds: The requested delay.
        """
        self.sleeps.append(seconds)
        self.now += seconds


@dataclass
class Harness:
    """A client wired to a fake adapter and a fake clock.

    Attributes:
        client: The client under test.
        adapter: The scripted adapter.
        clock: The fake clock.
        transport: The transport of the client.
    """

    client: TruvaliClient
    adapter: FakeAdapter
    clock: FakeClock
    transport: Transport


def make_config(**overrides: Any) -> TruvaliConfig:
    """Build a configuration with fake credentials.

    Args:
        **overrides: Settings that replace the defaults.

    Returns:
        The configuration.
    """
    settings: dict[str, Any] = {
        "base_url": BASE_URL,
        "api_key": API_KEY,
        "api_secret": API_SECRET,
        "retry": RetryPolicy(max_retries=2, backoff_initial=0.5, backoff_max=8.0),
    }
    settings.update(overrides)
    return TruvaliConfig(**settings)


def make_harness(token_store: TokenStore | None = None, **overrides: Any) -> Harness:
    """Build a client on a fake adapter and clock.

    Args:
        token_store: Token store to use, if any.
        **overrides: Configuration settings that replace the defaults.

    Returns:
        The harness.
    """
    config = make_config(**overrides)
    session = requests.Session()
    adapter = FakeAdapter()
    session.mount("https://", adapter)
    clock = FakeClock()
    transport = Transport(
        config, session=session, token_store=token_store, clock=clock.time, sleep=clock.sleep
    )
    return Harness(TruvaliClient(config, transport=transport), adapter, clock, transport)


def login_document(token: str = "token-0001", expires_in: int = 43200) -> dict[str, Any]:
    """Build a successful login response.

    Args:
        token: The access token value.
        expires_in: Lifetime in seconds.

    Returns:
        The response document.
    """
    return {
        "status": True,
        "message": "Login successful.",
        "data": {"access_token": token, "expires_in": expires_in},
    }


def envelope(
    data: Mapping[str, Any], *, request_id: str | None = "req-0001", message: str = "OK."
) -> dict[str, Any]:
    """Build a successful response envelope.

    Args:
        data: The ``data`` object.
        request_id: The request id, or ``None`` to omit it.
        message: The envelope message.

    Returns:
        The response document.
    """
    document: dict[str, Any] = {"status": True, "message": message, "data": dict(data)}
    if request_id is not None:
        document["request_id"] = request_id
    return document


def error_document(
    message: str, code: str | None = None, *, request_id: str | None = "req-error"
) -> dict[str, Any]:
    """Build an error response envelope shaped like the server's.

    Args:
        message: The error message.
        code: The ``data.error`` code, if any.
        request_id: The request id, or ``None`` to omit it.

    Returns:
        The response document.
    """
    document: dict[str, Any] = {"status": False, "message": message, "data": {}}
    if code is not None:
        document["data"] = {"error": code}
    if request_id is not None:
        document["request_id"] = request_id
    return document


def customer_result(ref: str, **overrides: Any) -> dict[str, Any]:
    """Build a customer result object.

    Args:
        ref: The customer reference.
        **overrides: Keys that replace the defaults.

    Returns:
        The result object.
    """
    result: dict[str, Any] = {
        "customer_ref": ref,
        "id": f"id-{ref}",
        "score": "25.00",
        "decision": "ACCEPT",
        "action": "created",
        "changed": True,
        "warnings": [],
        "sanction_score": 0,
        "sanction_topic": [],
        "sanction_status": "not_configured",
        "sanction_screened_at": None,
        "subjects": [],
        "subjects_sanction_status": None,
        "subjects_unscreened_count": None,
        "ip_address_score": "12.50",
        "email_risk_score": "0",
    }
    result.update(overrides)
    return result


def event_result(ref: str, **overrides: Any) -> dict[str, Any]:
    """Build an event result object.

    Args:
        ref: The transaction reference.
        **overrides: Keys that replace the defaults.

    Returns:
        The result object.
    """
    result: dict[str, Any] = {
        "transaction_ref": ref,
        "id": f"id-{ref}",
        "score": "40.00",
        "decision": "REVIEW",
        "action": "created",
        "rule_check": True,
        "changed": True,
        "warnings": [],
        "log_id": f"log-{ref}",
    }
    result.update(overrides)
    return result
