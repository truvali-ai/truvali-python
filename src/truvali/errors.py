"""Exception hierarchy and machine-readable error codes of the Truvali SDK.

Every exception the SDK raises on purpose derives from :class:`TruvaliError`.
Errors that come from an HTTP response carry the HTTP status, the server error
code from ``data.error`` and the ``request_id`` to quote to support. Branch on
:attr:`TruvaliError.code` or on the exception class, never on the message text.
"""

from __future__ import annotations

from truvali._enum import TextEnum


class ErrorCode(TextEnum):
    """Machine-readable codes the Truvali API returns in ``data.error``.

    Members compare equal to the plain strings, so ``error.code == "token_expired"``
    and ``error.code == ErrorCode.TOKEN_EXPIRED`` are equivalent.
    """

    TOKEN_EXPIRED = "token_expired"
    TOKEN_INVALID = "token_invalid"
    SESSION_INVALID = "session_invalid"
    INVALID_CREDENTIALS = "invalid_credentials"
    IP_MISMATCH = "ip_mismatch"
    IP_NOT_ALLOWED = "ip_not_allowed"
    CREDENTIAL_INACTIVE = "credential_inactive"
    BLOCKED = "blocked"
    RATE_LIMITED = "rate_limited"
    INTERNAL_ERROR = "internal_error"
    INVALID_CURSOR = "invalid_cursor"
    INVALID_SINCE = "invalid_since"
    INVALID_LIMIT = "invalid_limit"
    INVALID_ENDPOINT = "invalid_endpoint"

    @classmethod
    def parse(cls, value: object) -> str | None:
        """Turn a raw ``data.error`` value into an :class:`ErrorCode` when it is known.

        Args:
            value: The raw value found in the response document.

        Returns:
            The matching member, the raw string for a code this SDK version does not
            know yet, or ``None`` when the value is missing or not a non-empty string.
        """
        if not isinstance(value, str) or not value:
            return None
        try:
            return cls(value)
        except ValueError:
            return value


