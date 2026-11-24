"""Tests of result parsing, exercised through the public client methods."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from truvali import (
    CustomerRecord,
    CustomerResult,
    Dataset,
    Decision,
    DeliveryStatus,
    EventPage,
    FinancialEvent,
    FinancialResult,
    IoType,
    NonFinancialEvent,
    ReconciliationReport,
    RecordResult,
    UnexpectedResponseError,
    UpsertAction,
    UpsertResponse,
)

from .fakes import customer_result, envelope, event_result, login_document, make_harness

WHEN = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


def non_financial_batch(
    items: list[dict[str, Any]], *, request_id: str | None = "req-0001"
) -> UpsertResponse[RecordResult]:
    """Send one non-financial event per result object and return the parsed response.

    Args:
        items: The result objects the fake server answers with.
        request_id: Request id of the fake response.

    Returns:
        The parsed response.
    """
    harness = make_harness()
    harness.adapter.reply(200, login_document())
    harness.adapter.reply(200, envelope({"results": items}, request_id=request_id))
    records = [
        NonFinancialEvent(
            customer_ref="C-TEST-0001",
            event_slug="login",
            transaction_ref=f"EVT-{index}",
            event_date=WHEN,
        )
        for index in range(len(items))
    ]
    return harness.client.upsert_non_financial_events(records)


def non_financial_result(item: dict[str, Any]) -> RecordResult:
    """Parse one non-financial result object through the client.

    Args:
        item: The result object the fake server answers with.

    Returns:
        The parsed result.
    """
    return non_financial_batch([item])[0]


def financial_result(item: dict[str, Any]) -> FinancialResult:
    """Parse one financial result object through the client.

    Args:
        item: The result object the fake server answers with.

    Returns:
        The parsed result.
    """
    harness = make_harness()
    harness.adapter.reply(200, login_document())
    harness.adapter.reply(200, envelope({"results": [item]}))
    record = FinancialEvent(
        customer_ref="C-TEST-0001",
        event_slug="deposit",
        transaction_ref="TX-1",
        event_date=WHEN,
        amount=Decimal("1.00"),
        currency="EUR",
        io_type=IoType.CREDIT,
    )
    return harness.client.upsert_financial_event(record)


def customer_result_of(item: dict[str, Any]) -> CustomerResult:
    """Parse one customer result object through the client.

    Args:
        item: The result object the fake server answers with.

    Returns:
        The parsed result.
    """
    harness = make_harness()
    harness.adapter.reply(200, login_document())
    harness.adapter.reply(200, envelope({"results": [item]}, request_id="req-c"))
    return harness.client.upsert_customer(CustomerRecord(customer_ref="C-1"))


def skipped(error: str) -> RecordResult:
    """Parse a skipped event result with the given error text.

    Args:
        error: The error text.

    Returns:
        The parsed result.
    """
    return non_financial_result(
        {
            "transaction_ref": "TX-1",
            "id": None,
            "score": "100.0",
            "decision": "REJECT",
            "action": "skipped",
            "error": error,
            "warnings": [],
            "log_id": None,
            "changed": False,
        }
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("40.00", Decimal("40.00")),
        ("100.0", Decimal("100.0")),
        (0, Decimal(0)),
        ("0", Decimal(0)),
        (12.5, Decimal("12.5")),
        (None, None),
    ],
)
def test_scores_parse_from_strings_and_numbers(raw: object, expected: Decimal | None) -> None:
    """Scores are read from decimal strings, integers and floats.

    Args:
        raw: The score as sent by the server.
        expected: The parsed value.
    """
    assert non_financial_result(event_result("TX-1", score=raw)).score == expected


@pytest.mark.parametrize("raw", ["abc", True, [1], "NaN"])
def test_malformed_scores_are_unexpected(raw: object) -> None:
    """Scores that are not finite numbers raise UnexpectedResponseError.

    Args:
        raw: The malformed score.
    """
    with pytest.raises(UnexpectedResponseError) as caught:
        non_financial_result(event_result("TX-1", score=raw))
    assert caught.value.request_id == "req-0001"


def test_old_financial_event_has_no_decision() -> None:
    """A new financial event older than 24 h comes back without a decision."""
    result = financial_result(event_result("TX-1", decision=None, rule_check=False))
    assert result.decision is None
    assert result.rule_check is False
    assert result.ok


def test_old_non_financial_event_is_accepted_without_rules() -> None:
    """A new non-financial event older than 24 h carries score 0 and ACCEPT."""
    result = non_financial_result(
        event_result("EVT-1", score=0, decision="ACCEPT", rule_check=False)
    )
    assert result.score == Decimal(0)
    assert result.decision is Decision.ACCEPT
    assert result.rule_check is False


def test_unknown_action_is_tolerated_and_neither_ok_nor_skipped() -> None:
    """An action a newer server may send maps to UNKNOWN and is handled safely."""
    response = non_financial_batch(
        [
            event_result("EVT-0", action="merged", changed=None),
            event_result("EVT-1", action="merged", error="concurrent_duplicate"),
            event_result("EVT-2"),
        ]
    )
    unknown = response[0]
    assert unknown.action is UpsertAction.UNKNOWN
    assert unknown.raw["action"] == "merged"
    assert not unknown.ok
    assert not unknown.skipped
    assert unknown.changed is False
    assert not response[1].retryable
    assert response[2].ok
    assert not response.all_ok
    assert response.skipped == ()


def test_unknown_decision_is_tolerated() -> None:
    """A decision a newer server may send maps to UNKNOWN; the text stays in raw."""
    result = non_financial_result(event_result("EVT-1", decision="ESCALATE"))
    assert result.decision is Decision.UNKNOWN
    assert result.raw["decision"] == "ESCALATE"
    assert result.ok


@pytest.mark.parametrize(
    "overrides",
    [{"action": None}, {"action": ["created"]}, {"decision": {"x": 1}}, {"warnings": "none"}],
)
def test_malformed_values_are_unexpected(overrides: dict[str, Any]) -> None:
    """Missing or mistyped keys are still reported as malformed.

    Args:
        overrides: Keys that break the result object.
    """
    with pytest.raises(UnexpectedResponseError):
        non_financial_result(event_result("EVT-1", **overrides))


def test_unknown_keys_are_kept_in_raw() -> None:
    """Keys a newer server may add are tolerated and kept in raw."""
    result = non_financial_result(event_result("EVT-1", risk_band="low"))
    assert result.raw["risk_band"] == "low"


def test_skipped_record() -> None:
    """A skipped record is not ok, carries its error and no id."""
    result = skipped("'currency' is required.")
    assert result.action is UpsertAction.SKIPPED
    assert result.skipped
    assert not result.ok
    assert result.id is None
    assert result.score == Decimal("100.0")
    assert result.decision is Decision.REJECT
    assert result.rule_check is None
    assert result.changed is False
    assert result.request_id == "req-0001"
    assert not result.customer_missing
    assert not result.retryable


def test_customer_missing_alone_is_retryable() -> None:
    """A record whose only error is the missing customer can be resent."""
    result = skipped("Valid customer_ref 'C-TEST-9999' not found.")
    assert result.customer_missing
    assert result.retryable
    assert not result.concurrent_duplicate


@pytest.mark.parametrize(
    "error",
    [
        "'currency' is required. Valid customer_ref 'C-TEST-9999' not found.",
        "Valid customer_ref 'A' not found. 'amount' is required.",
        "Valid customer_ref 'A' not found. 'amount' is required. Valid customer_ref 'A' not found.",
        "Valid customer_ref 'A' not found. Valid customer_ref 'B' not found.",
        "Valid customer_ref 'A' not found.\n'amount' is required.",
    ],
)
def test_customer_missing_with_other_errors_is_not_retryable(error: str) -> None:
    """Joined errors keep customer_missing but are never retryable.

    Args:
        error: The joined error text.
    """
    result = skipped(error)
    assert result.customer_missing
    assert not result.retryable


def test_concurrent_duplicate_is_retryable() -> None:
    """A lost create race is flagged and retryable."""
    result = skipped("concurrent_duplicate")
    assert result.concurrent_duplicate
    assert result.retryable
    assert not result.customer_missing


def test_missing_changed_defaults_by_action() -> None:
    """Without a changed key, stored records count as changed."""
    item = event_result("TX-1")
    del item["changed"]
    assert non_financial_result(item).changed is True


def test_event_result_with_challenge_and_warnings() -> None:
    """Challenges and warnings are parsed into typed values."""
    item = event_result(
        "TX-1",
        warnings=["'currency' was normalised to upper case."],
        challenge={
            "id": "challenge-0001",
            "type": "verification_flow",
            "target": "step_up_selfie",
            "status": "pending",
            "rule": "fake rule",
            "session_id": None,
            "complete_by": "2026-10-19T12:15:00Z",
        },
    )
    result = non_financial_result(item)
    assert result.ref == "TX-1"
    assert result.log_id == "log-TX-1"
    assert result.warnings == ("'currency' was normalised to upper case.",)
    assert result.challenge is not None
    assert result.challenge.status == "pending"
    assert result.challenge.complete_by == datetime(2026, 10, 19, 12, 15, tzinfo=timezone.utc)
    assert result.raw == item


def test_customer_result_fields() -> None:
    """Customer results expose screening and scoring keys."""
    item = customer_result(
        "C-1",
        sanction_score=92,
        sanction_topic=["sanction"],
        sanction_status="screened",
        sanction_screened_at="2026-10-21T09:30:01.512000+03:00",
        subjects=[{"role": "ubo", "index": 0, "sanction_score": 92}],
        subjects_sanction_status="screened",
        subjects_unscreened_count=0,
    )
    result = customer_result_of(item)
    assert result.ref == "C-1"
    assert result.request_id == "req-c"
    assert result.rule_check is None
    assert result.sanction_score == Decimal(92)
    assert result.sanction_topic == ("sanction",)
    assert result.sanction_screened_at is not None
    assert result.sanction_screened_at.utcoffset() is not None
    assert result.subjects[0]["role"] == "ubo"
    assert result.subjects_unscreened_count == 0
    assert result.ip_address_score == Decimal("12.50")
    assert result.email_risk_score == Decimal(0)


def test_financial_result_mappings() -> None:
    """Financial results carry Travel Rule and counterparty screening objects."""
    item = event_result(
        "TX-1",
        travel_rule={"status": "incomplete", "missing": ["counterparty_name"], "regime": "fatf"},
        counterparty_sanction={"score": None, "side": None},
    )
    result = financial_result(item)
    assert result.travel_rule is not None
    assert result.travel_rule["missing"] == ["counterparty_name"]
    assert result.counterparty_sanction == {"score": None, "side": None}
    assert result.counterparty_wallet_sanction is None


def test_upsert_response_lookup_iteration_and_length() -> None:
    """The response keeps input order and looks results up by ref."""
    response = non_financial_batch(
        [
            event_result("TX-1"),
            event_result("TX-2", action="skipped", id=None, error="'amount' is required."),
            event_result("TX-1", action="updated", changed=False),
        ],
        request_id="req-u",
    )
    assert len(response) == 3
    assert [result.ref for result in response] == ["TX-1", "TX-2", "TX-1"]
    assert response[1].skipped
    assert response.skipped == (response[1],)
    assert not response.all_ok
    assert response.by_ref("TX-1") is response[2]
    assert response.by_ref("TX-404") is None
    assert all(result.request_id == "req-u" for result in response)


def test_missing_results_list_is_unexpected() -> None:
    """A response without data.results is reported with its request id."""
    harness = make_harness()
    harness.adapter.reply(200, login_document())
    harness.adapter.reply(200, envelope({}, request_id="req-empty"))
    with pytest.raises(UnexpectedResponseError) as caught:
        harness.client.upsert_customer(CustomerRecord(customer_ref="C-1"))
    assert caught.value.request_id == "req-empty"


def test_auth_check() -> None:
    """The token check answer is parsed; a missing key is reported."""
    harness = make_harness()
    harness.adapter.reply(200, login_document())
    harness.adapter.reply(
        200, envelope({"client": "Example Source", "session_id": "session-0001"}, request_id=None)
    )
    harness.adapter.reply(200, envelope({"client": "Example Source"}, request_id=None))
    assert harness.client.check_auth().session_id == "session-0001"
    with pytest.raises(UnexpectedResponseError):
        harness.client.check_auth()


def reconciliation_report(data: dict[str, Any]) -> ReconciliationReport:
    """Request a report and answer it with the given data object.

    Args:
        data: The ``data`` object of the fake response.

    Returns:
        The parsed report.
    """
    harness = make_harness()
    harness.adapter.reply(200, login_document())
    harness.adapter.reply(200, envelope(data, request_id=None))
    return harness.client.reconciliation()


def test_reconciliation_report() -> None:
    """Reconciliation counts, breakdowns and day buckets are typed."""
    report = reconciliation_report(
        {
            "data_source": {"id": "source-0001", "name": "Example Source"},
            "generated_at": "2026-10-20T08:00:00+00:00",
            "window": {
                "period": "range",
                "date_from": "2026-10-18",
                "date_to": "2026-10-19",
                "date_field": "registration_date",
                "timezone": "Europe/Berlin",
                "date_to_inclusive": True,
            },
            "datasets": {
                "financial": {
                    "count": 110,
                    "date_field_used": "create_date",
                    "note": "financial has no registration_date; counted by create_date instead.",
                    "breakdown": {"decision": {"ACCEPT": 98, "REVIEW": 12}, "io_type": {"0": 110}},
                    "by_day": {"2026-10-18": 50, "2026-10-19": 60},
                },
                "customer": {"count": 7, "date_field_used": "registration_date"},
                "screening": {"count": 3, "date_field_used": "create_date"},
            },
        }
    )
    assert report.data_source_id == "source-0001"
    assert report.window.date_from == date(2026, 10, 18)
    financial = report.datasets[Dataset.FINANCIAL]
    assert financial.count == 110
    assert financial.note is not None
    assert financial.breakdown["decision"]["REVIEW"] == 12
    assert financial.by_day[date(2026, 10, 19)] == 60
    customers = report.datasets["customer"]
    assert customers.breakdown == {}
    assert customers.by_day == {}
    assert report.datasets["screening"].count == 3


def test_malformed_reconciliation_report_is_unexpected() -> None:
    """Missing or mistyped keys raise UnexpectedResponseError."""
    with pytest.raises(UnexpectedResponseError):
        reconciliation_report({"data_source": {"id": "x"}})


def feed_page(events: list[dict[str, Any]]) -> EventPage:
    """Request one feed page and answer it with the given events.

    Args:
        events: The event objects of the fake page.

    Returns:
        The parsed page.
    """
    harness = make_harness()
    harness.adapter.reply(200, login_document())
    harness.adapter.reply(
        200,
        envelope(
            {"events": events, "next_cursor": None, "last_cursor": "cursor-0001"},
            request_id=None,
        ),
    )
    return harness.client.events()


def feed_event(status: str) -> dict[str, Any]:
    """Build an event feed entry with the given delivery status.

    Args:
        status: The delivery status.

    Returns:
        The entry object.
    """
    return {
        "event_id": "event-0001",
        "topic": "sanction_rescreen_hit",
        "topics": ["sanction_rescreen_hit"],
        "action": None,
        "request_id": None,
        "data_source": "source-0001",
        "occurred_at": "2026-10-20T02:10:00+00:00",
        "data": {"customer_ref": "C-TEST-0001"},
        "delivery": {
            "status": status,
            "attempts": 8,
            "last_attempt_at": "2026-10-20T15:52:00+00:00",
            "endpoint": {"id": "endpoint-0001", "name": "Example receiver"},
        },
    }


def test_event_page() -> None:
    """Feed entries are envelopes with a delivery state."""
    page = feed_page([feed_event("dead")])
    assert len(page) == 1
    assert not page.has_more
    assert page.last_cursor == "cursor-0001"
    event = next(iter(page))
    assert event.attempt is None
    assert event.customer_ref == "C-TEST-0001"
    assert event.delivery.status is DeliveryStatus.DEAD
    assert event.delivery.attempts == 8
    assert event.delivery.endpoint_id == "endpoint-0001"


def test_unknown_delivery_status_is_tolerated() -> None:
    """A delivery status a newer server may send maps to UNKNOWN."""
    event = feed_page([feed_event("paused")]).events[0]
    assert event.delivery.status is DeliveryStatus.UNKNOWN
    assert event.delivery.raw["status"] == "paused"


def test_malformed_feed_entry_is_unexpected() -> None:
    """An entry without the envelope keys raises UnexpectedResponseError."""
    with pytest.raises(UnexpectedResponseError):
        feed_page([{"event_id": "x"}])
