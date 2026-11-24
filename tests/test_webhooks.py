"""Tests of webhook verification and envelope parsing.

Deliveries are signed by :func:`sign`, an independent re-implementation of the
server's signer: ``sha256=`` plus the hex HMAC-SHA256 of ``"<timestamp>." + body``,
keyed with the UTF-8 bytes of the secret, with the previous secret in a second
header during a rotation.
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
from typing import Any

import pytest

from truvali import (
    ConfigurationError,
    InvalidArgumentError,
    WebhookEventIdMismatchError,
    WebhookMalformedBodyError,
    WebhookMissingHeaderError,
    WebhookSignatureMismatchError,
    WebhookTimestampError,
    WebhookVerificationError,
    compute_signature,
    construct_event,
    parse_envelope,
    verify_signature,
)
from truvali.webhooks import (
    EVENT_ID_HEADER,
    PREVIOUS_SIGNATURE_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    Secrets,
)

SECRET = "whsec-test-current-0000000000000000"
OLD_SECRET = "whsec-test-previous-000000000000000"
NOW = 1_800_000_000


def sign(
    body: bytes,
    secret: str,
    *,
    timestamp: int = NOW,
    previous: str | None = None,
    event_id: str | None = "event-0001",
) -> dict[str, str]:
    """Sign a body the way the server does.

    Args:
        body: The exact body bytes.
        secret: The current secret.
        timestamp: Unix seconds of the attempt.
        previous: The previous secret during a rotation, if any.
        event_id: Value of the event id header, or ``None`` to omit it.

    Returns:
        The signature headers of the delivery.
    """
    stamp = str(timestamp)

    def digest(key: str) -> str:
        """Compute one signature header value.

        Args:
            key: The secret.

        Returns:
            The ``sha256=<hex>`` value.
        """
        mac = hmac.new(key.encode("utf-8"), stamp.encode("ascii") + b"." + body, hashlib.sha256)
        return "sha256=" + mac.hexdigest()

    headers = {TIMESTAMP_HEADER: stamp, SIGNATURE_HEADER: digest(secret)}
    if previous is not None and previous != secret:
        headers[PREVIOUS_SIGNATURE_HEADER] = digest(previous)
    if event_id is not None:
        headers[EVENT_ID_HEADER] = event_id
    headers["AUTHORIZATION"] = secret
    return headers


def body_of(**overrides: Any) -> bytes:
    """Serialise an envelope the way the server does.

    Args:
        **overrides: Keys that replace the defaults.

    Returns:
        The body bytes.
    """
    envelope: dict[str, Any] = {
        "event_id": "event-0001",
        "topic": "risky_customer",
        "topics": ["risky_customer", "kyc_renew"],
        "action": None,
        "request_id": "req-0001",
        "data_source": "source-0001",
        "occurred_at": "2026-10-19T12:09:44.123456+00:00",
        "attempt": 1,
        "data": {
            "id": "log-0001",
            "score": "85.00",
            "data_context": {
                "customer": {"customer_ref": "C-TEST-0001"},
                "transaction": {"transaction_ref": "TX-TEST-0001"},
            },
            "callback_data": [
                {"name": "fake rule", "rule_id": "rule-1", "ruleset": "set-1", "data": "risky"},
                {"name": "fake rule 2", "rule_id": "rule-2", "ruleset": "set-1", "data": "kyc"},
            ],
        },
    }
    envelope.update(overrides)
    return json.dumps(envelope, ensure_ascii=False).encode("utf-8")


def test_compute_signature_matches_the_server_formula() -> None:
    """compute_signature equals the independently computed HMAC."""
    body = body_of()
    expected = sign(body, SECRET)[SIGNATURE_HEADER]
    assert "sha256=" + compute_signature(SECRET, NOW, body) == expected
    assert compute_signature(SECRET.encode(), str(NOW), body.decode()) == expected[7:]


def test_valid_delivery_is_verified_and_parsed() -> None:
    """A correctly signed delivery yields its envelope."""
    body = body_of()
    event = construct_event(body, sign(body, SECRET), [SECRET], tolerance=300, now=NOW + 299)

    assert event.event_id == "event-0001"
    assert event.topics == ("risky_customer", "kyc_renew")
    assert event.attempt == 1
    assert event.occurred_at is not None
    assert event.occurred_at.utcoffset() is not None
    assert event.customer_ref == "C-TEST-0001"
    assert event.transaction_ref == "TX-TEST-0001"
    assert event.callback_topics == ("risky", "kyc")
    assert verify_signature(body, sign(body, SECRET), SECRET, now=NOW) == "event-0001"


def test_previous_secret_header_verifies_during_rotation() -> None:
    """A receiver holding only the old secret verifies via the previous header."""
    body = body_of()
    headers = sign(body, SECRET, previous=OLD_SECRET)
    assert verify_signature(body, headers, [OLD_SECRET], now=NOW) == "event-0001"
    assert verify_signature(body, headers, [OLD_SECRET, SECRET], now=NOW) == "event-0001"


def test_receiver_with_both_secrets_accepts_the_new_one() -> None:
    """A receiver may hold the new and the old secret at the same time."""
    body = body_of()
    assert verify_signature(body, sign(body, SECRET), [OLD_SECRET, SECRET], now=NOW)


def test_wrong_secret_is_rejected() -> None:
    """A signature made with another secret is rejected."""
    body = body_of()
    with pytest.raises(WebhookSignatureMismatchError) as caught:
        construct_event(body, sign(body, "whsec-test-other"), [SECRET], now=NOW)
    assert caught.value.reason == "bad_signature"


def test_tampered_body_is_rejected() -> None:
    """A body changed after signing no longer verifies."""
    body = body_of()
    headers = sign(body, SECRET)
    with pytest.raises(WebhookSignatureMismatchError):
        verify_signature(body.replace(b"85.00", b"05.00"), headers, [SECRET], now=NOW)


@pytest.mark.parametrize("offset", [301, -301, 100_000])
def test_stale_or_future_timestamp_is_rejected(offset: int) -> None:
    """Timestamps more than 300 seconds away are rejected.

    Args:
        offset: Distance of the receiver clock from the timestamp.
    """
    body = body_of()
    with pytest.raises(WebhookTimestampError) as caught:
        verify_signature(body, sign(body, SECRET), [SECRET], now=NOW + offset)
    assert caught.value.reason == "timestamp_out_of_tolerance"


def test_tolerance_is_configurable() -> None:
    """A custom tolerance widens or narrows the window."""
    body = body_of()
    headers = sign(body, SECRET)
    assert verify_signature(body, headers, [SECRET], tolerance=600, now=NOW + 500)
    with pytest.raises(WebhookTimestampError):
        verify_signature(body, headers, [SECRET], tolerance=10, now=NOW + 11)


@pytest.mark.parametrize("stamp", ["abc", "1.5", "-1", "1234567890123"])
def test_unreadable_timestamp_is_rejected(stamp: str) -> None:
    """Timestamps that are not 1 to 12 digits are rejected.

    Args:
        stamp: The malformed timestamp header.
    """
    body = body_of()
    headers = sign(body, SECRET)
    headers[TIMESTAMP_HEADER] = stamp
    with pytest.raises(WebhookTimestampError) as caught:
        verify_signature(body, headers, [SECRET], now=NOW)
    assert caught.value.reason == "bad_timestamp"


def test_missing_timestamp_header_is_rejected() -> None:
    """A delivery without a timestamp is rejected."""
    body = body_of()
    headers = sign(body, SECRET)
    del headers[TIMESTAMP_HEADER]
    with pytest.raises(WebhookMissingHeaderError) as caught:
        verify_signature(body, headers, [SECRET], now=NOW)
    assert caught.value.reason == "missing_timestamp"


def test_missing_signature_header_is_rejected_even_with_legacy_header() -> None:
    """The legacy AUTHORIZATION header (raw secret) is never accepted."""
    body = body_of()
    headers = {TIMESTAMP_HEADER: str(NOW), "AUTHORIZATION": SECRET, EVENT_ID_HEADER: "event-0001"}
    with pytest.raises(WebhookMissingHeaderError) as caught:
        verify_signature(body, headers, [SECRET], now=NOW)
    assert caught.value.reason == "missing_signature"
    assert isinstance(caught.value, WebhookVerificationError)


def test_malformed_signature_value_is_a_mismatch() -> None:
    """A present but malformed signature header fails as a mismatch."""
    body = body_of()
    headers = sign(body, SECRET)
    headers[SIGNATURE_HEADER] = "md5=abc"
    with pytest.raises(WebhookSignatureMismatchError):
        verify_signature(body, headers, [SECRET], now=NOW)


def test_signature_list_in_primary_header_is_accepted() -> None:
    """Comma-separated values in the primary header are each compared."""
    body = body_of()
    headers = sign(body, SECRET)
    digest = headers[SIGNATURE_HEADER].removeprefix("sha256=")
    headers[SIGNATURE_HEADER] = f"sha256={'0' * 64}, sha256={digest.upper()}"
    assert verify_signature(body, headers, [SECRET], now=NOW) == "event-0001"


def test_header_names_are_case_insensitive() -> None:
    """Header names match whatever their letter case."""
    body = body_of()
    headers = {name.lower(): value for name, value in sign(body, SECRET).items()}
    assert verify_signature(body, headers, [SECRET], now=NOW) == "event-0001"
    shouted = {name.upper(): value for name, value in sign(body, SECRET).items()}
    assert verify_signature(body, shouted, [SECRET], now=NOW) == "event-0001"


def test_event_id_header_must_match_the_body() -> None:
    """A replayed body under another event id header is rejected."""
    body = body_of()
    headers = sign(body, SECRET, event_id="event-9999")
    with pytest.raises(WebhookEventIdMismatchError) as caught:
        construct_event(body, headers, [SECRET], now=NOW)
    assert caught.value.reason == "event_id_mismatch"


def test_event_id_from_body_when_header_is_absent() -> None:
    """Without the header the body event id is used."""
    body = body_of()
    assert verify_signature(body, sign(body, SECRET, event_id=None), SECRET, now=NOW)


def test_missing_event_id_everywhere_is_malformed() -> None:
    """A delivery with no event id in header or body is rejected."""
    body = body_of(event_id="")
    with pytest.raises(WebhookMalformedBodyError) as caught:
        verify_signature(body, sign(body, SECRET, event_id=None), [SECRET], now=NOW)
    assert caught.value.reason == "missing_event_id"


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[1, 2, 3]",
        json.dumps({"event_id": "event-0001", "topic": "x", "data": []}).encode(),
        json.dumps({"event_id": "event-0001", "data": {}}).encode(),
        json.dumps({"event_id": "event-0001", "topic": "x", "data": {}, "attempt": "1"}).encode(),
    ],
)
def test_malformed_body_is_rejected(body: bytes) -> None:
    """Correctly signed bodies that are not valid envelopes are rejected.

    Args:
        body: The malformed body.
    """
    with pytest.raises(WebhookMalformedBodyError) as caught:
        construct_event(body, sign(body, SECRET), [SECRET], now=NOW)
    assert caught.value.reason == "malformed_body"


def test_text_body_is_encoded_as_utf8() -> None:
    """A body passed as text is verified over its UTF-8 bytes."""
    body = body_of(data={"customer_ref": "C-TEST-\u00e7"})
    event = construct_event(body.decode("utf-8"), sign(body, SECRET), SECRET, now=NOW)
    assert event.customer_ref == "C-TEST-\u00e7"
    assert event.transaction_ref is None
    assert event.callback_topics == ()


def test_secrets_are_required() -> None:
    """An empty secret list is a configuration error, not a rejected delivery."""
    body = body_of()
    candidates: list[Secrets] = [[], "", [""], [b""]]
    for secrets in candidates:
        with pytest.raises(ConfigurationError):
            verify_signature(body, sign(body, SECRET), secrets, now=NOW)
    with pytest.raises(InvalidArgumentError):
        verify_signature(body, sign(body, SECRET), SECRET, tolerance=-1, now=NOW)
    with pytest.raises(InvalidArgumentError):
        construct_event(body, sign(body, SECRET), SECRET, tolerance=-1, now=NOW)


def test_tolerance_and_clock_are_keyword_only() -> None:
    """verify_signature and construct_event take tolerance and now by keyword only."""
    for function in (verify_signature, construct_event):
        parameters = inspect.signature(function).parameters
        assert parameters["tolerance"].kind is inspect.Parameter.KEYWORD_ONLY
        assert parameters["now"].kind is inspect.Parameter.KEYWORD_ONLY


def test_parse_envelope_accepts_bytes_text_and_mappings() -> None:
    """parse_envelope reads stored bodies without verifying them."""
    body = body_of(topics=None, action="suspend")
    from_bytes = parse_envelope(body)
    assert from_bytes.topics == ("risky_customer",)
    assert from_bytes.action == "suspend"
    assert parse_envelope(body.decode()) == from_bytes
    assert parse_envelope(json.loads(body)) == from_bytes
    with pytest.raises(WebhookMalformedBodyError):
        parse_envelope(b"\xff\xfe")


def test_execution_log_shape_exposes_references() -> None:
    """References are also found inside data.execution_log.data_context."""
    body = body_of(
        data={
            "execution_log": {
                "data_context": {
                    "customer": {"customer_ref": "C-TEST-0002"},
                    "transaction": {"transaction_ref": "TX-TEST-0002"},
                }
            },
            "callback_data": [{"data": "alert"}, {"data": 5}, "junk"],
        }
    )
    event = parse_envelope(body)
    assert event.customer_ref == "C-TEST-0002"
    assert event.transaction_ref == "TX-TEST-0002"
    assert event.callback_topics == ("alert",)
