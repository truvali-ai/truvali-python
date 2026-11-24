"""Verification and parsing of the webhooks (callbacks) Truvali sends.

Every delivery is signed: ``X-Truvali-Signature`` is ``sha256=`` followed by the
lower-case hex HMAC-SHA256 of ``"<X-Truvali-Timestamp>." + raw body``, keyed with
the UTF-8 bytes of the endpoint secret. During a secret rotation the same value
made with the previous secret arrives in ``X-Truvali-Signature-Previous``. The
deprecated ``AUTHORIZATION`` header, which carries the raw secret, is never read.

Typical receiver::

    try:
        event = construct_event(request_body_bytes, request_headers, [secret])
    except WebhookVerificationError:
        return 401
    if already_stored(event.event_id):
        return 200
    store(event)
    return 200

Always pass the raw body bytes as received: a parsed and re-serialised body no
longer matches the signature. De-duplicate on ``event_id`` for at least 48 hours,
because retries reuse it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypeAlias

from truvali import _parsing
from truvali.errors import (
    ConfigurationError,
    InvalidArgumentError,
    WebhookEventIdMismatchError,
    WebhookMalformedBodyError,
    WebhookMissingHeaderError,
    WebhookSignatureMismatchError,
    WebhookTimestampError,
)

SIGNATURE_HEADER = "X-Truvali-Signature"
PREVIOUS_SIGNATURE_HEADER = "X-Truvali-Signature-Previous"
TIMESTAMP_HEADER = "X-Truvali-Timestamp"
EVENT_ID_HEADER = "X-Truvali-Event-Id"
REQUEST_ID_HEADER = "X-Request-Id"
SIGNATURE_PREFIX = "sha256="
DEFAULT_TOLERANCE = 300

_UNIX_SECONDS = re.compile(r"[0-9]{1,12}")
_HEX_SHA256 = re.compile(r"[0-9a-fA-F]{64}")
_SIGNATURE_HEADERS = (SIGNATURE_HEADER.lower(), PREVIOUS_SIGNATURE_HEADER.lower())
_UNPARSED = object()

Secrets: TypeAlias = str | bytes | Iterable[str | bytes]


def compute_signature(secret: str | bytes, timestamp: int | str, body: bytes | str) -> str:
    """Compute the signature Truvali puts in ``X-Truvali-Signature``.

    Args:
        secret: The endpoint secret; a ``str`` is encoded as UTF-8.
        timestamp: The value of ``X-Truvali-Timestamp`` (Unix seconds).
        body: The raw request body; a ``str`` is encoded as UTF-8.

    Returns:
        The lower-case hex digest. The header value is :data:`SIGNATURE_PREFIX`
        followed by this digest.
    """
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    mac = hmac.new(key, str(timestamp).encode("ascii") + b".", hashlib.sha256)
    mac.update(body.encode("utf-8") if isinstance(body, str) else body)
    return mac.hexdigest()


def _header_lookup(headers: Mapping[str, str]) -> dict[str, str]:
    """Index the headers by lower-case name.

    Args:
        headers: The request headers in any letter case.

    Returns:
        A dict from lower-case header name to the stripped value.
    """
    return {str(name).lower(): str(value).strip() for name, value in headers.items()}


def _secret_keys(secrets: Secrets) -> list[bytes]:
    """Normalise the accepted secrets to non-empty byte strings.

    Args:
        secrets: One secret or several (current and previous during a rotation).

    Returns:
        The secrets as UTF-8 bytes, empty ones removed.

    Raises:
        ConfigurationError: If no non-empty secret is given.
    """
    items: Iterable[str | bytes] = (secrets,) if isinstance(secrets, (str, bytes)) else secrets
    keys = [item.encode("utf-8") if isinstance(item, str) else item for item in items if item]
    if not keys:
        raise ConfigurationError("At least one non-empty webhook secret is required.")
    return keys


def _sent_signatures(lookup: Mapping[str, str]) -> tuple[bool, list[str]]:
    """Collect the well-formed signatures from both signature headers.

    Args:
        lookup: Headers indexed by lower-case name.

    Returns:
        Whether any signature header is present, and the lower-case hex digests of
        the well-formed ``sha256=<hex>`` values (a header may list several,
        separated by commas).
    """
    present = False
    found: list[str] = []
    for name in _SIGNATURE_HEADERS:
        value = lookup.get(name)
        if not value:
            continue
        present = True
        for part in value.split(","):
            candidate = part.strip()
            if not candidate.startswith(SIGNATURE_PREFIX):
                continue
            digest = candidate[len(SIGNATURE_PREFIX) :]
            if _HEX_SHA256.fullmatch(digest):
                found.append(digest.lower())
    return present, found


def _verify(
    body: bytes,
    headers: Mapping[str, str],
    secrets: Secrets,
    tolerance: int,
    now: float | None,
) -> tuple[str, object]:
    """Verify a delivery and return its event id and the decoded body.

    Args:
        body: The raw request body.
        headers: The request headers.
        secrets: The accepted secrets.
        tolerance: Largest accepted distance in seconds between the timestamp and
            the current time.
        now: Current Unix time; ``time.time()`` when ``None``.

    Returns:
        The event id and the decoded JSON body, or a sentinel when the body is not
        JSON.

    Raises:
        ConfigurationError: If no non-empty secret is given.
        InvalidArgumentError: If the tolerance is not an integer >= 0.
        WebhookMissingHeaderError: If the timestamp or signature header is missing.
        WebhookTimestampError: If the timestamp is unreadable or out of tolerance.
        WebhookSignatureMismatchError: If no signature matches any secret.
        WebhookEventIdMismatchError: If the event id header differs from the body.
        WebhookMalformedBodyError: If neither the header nor the body has an event id.
    """
    keys = _secret_keys(secrets)
    if isinstance(tolerance, bool) or not isinstance(tolerance, int) or tolerance < 0:
        raise InvalidArgumentError("tolerance must be an integer number of seconds >= 0.")
    lookup = _header_lookup(headers)

    timestamp = lookup.get(TIMESTAMP_HEADER.lower())
    if not timestamp:
        raise WebhookMissingHeaderError(
            f"The {TIMESTAMP_HEADER} header is missing.", reason="missing_timestamp"
        )
    if not _UNIX_SECONDS.fullmatch(timestamp):
        raise WebhookTimestampError(
            f"The {TIMESTAMP_HEADER} header is not a Unix time in seconds.",
            reason="bad_timestamp",
        )

    present, signatures = _sent_signatures(lookup)
    if not present:
        raise WebhookMissingHeaderError(
            f"The {SIGNATURE_HEADER} header is missing.", reason="missing_signature"
        )

    current = time.time() if now is None else now
    if abs(current - int(timestamp)) > tolerance:
        raise WebhookTimestampError(
            f"The {TIMESTAMP_HEADER} header is more than {tolerance} seconds away "
            "from the current time.",
            reason="timestamp_out_of_tolerance",
        )

    matched = False
    for key in keys:
        expected = compute_signature(key, timestamp, body)
        for signature in signatures:
            matched |= hmac.compare_digest(expected, signature)
    if not matched:
        raise WebhookSignatureMismatchError(
            "No signature matches the configured secrets.", reason="bad_signature"
        )

    try:
        document: object = json.loads(body)
    except (ValueError, TypeError):
        document = _UNPARSED
    body_id = None
    if isinstance(document, dict) and document.get("event_id") not in (None, ""):
        body_id = str(document["event_id"])
    header_id = lookup.get(EVENT_ID_HEADER.lower()) or None
    if header_id and body_id and header_id != body_id:
        raise WebhookEventIdMismatchError(
            f"The {EVENT_ID_HEADER} header does not match event_id in the body.",
            reason="event_id_mismatch",
        )
    event_id = body_id or header_id
    if not event_id:
        raise WebhookMalformedBodyError(
            "The delivery carries no event id.", reason="missing_event_id"
        )
    return event_id, document


def _as_bytes(body: bytes | str) -> bytes:
    """Return the body as bytes, encoding text as UTF-8.

    Args:
        body: The raw body.

    Returns:
        The body bytes.
    """
    return body.encode("utf-8") if isinstance(body, str) else body


def verify_signature(
    body: bytes | str,
    headers: Mapping[str, str],
    secrets: Secrets,
    *,
    tolerance: int = DEFAULT_TOLERANCE,
    now: float | None = None,
) -> str:
    """Verify the signature, timestamp and event id of a webhook delivery.

    The checks run in this order: timestamp present and numeric, a signature header
    present, timestamp within ``tolerance``, a signature in
    ``X-Truvali-Signature`` or ``X-Truvali-Signature-Previous`` matching one of the
    secrets (constant-time comparison, every combination is compared), and finally
    ``X-Truvali-Event-Id`` equal to ``event_id`` in the body. Header names are
    matched case-insensitively.

    Args:
        body: The raw request body exactly as received.
        headers: The request headers.
        secrets: The endpoint secret, or several during a rotation.
        tolerance: Largest accepted distance in seconds between the timestamp and
            the current time.
        now: Current Unix time, for tests; ``time.time()`` when ``None``.

    Returns:
        The event id to de-duplicate on.

    Raises:
        ConfigurationError: If no non-empty secret is given.
        InvalidArgumentError: If the tolerance is not an integer >= 0.
        WebhookVerificationError: A subclass describing why the delivery must be
            rejected.
    """
    event_id, _ = _verify(_as_bytes(body), headers, secrets, tolerance, now)
    return event_id


def _context_value(data: Mapping[str, Any], section: str, key: str) -> str | None:
    """Find a reference directly in ``data`` or inside a rule execution log.

    Args:
        data: The ``data`` object of an envelope.
        section: ``"customer"`` or ``"transaction"`` in ``data_context``.
        key: The key to read, for example ``"customer_ref"``.

    Returns:
        The first string found, or ``None``.
    """
    direct = data.get(key)
    if isinstance(direct, str):
        return direct
    for container in (data, data.get("execution_log")):
        if not isinstance(container, Mapping):
            continue
        context = container.get("data_context")
        part = context.get(section) if isinstance(context, Mapping) else None
        value = part.get(key) if isinstance(part, Mapping) else None
        if isinstance(value, str):
            return value
    return None


@dataclass(frozen=True, slots=True, kw_only=True)
class WebhookEnvelope:
    """The envelope every webhook delivery and event feed entry shares.

    Attributes:
        event_id: Id of this delivery; retries reuse it. De-duplicate on it.
        topic: The first topic of the delivery.
        topics: Every topic merged into this delivery.
        action: ``None``, ``"suspend"`` or ``"unsuspend"``.
        request_id: The API call that caused the event, when there is one.
        data_source: Id of the Source Service the event belongs to, or ``None``.
        occurred_at: When the event was created; use it to order events.
        attempt: Delivery attempt number; ``None`` in event feed entries.
        data: The topic-specific payload.
    """

    event_id: str
    topic: str
    topics: tuple[str, ...]
    action: str | None
    request_id: str | None
    data_source: str | None
    occurred_at: datetime | None
    attempt: int | None
    data: Mapping[str, Any]

    @staticmethod
    def _envelope_values(envelope: Mapping[str, Any]) -> dict[str, Any]:
        """Read the envelope keys of a decoded document.

        Subclasses that extend the envelope, such as event feed entries, reuse it.

        Args:
            envelope: The decoded JSON object.

        Returns:
            Keyword arguments for the envelope fields.

        Raises:
            ShapeError: If a key is missing or has the wrong type.
        """
        topic = _parsing.required_str(envelope, "topic")
        return {
            "event_id": _parsing.required_str(envelope, "event_id"),
            "topic": topic,
            "topics": _parsing.str_tuple(envelope, "topics") or (topic,),
            "action": _parsing.optional_str(envelope, "action"),
            "request_id": _parsing.optional_str(envelope, "request_id"),
            "data_source": _parsing.optional_str(envelope, "data_source"),
            "occurred_at": _parsing.parse_datetime(envelope.get("occurred_at"), "occurred_at"),
            "attempt": _parsing.optional_int(envelope, "attempt"),
            "data": _parsing.mapping(envelope.get("data"), "data"),
        }

    @property
    def customer_ref(self) -> str | None:
        """The customer reference the event is about, when the payload names one.

        Looks at ``data.customer_ref`` first, then at the customer of a rule
        execution log (``data.data_context`` or ``data.execution_log.data_context``).

        Returns:
            The reference, or ``None``.
        """
        return _context_value(self.data, "customer", "customer_ref")

    @property
    def transaction_ref(self) -> str | None:
        """The transaction reference the event is about, when the payload names one.

        Looks at ``data.transaction_ref`` first, then at the transaction of a rule
        execution log.

        Returns:
            The reference, or ``None``.
        """
        return _context_value(self.data, "transaction", "transaction_ref")

    @property
    def callback_topics(self) -> tuple[str, ...]:
        """The topics of the rule callback actions that fired, from ``data.callback_data``.

        Returns:
            The topic strings in payload order; empty for non-rule topics.
        """
        entries = self.data.get("callback_data")
        if not isinstance(entries, list):
            return ()
        return tuple(
            entry["data"]
            for entry in entries
            if isinstance(entry, Mapping) and isinstance(entry.get("data"), str)
        )


def _envelope_from(document: object) -> WebhookEnvelope:
    """Build an envelope from a decoded body.

    Args:
        document: The decoded JSON body.

    Returns:
        The envelope.

    Raises:
        WebhookMalformedBodyError: If the body is not a valid envelope.
    """
    if not isinstance(document, Mapping):
        raise WebhookMalformedBodyError("The body is not a JSON object.", reason="malformed_body")
    try:
        return WebhookEnvelope(**WebhookEnvelope._envelope_values(document))
    except _parsing.ShapeError as exc:
        raise WebhookMalformedBodyError(
            f"The body is not a valid webhook envelope: {exc}", reason="malformed_body"
        ) from exc


def parse_envelope(body: bytes | str | Mapping[str, Any]) -> WebhookEnvelope:
    """Parse a webhook body without verifying it.

    Use :func:`construct_event` for deliveries received over the network; this
    function is for bodies that were verified and stored earlier.

    Args:
        body: The raw body or an already decoded JSON object.

    Returns:
        The envelope.

    Raises:
        WebhookMalformedBodyError: If the body is not JSON or not a valid envelope.
    """
    if isinstance(body, Mapping):
        return _envelope_from(body)
    try:
        document = json.loads(body)
    except (ValueError, TypeError) as exc:
        raise WebhookMalformedBodyError(
            "The body is not valid JSON.", reason="malformed_body"
        ) from exc
    return _envelope_from(document)


def construct_event(
    body: bytes | str,
    headers: Mapping[str, str],
    secrets: Secrets,
    *,
    tolerance: int = DEFAULT_TOLERANCE,
    now: float | None = None,
) -> WebhookEnvelope:
    """Verify a webhook delivery and parse its envelope.

    Equivalent to :func:`verify_signature` followed by :func:`parse_envelope`, but
    decodes the body only once.

    Args:
        body: The raw request body exactly as received.
        headers: The request headers.
        secrets: The endpoint secret, or several during a rotation.
        tolerance: Largest accepted distance in seconds between the timestamp and
            the current time.
        now: Current Unix time, for tests; ``time.time()`` when ``None``.

    Returns:
        The verified envelope.

    Raises:
        ConfigurationError: If no non-empty secret is given.
        InvalidArgumentError: If the tolerance is not an integer >= 0.
        WebhookVerificationError: A subclass describing why the delivery must be
            rejected.
    """
    _, document = _verify(_as_bytes(body), headers, secrets, tolerance, now)
    return _envelope_from(document)
