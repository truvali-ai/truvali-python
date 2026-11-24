"""Client configuration: endpoint, credentials, timeouts and retry policy."""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from truvali.errors import ConfigurationError

ENV_BASE_URL = "TRUVALI_BASE_URL"
ENV_API_KEY = "TRUVALI_API_KEY"
ENV_API_SECRET = "TRUVALI_API_SECRET"

SERVER_MAX_RECORDS = 1000

_MAX_BACKOFF_EXPONENT = 32
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

logger = logging.getLogger("truvali")


def _is_number(value: object) -> bool:
    """Tell whether a value is a finite real number that is not a bool.

    Args:
        value: The value to inspect.

    Returns:
        ``True`` for finite ``int`` and ``float`` values other than ``bool``.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_count(value: object) -> bool:
    """Tell whether a value is an ``int`` that is not a bool.

    Args:
        value: The value to inspect.

    Returns:
        ``True`` for ``int`` values other than ``bool``.
    """
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How the transport retries failed attempts.

    Connection errors, timeouts, HTTP 5xx and HTTP 429 are retried; every other 4xx
    is final. The delay before retry ``n`` (0-based) is
    ``min(backoff_max, backoff_initial * 2 ** n)``. A 429 or 5xx response with a
    ``Retry-After`` value waits ``max(retry_after, delay)``; when ``retry_after``
    exceeds ``backoff_max`` the SDK does not wait at all and raises the error so the
    caller can decide. Retrying upserts is safe because records are keyed by their
    refs.

    Attributes:
        max_retries: Retries after the first attempt; ``0`` disables retrying.
        backoff_initial: Delay in seconds before the first retry.
        backoff_max: Upper bound in seconds for any single delay.
        retry_on_timeout: Whether failures that may have reached the server (read
            timeouts, dropped connections) are retried. Failures to connect are
            always retried.
    """

    max_retries: int = 2
    backoff_initial: float = 0.5
    backoff_max: float = 8.0
    retry_on_timeout: bool = True

    def __post_init__(self) -> None:
        """Validate the policy.

        Raises:
            ConfigurationError: If a value is out of range or of the wrong type.
        """
        if not _is_count(self.max_retries) or self.max_retries < 0:
            raise ConfigurationError("max_retries must be an integer >= 0.")
        if not _is_number(self.backoff_initial) or self.backoff_initial < 0:
            raise ConfigurationError("backoff_initial must be a finite number >= 0.")
        if not _is_number(self.backoff_max) or self.backoff_max < self.backoff_initial:
            raise ConfigurationError("backoff_max must be a finite number >= backoff_initial.")
        if not isinstance(self.retry_on_timeout, bool):
            raise ConfigurationError("retry_on_timeout must be a bool.")

    def backoff(self, retry: int) -> float:
        """Compute the delay before a retry.

        Args:
            retry: Zero-based number of the retry about to happen.

        Returns:
            The delay in seconds, never above ``backoff_max``.
        """
        exponent = min(retry, _MAX_BACKOFF_EXPONENT)
        return float(min(self.backoff_max, self.backoff_initial * 2.0**exponent))


def _check_text(name: str, value: object) -> str:
    """Validate a required configuration string.

    Args:
        name: Name of the setting, used in the error message.
        value: The value to validate.

    Returns:
        The value, unchanged.

    Raises:
        ConfigurationError: If the value is not a non-empty string without
            surrounding whitespace.
    """
    if not isinstance(value, str) or not value:
        raise ConfigurationError(f"{name} must be a non-empty string.")
    if value != value.strip():
        raise ConfigurationError(f"{name} must not have leading or trailing whitespace.")
    return value


def _normalise_base_url(value: object) -> str:
    """Validate the base URL and strip trailing slashes.

    Args:
        value: The configured base URL.

    Returns:
        The base URL without trailing slashes.

    Raises:
        ConfigurationError: If the URL is not an absolute http or https URL without
            query string or fragment.
    """
    url = _check_text("base_url", value).rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ConfigurationError("base_url must be an absolute http:// or https:// URL.")
    if parts.query or parts.fragment:
        raise ConfigurationError("base_url must not contain a query string or a fragment.")
    return url


