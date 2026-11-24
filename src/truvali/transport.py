"""HTTP transport: authentication, token caching, retries and error mapping.

The transport sends every request through one ``requests.Session``. It logs in
lazily, caches the access token, renews it shortly before it expires and, when a
protected call answers ``401 token_expired``, ``token_invalid`` or
``session_invalid`` or ``403 ip_mismatch``, logs in again and repeats the call
exactly once. Connection errors, timeouts, HTTP 5xx and HTTP 429 are retried
according to the :class:`~truvali.RetryPolicy`; every other failure is mapped to
the exception hierarchy using ``data.error`` first and the HTTP status second.

The library logs to the ``truvali`` logger: method, path, status, request id and
timing only. Bodies, query strings, tokens, secrets and personal data are never
logged.
"""

from __future__ import annotations

import json
import logging
import math
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import requests
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError

from truvali._version import __version__
from truvali.config import TruvaliConfig
from truvali.errors import (
    AuthenticationError,
    BadRequestError,
    ConfigurationError,
    ErrorCode,
    InvalidArgumentError,
    NetworkError,
    PayloadTooLargeError,
    PermissionDeniedError,
    RateLimitError,
    ServerError,
    TruvaliError,
    UnexpectedResponseError,
)

logger = logging.getLogger("truvali")

TOKEN_PATH = "/api/merchant/auth/token/"
RENEW_MARGIN_MAX = 300.0
RENEW_MARGIN_RATIO = 0.1

_RELOGIN_401 = frozenset(
    {ErrorCode.TOKEN_EXPIRED, ErrorCode.TOKEN_INVALID, ErrorCode.SESSION_INVALID, None}
)
_ERROR_BY_CODE: Mapping[str, type[TruvaliError]] = {
    ErrorCode.TOKEN_EXPIRED: AuthenticationError,
    ErrorCode.TOKEN_INVALID: AuthenticationError,
    ErrorCode.SESSION_INVALID: AuthenticationError,
    ErrorCode.INVALID_CREDENTIALS: AuthenticationError,
    ErrorCode.IP_MISMATCH: PermissionDeniedError,
    ErrorCode.IP_NOT_ALLOWED: PermissionDeniedError,
    ErrorCode.CREDENTIAL_INACTIVE: PermissionDeniedError,
    ErrorCode.BLOCKED: PermissionDeniedError,
    ErrorCode.RATE_LIMITED: RateLimitError,
    ErrorCode.INTERNAL_ERROR: ServerError,
    ErrorCode.INVALID_CURSOR: BadRequestError,
    ErrorCode.INVALID_SINCE: BadRequestError,
    ErrorCode.INVALID_LIMIT: BadRequestError,
    ErrorCode.INVALID_ENDPOINT: BadRequestError,
}
_ERROR_BY_STATUS: Mapping[int, type[TruvaliError]] = {
    400: BadRequestError,
    401: AuthenticationError,
    403: PermissionDeniedError,
    413: PayloadTooLargeError,
    429: RateLimitError,
}
_DEFAULT_MESSAGES: Mapping[int, str] = {
    413: "The request body is too large; keep upsert bodies under 1 MiB.",
    429: "Too many requests; retry after the indicated delay.",
}
_CONNECT_FAILURES = (NewConnectionError, ConnectTimeoutError)


def validate_timeout(value: object) -> float | None:
    """Validate a per-call read timeout override.

    Args:
        value: Seconds to wait between bytes of the response, or ``None`` to keep
            the configured ``read_timeout``.

    Returns:
        The timeout as a float, or ``None``.

    Raises:
        InvalidArgumentError: If the value is not a finite number greater than 0.
    """
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise InvalidArgumentError("timeout must be a finite number of seconds > 0, or None.")
    return float(value)


def user_agent(suffix: str | None = None) -> str:
    """Build the User-Agent header of the SDK.

    Args:
        suffix: Optional product token appended at the end.

    Returns:
        ``truvali-python/<version> python/<x.y> requests/<version>`` plus the suffix.
    """
    version = sys.version_info
    agent = (
        f"truvali-python/{__version__} python/{version.major}.{version.minor} "
        f"requests/{requests.__version__}"
    )
    return f"{agent} {suffix}" if suffix else agent