class TruvaliError(Exception):
    """Base class of every exception raised by the Truvali SDK.

    Attributes:
        message: Human-readable description. For API errors this is the server's
            ``message`` text, which may change between releases.
        status_code: HTTP status of the response, or ``None`` when no response was
            involved.
        code: Machine-readable code: the server's ``data.error`` (an
            :class:`ErrorCode` when known) or, for webhook verification, the reason.
        request_id: The server's ``request_id`` for the call, when it returned one.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        request_id: str | None = None,
    ) -> None:
        """Initialise the error.

        Args:
            message: Human-readable description of the problem.
            status_code: HTTP status of the response, if any.
            code: Machine-readable error code, if any.
            request_id: Server request id of the failed call, if any.
        """
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.request_id = request_id

    def __str__(self) -> str:
        """Render the message followed by the status, code and request id that are set.

        Returns:
            A single-line description suitable for logs.
        """
        details = [
            f"{name}={value}"
            for name, value in (
                ("status", self.status_code),
                ("code", self.code),
                ("request_id", self.request_id),
            )
            if value is not None
        ]
        if not details:
            return self.message
        return f"{self.message} ({', '.join(details)})"


class ConfigurationError(TruvaliError, ValueError):
    """The client configuration or a webhook secret list is invalid or incomplete.

    Also raised when a request is attempted on a client that has been closed.
    """


class InvalidArgumentError(TruvaliError, ValueError):
    """An argument of a public method is malformed or conflicts with another one.

    Raised before anything is sent. It also subclasses ``ValueError``, so both
    ``except TruvaliError`` and ``except ValueError`` catch it.
    """


class NetworkError(TruvaliError):
    """The request failed before a complete HTTP response arrived.

    Attributes:
        may_have_reached_server: ``True`` when the server may have received and
            processed the request (for example a read timeout), ``False`` when the
            connection was never established. Upserts are keyed by their refs, so
            resending after either case is safe.
    """

    def __init__(self, message: str, *, may_have_reached_server: bool) -> None:
        """Initialise the error.

        Args:
            message: Human-readable description of the failure.
            may_have_reached_server: Whether the server may have received the request.
        """
        super().__init__(message)
        self.may_have_reached_server = may_have_reached_server


class AuthenticationError(TruvaliError):
    """The credentials or the access token were rejected (HTTP 401).

    Raised for ``invalid_credentials`` on login, and for token errors that persist
    after the SDK has logged in again once.
    """


class PermissionDeniedError(TruvaliError):
    """The call was refused for this credential or IP address (HTTP 403).

    ``ip_not_allowed``, ``credential_inactive`` and ``blocked`` need an operator to
    act; logging in again does not help. ``ip_mismatch`` is retried once with a new
    token and only raised when it persists.
    """


class RateLimitError(TruvaliError):
    """The rate limit of the Source Service was exceeded (HTTP 429).

    Attributes:
        retry_after: Seconds to wait before the next attempt, from the
            ``Retry-After`` header or ``data.retry_after``; ``None`` when absent.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        request_id: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Initialise the error.

        Args:
            message: Human-readable description of the problem.
            status_code: HTTP status of the response.
            code: Machine-readable error code.
            request_id: Server request id of the refused call.
            retry_after: Seconds to wait before retrying, if known.
        """
        super().__init__(message, status_code=status_code, code=code, request_id=request_id)
        self.retry_after = retry_after


class PayloadTooLargeError(TruvaliError):
    """The request carried too many records or too many bytes.

    Raised for an HTTP 413 (JSON or HTML) and, before anything is sent, when a batch
    exceeds the configured record limit or the 1 MiB body limit.
    """


class BadRequestError(TruvaliError):
    """The server rejected the request as a whole (HTTP 400)."""


class ServerError(TruvaliError):
    """The server failed to process the request (HTTP 5xx or ``internal_error``)."""


class UnexpectedResponseError(TruvaliError):
    """The response did not have the documented shape.

    Raised for bodies that are not JSON, envelopes without the expected keys,
    unexpected HTTP statuses (for example redirects) and result lists whose length
    does not match the number of records sent.
    """


class RecordValidationError(TruvaliError, ValueError):
    """A record failed client-side validation and was not sent.

    Attributes:
        field: Name of the offending field, or ``None`` when the problem concerns
            the batch as a whole.
    """

    def __init__(self, message: str, *, field: str | None = None) -> None:
        """Initialise the error.

        Args:
            message: Human-readable description of the problem.
            field: Name of the offending field, if any.
        """
        super().__init__(message)
        self.field = field


class WebhookVerificationError(TruvaliError, ValueError):
    """A webhook delivery could not be verified and must be rejected.

    The machine-readable reason is available as :attr:`reason` (also stored in
    :attr:`TruvaliError.code`). Answer such deliveries with HTTP 401 or 400.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        """Initialise the error.

        Args:
            message: Human-readable description of the problem.
            reason: Machine-readable reason, for example ``bad_signature``.
        """
        super().__init__(message, code=reason)

    @property
    def reason(self) -> str:
        """Machine-readable reason of the failure.

        Returns:
            The reason code, for example ``timestamp_out_of_tolerance``.
        """
        return self.code or ""


class WebhookMissingHeaderError(WebhookVerificationError):
    """A required header is missing.

    Reasons: ``missing_timestamp`` and ``missing_signature``.
    """


class WebhookTimestampError(WebhookVerificationError):
    """The timestamp header is unreadable or outside the tolerance window.

    Reasons: ``bad_timestamp`` and ``timestamp_out_of_tolerance``.
    """


class WebhookSignatureMismatchError(WebhookVerificationError):
    """No signature header matches any of the given secrets (``bad_signature``)."""


class WebhookEventIdMismatchError(WebhookVerificationError):
    """The event id header differs from ``event_id`` in the body.

    Reason: ``event_id_mismatch``.
    """


class WebhookMalformedBodyError(WebhookVerificationError):
    """The body is not a valid webhook envelope.

    Reasons: ``missing_event_id`` and ``malformed_body``.
    """