def _warn_if_plain_http(url: str) -> None:
    """Log a warning when a non-local host is reached over plain http.

    Plain http stays allowed for local sandboxes, but elsewhere the credentials,
    the access token and the data would travel unencrypted.

    Args:
        url: The validated base URL.
    """
    parts = urlsplit(url)
    if parts.scheme == "http" and parts.hostname not in _LOCAL_HOSTS:
        logger.warning(
            "base_url uses plain http:// for a host that is not local; credentials, "
            "tokens and records travel unencrypted. Use https:// outside local sandboxes."
        )


@dataclass(frozen=True, slots=True)
class TruvaliConfig:
    """Settings of a :class:`truvali.TruvaliClient`.

    The secret never appears in ``repr()``. Use :meth:`from_env` to read the
    endpoint and credentials from the environment and ``dataclasses.replace`` to
    derive a copy with other settings.

    Attributes:
        base_url: Root URL of the Truvali installation, for example
            ``https://truvali.example.com``. Trailing slashes are removed. Plain
            ``http://`` is accepted, but logs a warning on the ``truvali`` logger
            unless the host is ``localhost``, ``127.0.0.1`` or ``::1``.
        api_key: The Source Credential key.
        api_secret: The Source Credential secret.
        connect_timeout: Seconds to wait for a connection.
        read_timeout: Seconds to wait between bytes of the response. Every client
            method that sends a request accepts ``timeout`` to override it for
            one call.
        max_batch_size: Most records accepted in one upsert call, from 1 to 1000
            (the server maximum). The documentation recommends 100 to 200.
        user_agent_suffix: Optional product token appended to the User-Agent, for
            example ``"payments-core/4.2"``.
        retry: Retry policy of the transport.
    """

    base_url: str
    api_key: str
    api_secret: str = field(repr=False)
    connect_timeout: float = 5.0
    read_timeout: float = 30.0
    max_batch_size: int = 200
    user_agent_suffix: str | None = None
    retry: RetryPolicy = field(default_factory=RetryPolicy)

    def __post_init__(self) -> None:
        """Validate and normalise the settings.

        Raises:
            ConfigurationError: If a setting is missing, malformed or out of range.
        """
        object.__setattr__(self, "base_url", _normalise_base_url(self.base_url))
        _check_text("api_key", self.api_key)
        _check_text("api_secret", self.api_secret)
        for name, timeout in (
            ("connect_timeout", self.connect_timeout),
            ("read_timeout", self.read_timeout),
        ):
            if not _is_number(timeout) or timeout <= 0:
                raise ConfigurationError(f"{name} must be a finite number of seconds > 0.")
        if not _is_count(self.max_batch_size) or not (
            1 <= self.max_batch_size <= SERVER_MAX_RECORDS
        ):
            raise ConfigurationError(
                f"max_batch_size must be an integer from 1 to {SERVER_MAX_RECORDS}."
            )
        suffix = self.user_agent_suffix
        if suffix is not None:
            _check_text("user_agent_suffix", suffix)
            if not (suffix.isascii() and suffix.isprintable()):
                raise ConfigurationError("user_agent_suffix must be printable ASCII text.")
        if not isinstance(self.retry, RetryPolicy):
            raise ConfigurationError("retry must be a RetryPolicy.")
        _warn_if_plain_http(self.base_url)

    @property
    def timeout(self) -> tuple[float, float]:
        """Connect and read timeouts in the form ``requests`` expects.

        Returns:
            The ``(connect_timeout, read_timeout)`` pair.
        """
        return (self.connect_timeout, self.read_timeout)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> TruvaliConfig:
        """Build a configuration from environment variables.

        Reads ``TRUVALI_BASE_URL``, ``TRUVALI_API_KEY`` and ``TRUVALI_API_SECRET``.
        Every other setting keeps its default.

        Args:
            environ: Mapping to read instead of ``os.environ``.

        Returns:
            The validated configuration.

        Raises:
            ConfigurationError: If a variable is missing or empty, or a value is
                invalid. Values are never included in the message.
        """
        source = os.environ if environ is None else environ
        names = (ENV_BASE_URL, ENV_API_KEY, ENV_API_SECRET)
        missing = [name for name in names if not source.get(name)]
        if missing:
            raise ConfigurationError(f"Missing environment variable(s): {', '.join(missing)}.")
        return cls(
            base_url=source[ENV_BASE_URL],
            api_key=source[ENV_API_KEY],
            api_secret=source[ENV_API_SECRET],
        )