@dataclass(frozen=True, slots=True)
class AccessToken:
    """A cached access token. The token value never appears in ``repr()``.

    Attributes:
        value: The bearer token.
        issued_at: Unix time just before the login request was sent.
        expires_at: Unix time at which the server stops accepting the token.
    """

    value: str = field(repr=False)
    issued_at: float
    expires_at: float

    @property
    def renew_at(self) -> float:
        """Unix time after which the token is renewed before use.

        The safety margin is a tenth of the lifetime, at most five minutes.

        Returns:
            The renewal time.
        """
        lifetime = self.expires_at - self.issued_at
        return self.expires_at - min(RENEW_MARGIN_MAX, lifetime * RENEW_MARGIN_RATIO)


class TokenStore(Protocol):
    """Where the transport keeps its access token.

    A token is bound to the IP address that logged in. A store may outlive the
    process (for example a cache shared by the workers of one host), but it must
    never be shared across hosts or egress IP addresses: a token used from another
    IP answers ``403 ip_mismatch``. Implementations need not be thread-safe; the
    token manager serialises access.
    """

    def get(self) -> AccessToken | None:
        """Return the stored token.

        Returns:
            The token, or ``None`` when nothing is stored.
        """

    def set(self, token: AccessToken) -> None:
        """Store a token, replacing any previous one.

        Args:
            token: The token to store.
        """

    def clear(self) -> None:
        """Forget the stored token."""


class MemoryTokenStore:
    """Default token store: one token in process memory."""

    __slots__ = ("_token",)

    def __init__(self) -> None:
        """Initialise an empty store."""
        self._token: AccessToken | None = None

    def get(self) -> AccessToken | None:
        """Return the stored token.

        Returns:
            The token, or ``None`` when nothing is stored.
        """
        return self._token

    def set(self, token: AccessToken) -> None:
        """Store a token, replacing any previous one.

        Args:
            token: The token to store.
        """
        self._token = token

    def clear(self) -> None:
        """Forget the stored token."""
        self._token = None


