"""Tests of the client methods against the scripted adapter."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
import requests

from truvali import (
    BadRequestError,
    ConfigurationError,
    CustomerRecord,
    Dataset,
    DateBasis,
    ErrorCode,
    FinancialEvent,
    InvalidArgumentError,
    IoType,
    NonFinancialEvent,
    PayloadTooLargeError,
    RecordValidationError,
    TruvaliClient,
    UnexpectedResponseError,
)
from truvali.transport import TOKEN_PATH, Transport

from .fakes import (
    customer_result,
    envelope,
    error_document,
    event_result,
    login_document,
    make_config,
    make_harness,
)

WHEN = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
ENDPOINT_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")


def financial(ref: str, **overrides: Any) -> FinancialEvent:
    """Build a valid financial event with fake data.

    Args:
        ref: The transaction reference.
        **overrides: Fields that replace the defaults.

    Returns:
        The event.
    """
    fields: dict[str, Any] = {
        "customer_ref": "C-TEST-0001",
        "event_slug": "deposit",
        "transaction_ref": ref,
        "event_date": WHEN,
        "amount": Decimal("100.00"),
        "currency": "EUR",
        "io_type": IoType.CREDIT,
    }
    fields.update(overrides)
    return FinancialEvent(**fields)


def login_event(ref: str) -> NonFinancialEvent:
    """Build a valid non-financial event with fake data.

    Args:
        ref: The transaction reference.

    Returns:
        The event.
    """
    return NonFinancialEvent(
        customer_ref="C-TEST-0001", event_slug="login", transaction_ref=ref, event_date=WHEN
    )


def feed_event(event_id: str) -> dict[str, Any]:
    """Build an event feed entry.

    Args:
        event_id: The event id.

    Returns:
        The entry object.
    """
    return {
        "event_id": event_id,
        "topic": "compliance_decision",
        "topics": ["compliance_decision"],
        "action": None,
        "request_id": None,
        "data_source": "source-0001",
        "occurred_at": "2026-10-20T02:10:00+00:00",
        "data": {"kind": "compliance_decision"},
        "delivery": {"status": "success", "attempts": 1, "last_attempt_at": None, "endpoint": None},
    }


def test_upsert_customers_sends_a_canonical_batch() -> None:
    """Customers are sent as a sorted, compact JSON list and parsed in order."""
    harness = make_harness()
    harness.adapter.reply(200, login_document())
    harness.adapter.reply(
        200,
        envelope(
            {"results": [customer_result("C-1"), customer_result("C-2", action="updated")]},
            request_id="req-batch",
            message="Processed 2 customer record(s).",
        ),
    )

    response = harness.client.upsert_customers(
        [
            CustomerRecord(customer_ref="C-1", last_name="Example", first_name="Jane"),
            CustomerRecord(customer_ref="C-2", migration=True),
        ]
    )

    assert response.request_id == "req-batch"
    assert response.message == "Processed 2 customer record(s)."
    assert [result.ref for result in response] == ["C-1", "C-2"]
    request = harness.adapter.sent[1]
    assert request.method == "POST"
    assert harness.adapter.paths()[1] == "/api/customer/upsert/"
    assert request.headers["Content-Type"] == "application/json"
    assert request.body == (
        b'[{"customer_ref":"C-1","first_name":"Jane","last_name":"Example"},'
        b'{"customer_ref":"C-2","migration":true}]'
    )


def test_non_ascii_text_is_sent_as_utf8() -> None:
    """Text is sent as UTF-8 rather than escaped, keeping bodies small."""
    harness = make_harness()
    harness.adapter.reply(200, login_document())
    harness.adapter.reply(200, envelope({"results": [customer_result("C-1")]}))

    harness.client.upsert_customer(CustomerRecord(customer_ref="C-1", first_name="J\u00f6rg"))

    body = harness.adapter.sent[1].body
    assert isinstance(body, bytes)
    assert "J\u00f6rg".encode() in body


def test_single_record_conveniences() -> None:
    """The single-record methods return the one result with its request id."""
    harness = make_harness()
    harness.adapter.reply(200, login_document())
    harness.adapter.reply(200, envelope({"results": [event_result("TX-1")]}, request_id="r1"))
    harness.adapter.reply(200, envelope({"results": [event_result("EVT-1")]}, request_id="r2"))
    harness.adapter.reply(200, envelope({"results": [customer_result("C-1")]}, request_id="r3"))

    financial_result = harness.client.upsert_financial_event(financial("TX-1"))
    event_result_ = harness.client.upsert_non_financial_event(login_event("EVT-1"))
    customer = harness.client.upsert_customer(CustomerRecord(customer_ref="C-1"))

    assert financial_result.request_id == "r1"
    assert financial_result.score == Decimal("40.00")
    assert event_result_.ref == "EVT-1"
    assert customer.request_id == "r3"
    assert harness.adapter.paths()[1:] == [
        "/api/transaction/financial/upsert/",
        "/api/transaction/non-financial/upsert/",
        "/api/customer/upsert/",
    ]
    assert harness.adapter.body(1)[0]["amount"] == "100.00"


def test_empty_batches_are_rejected_locally() -> None:
    """An empty batch raises before anything is sent."""
    harness = make_harness()
    with pytest.raises(RecordValidationError):
        harness.client.upsert_customers([])
    assert harness.adapter.sent == []


def test_mixed_record_types_are_rejected_locally() -> None:
    """A batch may hold one record type only."""
    harness = make_harness()
    batch: list[Any] = [financial("TX-1"), login_event("EVT-1")]
    with pytest.raises(RecordValidationError) as caught:
        harness.client.upsert_financial_events(batch)
    assert "records[1]" in str(caught.value)
    assert harness.adapter.sent == []


def test_single_record_instead_of_sequence_is_rejected() -> None:
    """Passing one record to a batch method gives a clear error."""
    harness = make_harness()
    record: Any = CustomerRecord(customer_ref="C-1")
    with pytest.raises(RecordValidationError):
        harness.client.upsert_customers(record)


def test_batches_over_max_batch_size_are_rejected_locally() -> None:
    """More records than max_batch_size raise PayloadTooLargeError."""
    harness = make_harness(max_batch_size=2)
    records = [CustomerRecord(customer_ref=f"C-{index}") for index in range(3)]
    with pytest.raises(PayloadTooLargeError) as caught:
        harness.client.upsert_customers(records)
    assert caught.value.status_code is None
    assert harness.adapter.sent == []


def test_server_maximum_of_1000_records() -> None:
    """With the maximum batch size, 1001 records are still rejected locally."""
    harness = make_harness(max_batch_size=1000)
    records = [CustomerRecord(customer_ref=f"C-{index}") for index in range(1001)]
    with pytest.raises(PayloadTooLargeError):
        harness.client.upsert_customers(records)
    assert harness.adapter.sent == []


def test_bodies_over_one_mebibyte_are_rejected_locally() -> None:
    """A body over 1 MiB raises PayloadTooLargeError before sending."""
    harness = make_harness(max_batch_size=1000)
    records = [
        NonFinancialEvent(
            customer_ref="C-TEST-0001",
            event_slug="login",
            transaction_ref=f"EVT-{index}",
            event_date=WHEN,
            user_agent="x" * 2000,
        )
        for index in range(600)
    ]
    with pytest.raises(PayloadTooLargeError) as caught:
        harness.client.upsert_non_financial_events(records)
    assert "1 MiB" in str(caught.value)
    assert harness.adapter.sent == []


def test_result_count_mismatch_is_unexpected() -> None:
    """Fewer results than records raise UnexpectedResponseError."""
    harness = make_harness()
    harness.adapter.reply(200, login_document())
    harness.adapter.reply(200, envelope({"results": [event_result("EVT-1")]}, request_id="rq"))

    with pytest.raises(UnexpectedResponseError) as caught:
        harness.client.upsert_non_financial_events([login_event("EVT-1"), login_event("EVT-2")])
    assert caught.value.request_id == "rq"


def test_batch_format_error_is_bad_request() -> None:
    """A 400 for the batch as a whole is raised with its request id."""
    harness = make_harness()
    harness.adapter.reply(200, login_document())
    harness.adapter.reply(400, error_document("Request body is not valid JSON.", None))

    with pytest.raises(BadRequestError) as caught:
        harness.client.upsert_customer(CustomerRecord(customer_ref="C-1"))
    assert caught.value.request_id == "req-error"


def test_reconciliation_range_with_day_groups() -> None:
    """A date range with day groups is sent with the documented parameters."""
    harness = make_harness()
    harness.adapter.reply(200, login_document())
    harness.adapter.reply(
        200,
        envelope(
            {
                "data_source": {"id": "source-0001", "name": "Example Source"},
                "generated_at": "2026-10-20T08:00:00+00:00",
                "window": {
                    "period": "range",
                    "date_from": "2026-10-18",
                    "date_to": "2026-10-19",
                    "date_field": "event_date",
                    "timezone": "Europe/Berlin",
                },
                "datasets": {
                    "nonfinancial": {
                        "count": 3,
                        "date_field_used": "event_date",
                        "by_day": {"2026-10-18": 1, "2026-10-19": 2},
                    }
                },
            },
            request_id=None,
        ),
    )

    report = harness.client.reconciliation(
        datasets=[Dataset.NON_FINANCIAL, Dataset.NON_FINANCIAL],
        date_from=date(2026, 10, 18),
        date_to=date(2026, 10, 19),
        timezone="Europe/Berlin",
        date_field=DateBasis.EVENT_DATE,
        group_by_day=True,
        breakdown=False,
    )

    assert harness.adapter.query(1) == {
        "dataset": ["nonfinancial"],
        "period": ["range"],
        "date_from": ["2026-10-18"],
        "date_to": ["2026-10-19"],
        "timezone": ["Europe/Berlin"],
        "date_field": ["event_date"],
        "group_by": ["day"],
        "breakdown": ["0"],
    }
    assert report.datasets[Dataset.NON_FINANCIAL].by_day[date(2026, 10, 19)] == 2


def test_reconciliation_day_and_total_parameters() -> None:
    """A single day and the total use their own period values."""
    harness = make_harness()
    data = {
        "data_source": {"id": "source-0001", "name": "Example Source"},
        "window": {"period": "total", "date_field": "create_date", "timezone": "UTC"},
        "datasets": {},
    }
    harness.adapter.reply(200, login_document())
    harness.adapter.reply(200, envelope(data, request_id=None))
    harness.adapter.reply(200, envelope(data, request_id=None))

    harness.client.reconciliation(day=date(2026, 10, 19))
    harness.client.reconciliation()

    assert harness.adapter.query(1) == {
        "period": ["day"],
        "date": ["2026-10-19"],
        "breakdown": ["1"],
    }
    assert harness.adapter.query(2) == {"period": ["total"], "breakdown": ["1"]}


@pytest.mark.parametrize(
    "arguments",
    [
        {"day": date(2026, 10, 19), "date_from": date(2026, 10, 18)},
        {"date_from": date(2026, 10, 18)},
        {"date_from": date(2026, 10, 19), "date_to": date(2026, 10, 18)},
        {"day": datetime(2026, 10, 19, tzinfo=timezone.utc)},
        {"group_by_day": True},
        {"date_from": date(2025, 1, 1), "date_to": date(2026, 1, 2), "group_by_day": True},
        {"datasets": []},
        {"datasets": ["orders"]},
        {"datasets": "customer"},
        {"date_field": "updated_at"},
        {"timezone": " "},
        {"timeout": 0},
    ],
)
def test_invalid_reconciliation_arguments(arguments: dict[str, Any]) -> None:
    """Conflicting or malformed arguments raise InvalidArgumentError before sending.

    Args:
        arguments: The invalid keyword arguments.
    """
    harness = make_harness()
    with pytest.raises(InvalidArgumentError) as caught:
        harness.client.reconciliation(**arguments)
    assert isinstance(caught.value, ValueError)
    assert harness.adapter.sent == []


def test_invalid_enum_arguments_have_readable_messages() -> None:
    """Unknown dataset and date field names list the accepted values."""
    harness = make_harness()
    datasets: Any = ["orders"]
    with pytest.raises(InvalidArgumentError) as caught:
        harness.client.reconciliation(datasets=datasets)
    assert str(caught.value) == (
        "datasets got 'orders'; expected one of: customer, financial, nonfinancial."
    )
    date_field: Any = "updated_at"
    with pytest.raises(InvalidArgumentError) as caught:
        harness.client.reconciliation(date_field=date_field)
    assert "create_date, event_date, registration_date" in str(caught.value)


def test_events_page_parameters() -> None:
    """Feed filters are sent with a UTC since and a canonical endpoint id."""
    harness = make_harness()
    harness.adapter.reply(200, login_document())
    harness.adapter.reply(
        200,
        envelope({"events": [], "next_cursor": None, "last_cursor": None}, request_id=None),
    )

    page = harness.client.events(
        topic="compliance_decision",
        since=datetime(2026, 10, 20, tzinfo=timezone(timedelta(hours=3))),
        endpoint=str(ENDPOINT_ID).upper(),
        limit=500,
    )

    assert page.events == ()
    assert page.last_cursor is None
    assert harness.adapter.query(1) == {
        "topic": ["compliance_decision"],
        "since": ["2026-10-19T21:00:00+00:00"],
        "endpoint": [str(ENDPOINT_ID)],
        "limit": ["500"],
    }


@pytest.mark.parametrize(
    "arguments",
    [
        {"limit": 0},
        {"limit": 501},
        {"limit": True},
        {"since": datetime(2026, 10, 20)},
        {"endpoint": "not-a-uuid"},
        {"topic": ""},
        {"cursor": ""},
        {"timeout": -1},
        {"timeout": True},
        {"timeout": float("nan")},
    ],
)
def test_invalid_feed_arguments(arguments: dict[str, Any]) -> None:
    """Malformed feed filters raise InvalidArgumentError before sending.

    Args:
        arguments: The invalid keyword arguments.
    """
    harness = make_harness()
    with pytest.raises(InvalidArgumentError):
        harness.client.events(**arguments)
    with pytest.raises(InvalidArgumentError):
        harness.client.iter_events(**arguments)
    with pytest.raises(InvalidArgumentError):
        harness.client.iter_event_pages(**arguments)
    assert harness.adapter.sent == []


def test_iter_events_follows_cursors() -> None:
    """iter_events reads every page in order, passing the filters each time."""
    harness = make_harness()
    harness.adapter.reply(200, login_document())
    harness.adapter.reply(
        200,
        envelope(
            {
                "events": [feed_event("event-1"), feed_event("event-2")],
                "next_cursor": "cursor-2",
                "last_cursor": "cursor-2",
            },
            request_id=None,
        ),
    )
    harness.adapter.reply(
        200,
        envelope(
            {"events": [feed_event("event-3")], "next_cursor": None, "last_cursor": "cursor-3"},
            request_id=None,
        ),
    )

    events = list(harness.client.iter_events(endpoint=ENDPOINT_ID, limit=2))

    assert [event.event_id for event in events] == ["event-1", "event-2", "event-3"]
    assert harness.adapter.paths() == [TOKEN_PATH, "/api/engine/events/", "/api/engine/events/"]
    assert "cursor" not in harness.adapter.query(1)
    assert harness.adapter.query(2) == {
        "endpoint": [str(ENDPOINT_ID)],
        "limit": ["2"],
        "cursor": ["cursor-2"],
    }


def test_iter_event_pages_exposes_last_cursor() -> None:
    """Paging from a stored cursor ends with the cursor to store next."""
    harness = make_harness()
    harness.adapter.reply(200, login_document())
    harness.adapter.reply(
        200,
        envelope({"events": [], "next_cursor": None, "last_cursor": "cursor-9"}, request_id=None),
    )

    pages = list(harness.client.iter_event_pages(cursor="cursor-9"))

    assert len(pages) == 1
    assert pages[-1].last_cursor == "cursor-9"
    assert harness.adapter.query(1) == {"cursor": ["cursor-9"]}


def test_feed_error_codes_map_to_bad_request() -> None:
    """Feed parameter errors keep their data.error code."""
    harness = make_harness()
    harness.adapter.reply(200, login_document())
    harness.adapter.reply(
        400,
        {
            "status": False,
            "message": "cursor is not a value returned by this endpoint.",
            "data": {"error": "invalid_cursor"},
        },
    )

    with pytest.raises(BadRequestError) as caught:
        harness.client.events(cursor="forged")
    assert caught.value.code == ErrorCode.INVALID_CURSOR


def test_transport_and_session_are_exclusive() -> None:
    """A ready transport cannot be combined with a session or another config."""
    config = make_config()
    transport = Transport(config)
    with pytest.raises(ConfigurationError):
        TruvaliClient(config, transport=transport, session=requests.Session())
    with pytest.raises(ConfigurationError):
        TruvaliClient(make_config(read_timeout=1.0), transport=transport)
    transport.close()


class TrackingSession(requests.Session):
    """Session that records whether it was closed."""

    def __init__(self) -> None:
        """Initialise an open session."""
        super().__init__()
        self.closed = False

    def close(self) -> None:
        """Record the close and close the session."""
        self.closed = True
        super().close()


def test_client_is_a_context_manager_that_keeps_foreign_sessions_open() -> None:
    """Leaving the context closes only sessions the client created."""
    session = TrackingSession()
    with TruvaliClient(make_config(), session=session) as client:
        assert client.config.base_url.endswith("example.com")
    assert session.closed is False


def test_timeout_overrides_the_read_timeout_per_call() -> None:
    """A per-call timeout replaces the read timeout of that request only."""
    harness = make_harness(read_timeout=30.0)
    harness.adapter.reply(200, login_document())
    harness.adapter.reply(200, envelope({"results": [customer_result("C-1")]}))
    harness.adapter.reply(200, envelope({"results": [event_result("EVT-1")]}))
    harness.adapter.reply(
        200, envelope({"events": [], "next_cursor": None, "last_cursor": None}, request_id=None)
    )
    harness.adapter.reply(
        200, envelope({"client": "Example Source", "session_id": "s-1"}, request_id=None)
    )

    harness.client.upsert_customer(CustomerRecord(customer_ref="C-1"), timeout=10)
    harness.client.upsert_non_financial_events([login_event("EVT-1")], timeout=60.0)
    list(harness.client.iter_events(timeout=12.5))
    harness.client.check_auth()

    assert harness.adapter.timeouts == [
        (5.0, 30.0),
        (5.0, 10.0),
        (5.0, 60.0),
        (5.0, 12.5),
        (5.0, 30.0),
    ]


@pytest.mark.parametrize("timeout", [0, -5, float("inf"), "10", True])
def test_invalid_timeouts_are_rejected_before_sending(timeout: Any) -> None:
    """Timeouts must be finite numbers greater than zero.

    Args:
        timeout: The invalid timeout.
    """
    harness = make_harness()
    with pytest.raises(InvalidArgumentError):
        harness.client.upsert_customer(CustomerRecord(customer_ref="C-1"), timeout=timeout)
    with pytest.raises(InvalidArgumentError):
        harness.client.check_auth(timeout=timeout)
    assert harness.adapter.sent == []


def test_closed_client_refuses_requests() -> None:
    """After close() every request raises ConfigurationError without sending."""
    harness = make_harness()
    harness.client.close()
    harness.client.close()

    assert harness.client.closed
    with pytest.raises(ConfigurationError, match="closed"):
        harness.client.check_auth()
    with pytest.raises(ConfigurationError):
        harness.client.upsert_customer(CustomerRecord(customer_ref="C-1"))
    with pytest.raises(ConfigurationError):
        harness.client.reconciliation()
    with pytest.raises(ConfigurationError):
        harness.client.iter_events()
    with pytest.raises(ConfigurationError):
        harness.transport.request("GET", "/api/merchant/auth/check/")
    assert harness.adapter.sent == []


def test_leaving_the_context_closes_the_client() -> None:
    """The context manager closes the client on exit."""
    harness = make_harness()
    harness.adapter.reply(200, login_document())
    harness.adapter.reply(
        200, envelope({"client": "Example Source", "session_id": "s-1"}, request_id=None)
    )
    with harness.client as client:
        assert client.check_auth().client == "Example Source"
        assert not client.closed
    with pytest.raises(ConfigurationError):
        harness.client.check_auth()