class TokenManager:
    """Thread-safe owner of the access token.

    Logs in lazily on first use, reuses the cached token, renews it once
    :attr:`AccessToken.renew_at` has passed and drops it on demand. Concurrent
    callers wait for a single login instead of logging in once each.
    """

    def __init__(
        self,
        login: Callable[[], AccessToken],
        *,
        store: TokenStore | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Initialise the manager.

        Args:
            login: Performs a login and returns the new token.
            store: Where the token is kept; process memory by default.
            clock: Returns the current Unix time.
        """
        self._login = login
        self._store: TokenStore = store if store is not None else MemoryTokenStore()
        self._clock = clock
        self._lock = threading.Lock()

    def token(self) -> str:
        """Return a token that is valid now, logging in when needed.

        When renewal fails for a transient reason (network, server or rate limit)
        and the cached token has not expired yet, the cached token is returned.

        Returns:
            The bearer token value.

        Raises:
            TruvaliError: If the login fails and no usable token is cached.
        """
        with self._lock:
            current = self._store.get()
            now = self._clock()
            if current is not None and now < current.renew_at:
                return current.value
            try:
                fresh = self._login()
            except (NetworkError, ServerError, RateLimitError):
                if current is not None and now < current.expires_at:
                    logger.warning("Token renewal failed; using the current token until expiry")
                    return current.value
                raise
            self._store.set(fresh)
            return fresh.value

    def invalidate(self, stale: str | None = None) -> None:
        """Drop the cached token.

        Args:
            stale: The token value the caller saw rejected. The cache is only
                cleared when it still holds that value, so a token another thread
                has just renewed survives. ``None`` clears unconditionally.
        """
        with self._lock:
            current = self._store.get()
            if current is not None and (stale is None or current.value == stale):
                self._store.clear()


@dataclass(frozen=True, slots=True)
class _Reply:
    """A decoded HTTP response.

    Attributes:
        status_code: HTTP status.
        document: The JSON object of the body, or ``None`` when the body is not one.
        retry_after: Seconds from ``Retry-After`` or ``data.retry_after``, if any.
    """

    status_code: int
    document: Mapping[str, Any] | None
    retry_after: float | None

    @property
    def data(self) -> Mapping[str, Any]:
        """The ``data`` object of the envelope, or an empty mapping.

        Returns:
            The ``data`` object.
        """
        data = self.document.get("data") if self.document is not None else None
        return data if isinstance(data, Mapping) else {}

    @property
    def code(self) -> str | None:
        """The machine-readable error code, from ``data.error`` or ``error``.

        Returns:
            An :class:`ErrorCode`, an unknown code string, or ``None``.
        """
        code = ErrorCode.parse(self.data.get("error"))
        if code is None and self.document is not None:
            code = ErrorCode.parse(self.document.get("error"))
        return code

    @property
    def request_id(self) -> str | None:
        """The server request id from the envelope.

        Returns:
            The request id, or ``None``.
        """
        value = self.document.get("request_id") if self.document is not None else None
        return value if isinstance(value, str) and value else None

    @property
    def message(self) -> str | None:
        """The envelope message.

        Returns:
            The message, or ``None`` when absent.
        """
        value = self.document.get("message") if self.document is not None else None
        return value if isinstance(value, str) and value else None


def _decode(response: requests.Response) -> _Reply:
    """Decode a raw response without ever raising.

    Args:
        response: The ``requests`` response.

    Returns:
        The decoded reply; non-JSON bodies yield ``document=None``.
    """
    try:
        document = json.loads(response.content) if response.content else None
    except (ValueError, RecursionError):
        document = None
    if not isinstance(document, Mapping):
        document = None
    retry_after: float | None = None
    header = response.headers.get("Retry-After", "").strip()
    if header.isascii() and header.isdigit():
        retry_after = float(header)
    elif document is not None:
        data = document.get("data")
        value = data.get("retry_after") if isinstance(data, Mapping) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            retry_after = float(value)
    return _Reply(response.status_code, document, retry_after)


def _network_error(exc: requests.RequestException, method: str, path: str) -> NetworkError:
    """Map a ``requests`` exception to :class:`NetworkError`.

    Args:
        exc: The exception raised by ``requests``.
        method: HTTP method of the request.
        path: Path of the request.

    Returns:
        The error, telling whether the server may have received the request.
    """
    reached = True
    if isinstance(exc, (requests.ConnectTimeout, requests.exceptions.SSLError)):
        reached = False
    elif isinstance(exc, requests.ConnectionError):
        reason = exc.args[0] if exc.args else None
        cause = getattr(reason, "reason", reason)
        reached = not isinstance(cause, _CONNECT_FAILURES) and not isinstance(
            exc, requests.exceptions.ProxyError
        )
    kind = "timed out" if isinstance(exc, requests.Timeout) else "failed"
    return NetworkError(
        f"{method} {path} {kind}: {type(exc).__name__}", may_have_reached_server=reached
    )


def _error_for(reply: _Reply) -> TruvaliError:
    """Map an unsuccessful reply to the exception hierarchy.

    ``data.error`` decides first; the HTTP status decides when there is no known
    code; any other status yields :class:`UnexpectedResponseError`.

    Args:
        reply: The decoded reply.

    Returns:
        The error to raise.
    """
    status = reply.status_code
    code = reply.code
    error_class = _ERROR_BY_CODE.get(code) if code is not None else None
    if error_class is None:
        error_class = _ERROR_BY_STATUS.get(status)
    if error_class is None:
        error_class = ServerError if status >= 500 else UnexpectedResponseError
    message = reply.message
    if message is None:
        message = _DEFAULT_MESSAGES.get(status)
    if message is None:
        shape = "JSON" if reply.document is not None else "non-JSON"
        message = f"HTTP {status} with a {shape} body."
    if error_class is RateLimitError:
        return RateLimitError(
            message,
            status_code=status,
            code=code,
            request_id=reply.request_id,
            retry_after=reply.retry_after,
        )
    return error_class(message, status_code=status, code=code, request_id=reply.request_id)


@dataclass(frozen=True, slots=True)
class ApiResponse:
    """A successful API response.

    Attributes:
        status_code: HTTP status.
        message: The envelope message.
        data: The ``data`` object of the envelope.
        request_id: The server request id, when the endpoint returns one.
    """

    status_code: int
    message: str
    data: Mapping[str, Any]
    request_id: str | None


class Transport:
    """Sends authenticated requests to one Truvali installation.

    Thread-safe as far as ``requests.Session`` is: share one transport per process
    and egress IP address. After :meth:`close` every request raises
    :class:`~truvali.ConfigurationError`.
    """

    def __init__(
        self,
        config: TruvaliConfig,
        *,
        session: requests.Session | None = None,
        token_store: TokenStore | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Initialise the transport.

        Args:
            config: The client configuration.
            session: Session to send requests with; a new one is created and owned
                when ``None``. A session passed in is not closed by :meth:`close`.
            token_store: Where the access token is kept; process memory by default.
            clock: Returns the current Unix time; used for token expiry.
            sleep: Blocks for the given seconds; used between retries.
        """
        self._config = config
        self._closed = False
        self._owns_session = session is None
        self._session = session if session is not None else requests.Session()
        self._clock = clock
        self._sleep = sleep
        self._headers = {
            "Accept": "application/json",
            "User-Agent": user_agent(config.user_agent_suffix),
        }
        self._tokens = TokenManager(self._login, store=token_store, clock=clock)

    @property
    def config(self) -> TruvaliConfig:
        """The configuration of this transport.

        Returns:
            The configuration.
        """
        return self._config

    @property
    def tokens(self) -> TokenManager:
        """The token manager of this transport.

        Returns:
            The token manager.
        """
        return self._tokens

    @property
    def closed(self) -> bool:
        """Whether :meth:`close` has been called.

        Returns:
            ``True`` once the transport is closed.
        """
        return self._closed

    def close(self) -> None:
        """Refuse further requests and close the session when the transport created it.

        A session passed in by the caller is left open. Calling it again does
        nothing.
        """
        if self._closed:
            return
        self._closed = True
        if self._owns_session:
            self._session.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: bytes | None = None,
        timeout: float | None = None,
    ) -> ApiResponse:
        """Send an authenticated request and return the successful response.

        Args:
            method: HTTP method.
            path: Path below the base URL, starting with ``/``.
            params: Query parameters.
            body: JSON body bytes.
            timeout: Read timeout in seconds for this request, overriding the
                configured ``read_timeout``; a login it triggers keeps the
                configured timeouts.

        Returns:
            The successful response.

        Raises:
            ConfigurationError: If the transport is closed.
            InvalidArgumentError: If ``timeout`` is not a number greater than 0.
            TruvaliError: A subclass describing the failure.
        """
        if self._closed:
            raise ConfigurationError(
                "The client is closed; create a new TruvaliClient to send requests."
            )
        read_timeout = validate_timeout(timeout)
        timeouts = (
            self._config.timeout
            if read_timeout is None
            else (self._config.connect_timeout, read_timeout)
        )
        token = self._tokens.token()
        reply = self._exchange(
            method, path, params=params, body=body, token=token, timeouts=timeouts
        )
        if self._token_rejected(reply):
            logger.info(
                "%s %s rejected the access token (%s); logging in again",
                method,
                path,
                reply.code,
            )
            self._tokens.invalidate(token)
            token = self._tokens.token()
            reply = self._exchange(
                method, path, params=params, body=body, token=token, timeouts=timeouts
            )
        return self._accept(reply)

    def _login(self) -> AccessToken:
        """Exchange the API key and secret for an access token.

        Returns:
            The new token.

        Raises:
            TruvaliError: A subclass describing why the login failed.
        """
        issued_at = self._clock()
        body = json.dumps(
            {"api_key": self._config.api_key, "api_secret": self._config.api_secret}
        ).encode("utf-8")
        response = self._accept(self._exchange("POST", TOKEN_PATH, body=body))
        value = response.data.get("access_token")
        lifetime = response.data.get("expires_in")
        if not isinstance(value, str) or not value:
            raise UnexpectedResponseError("The login response has no access_token.")
        if isinstance(lifetime, bool) or not isinstance(lifetime, int) or lifetime <= 0:
            raise UnexpectedResponseError("The login response has no positive expires_in.")
        logger.info("Logged in; the access token expires in %d s", lifetime)
        return AccessToken(value, issued_at, issued_at + lifetime)

    @staticmethod
    def _token_rejected(reply: _Reply) -> bool:
        """Tell whether a reply asks for a new token.

        Args:
            reply: The decoded reply of a protected call.

        Returns:
            ``True`` for HTTP 401 with a token or session code (or no code) and for
            HTTP 403 ``ip_mismatch``.
        """
        if reply.status_code == 401:
            return reply.code in _RELOGIN_401
        return reply.status_code == 403 and reply.code == ErrorCode.IP_MISMATCH

    def _accept(self, reply: _Reply) -> ApiResponse:
        """Return a successful reply as :class:`ApiResponse` or raise its error.

        Args:
            reply: The decoded reply.

        Returns:
            The successful response.

        Raises:
            TruvaliError: A subclass describing the failure.
        """
        if not 200 <= reply.status_code < 300:
            raise _error_for(reply)
        document = reply.document
        if document is None or document.get("status") is False:
            raise UnexpectedResponseError(
                f"HTTP {reply.status_code} without a successful JSON envelope.",
                status_code=reply.status_code,
                request_id=reply.request_id,
            )
        data = document.get("data")
        if data is not None and not isinstance(data, Mapping):
            raise UnexpectedResponseError(
                "The envelope data is not a JSON object.",
                status_code=reply.status_code,
                request_id=reply.request_id,
            )
        return ApiResponse(reply.status_code, reply.message or "", data or {}, reply.request_id)

    def _retry_delay(self, reply: _Reply, retry: int) -> float | None:
        """Decide whether and how long to wait before retrying a reply.

        Args:
            reply: The decoded reply.
            retry: Zero-based number of the retry that would follow.

        Returns:
            The delay in seconds, or ``None`` when the reply is final.
        """
        policy = self._config.retry
        status = reply.status_code
        if retry >= policy.max_retries or not (status == 429 or status >= 500):
            return None
        delay = policy.backoff(retry)
        if reply.retry_after is None:
            return delay
        if reply.retry_after > policy.backoff_max:
            return None
        return max(reply.retry_after, delay)

    def _exchange(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: bytes | None = None,
        token: str | None = None,
        timeouts: tuple[float, float] | None = None,
    ) -> _Reply:
        """Send one request, retrying transient failures.

        Args:
            method: HTTP method.
            path: Path below the base URL.
            params: Query parameters.
            body: JSON body bytes.
            token: Bearer token, or ``None`` for the login call.
            timeouts: Connect and read timeouts; the configured ones when ``None``.

        Returns:
            The last decoded reply, successful or not.

        Raises:
            NetworkError: If the request fails without a response and may not be
                retried any more.
        """
        url = self._config.base_url + path
        headers = dict(self._headers)
        if body is not None:
            headers["Content-Type"] = "application/json"
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        policy = self._config.retry
        limits = self._config.timeout if timeouts is None else timeouts
        retry = 0
        while True:
            started = time.perf_counter()
            try:
                response = self._session.request(
                    method,
                    url,
                    params=params,
                    data=body,
                    headers=headers,
                    timeout=limits,
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                error = _network_error(exc, method, path)
                retryable = not error.may_have_reached_server or policy.retry_on_timeout
                if retry >= policy.max_retries or not retryable:
                    raise error from exc
                delay = policy.backoff(retry)
                logger.warning(
                    "%s %s failed (%s); retry %d of %d in %.2f s",
                    method,
                    path,
                    type(exc).__name__,
                    retry + 1,
                    policy.max_retries,
                    delay,
                )
                self._sleep(delay)
                retry += 1
                continue
            reply = _decode(response)
            logger.debug(
                "%s %s -> %d in %.1f ms (request_id=%s)",
                method,
                path,
                reply.status_code,
                (time.perf_counter() - started) * 1000.0,
                reply.request_id,
            )
            delay_or_none = self._retry_delay(reply, retry)
            if delay_or_none is None:
                return reply
            logger.warning(
                "%s %s -> %d (request_id=%s); retry %d of %d in %.2f s",
                method,
                path,
                reply.status_code,
                reply.request_id,
                retry + 1,
                policy.max_retries,
                delay_or_none,
            )
            self._sleep(delay_or_none)
            retry += 1
