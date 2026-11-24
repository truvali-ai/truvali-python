"""Tests of record validation and payload rendering."""

from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from truvali import (
    Channel,
    Counterparty,
    CounterpartyDeclaration,
    CounterpartyKind,
    CustomerRecord,
    CustomerType,
    FinancialEvent,
    Gender,
    InvalidArgumentError,
    IoType,
    NonFinancialEvent,
    RecordValidationError,
)

UTC = timezone.utc
PLUS_THREE = timezone(timedelta(hours=3))
WHEN = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def financial(**overrides: Any) -> FinancialEvent:
    """Build a valid financial event with fake data.

    Args:
        **overrides: Fields that replace the defaults.

    Returns:
        The event.
    """
    fields: dict[str, Any] = {
        "customer_ref": "C-TEST-0001",
        "event_slug": "transfer_out",
        "transaction_ref": "TX-TEST-0001",
        "event_date": WHEN,
        "amount": Decimal("9000.00"),
        "currency": "EUR",
        "io_type": IoType.DEBIT,
    }
    fields.update(overrides)
    return FinancialEvent(**fields)


def non_financial(**overrides: Any) -> NonFinancialEvent:
    """Build a valid non-financial event with fake data.

    Args:
        **overrides: Fields that replace the defaults.

    Returns:
        The event.
    """
    fields: dict[str, Any] = {
        "customer_ref": "C-TEST-0001",
        "event_slug": "failed_login",
        "transaction_ref": "EVT-TEST-0001",
        "event_date": WHEN,
    }
    fields.update(overrides)
    return NonFinancialEvent(**fields)


def rejected(build: Any, **overrides: Any) -> RecordValidationError:
    """Assert that building a record fails and return the error.

    Args:
        build: The record factory.
        **overrides: Fields passed to the factory.

    Returns:
        The validation error.
    """
    with pytest.raises(RecordValidationError) as caught:
        build(**overrides)
    return caught.value


def test_customer_ref_length_limit() -> None:
    """customer_ref accepts 48 characters and rejects 49."""
    assert CustomerRecord(customer_ref="C" * 48).customer_ref == "C" * 48
    error = rejected(CustomerRecord, customer_ref="C" * 49)
    assert error.field == "customer_ref"
    assert "48" in str(error)


@pytest.mark.parametrize("ref", ["", " C-1", "C-1 ", "\tC-1"])
def test_refs_must_be_exact(ref: str) -> None:
    """References must be non-empty and free of surrounding whitespace.

    Args:
        ref: The invalid reference.
    """
    assert rejected(CustomerRecord, customer_ref=ref).field == "customer_ref"
    assert rejected(non_financial, transaction_ref=ref).field == "transaction_ref"


def test_transaction_ref_is_required_for_non_financial_events() -> None:
    """A non-financial event without transaction_ref is rejected."""
    assert rejected(non_financial, transaction_ref=None).field == "transaction_ref"


def test_transaction_ref_length_limit() -> None:
    """transaction_ref accepts 64 characters and rejects 65."""
    assert financial(transaction_ref="T" * 64).transaction_ref == "T" * 64
    assert rejected(financial, transaction_ref="T" * 65).field == "transaction_ref"


def test_text_length_is_measured_after_trimming() -> None:
    """Like the server, the length limit applies to the trimmed text."""
    assert non_financial(status="s" * 16 + "  ").status == "s" * 16 + "  "
    assert rejected(non_financial, status="s" * 17).field == "status"
    assert rejected(CustomerRecord, customer_ref="C-1", status="x" * 25).field == "status"
    assert rejected(CustomerRecord, customer_ref="C-1", lei_code="L" * 21).field == "lei_code"


def test_text_fields_must_be_strings() -> None:
    """Non-string values in text fields are rejected."""
    assert rejected(CustomerRecord, customer_ref="C-1", phone=5550100).field == "phone"


@pytest.mark.parametrize("currency", ["eur", "EURO", "EU", "E1R", "", "US$"])
def test_currency_must_be_three_upper_case_letters(currency: str) -> None:
    """Currency codes are three upper-case letters; nothing is normalised.

    Args:
        currency: The invalid code.
    """
    assert rejected(financial, currency=currency).field == "currency"
    assert rejected(financial, fee_currency=currency).field == "fee_currency"


@pytest.mark.parametrize(
    "amount",
    [
        Decimal("10.005"),
        Decimal("0.001"),
        Decimal("-1.00"),
        Decimal("-0"),
        Decimal("10000000000.00"),
        Decimal("NaN"),
        Decimal("Infinity"),
        10.5,
        "10.50",
        True,
    ],
)
def test_invalid_amounts_are_rejected_not_rounded(amount: object) -> None:
    """Amounts must be non-negative Decimals or ints with at most two places.

    Args:
        amount: The invalid amount.
    """
    assert rejected(financial, amount=amount).field == "amount"
    assert rejected(financial, fee_amount=amount).field == "fee_amount"


@pytest.mark.parametrize(
    ("amount", "rendered"),
    [
        (Decimal("0"), "0.00"),
        (Decimal("10.5"), "10.50"),
        (Decimal("10.500"), "10.50"),
        (Decimal("1E+2"), "100.00"),
        (Decimal("9999999999.99"), "9999999999.99"),
        (25, "25.00"),
    ],
)
def test_valid_amounts_render_with_two_decimals(amount: Decimal | int, rendered: str) -> None:
    """Valid amounts are rendered as strings with exactly two decimal places.

    Args:
        amount: The valid amount.
        rendered: Its expected payload value.
    """
    event = financial(amount=amount, fee_amount=amount)
    assert event.to_payload()["amount"] == rendered
    assert event.to_payload()["fee_amount"] == rendered
    assert isinstance(event.amount, Decimal)


def test_naive_datetimes_are_rejected() -> None:
    """Every date-time field rejects naive values."""
    naive = datetime(2026, 10, 19, 12, 0)
    assert rejected(financial, event_date=naive).field == "event_date"
    assert rejected(non_financial, event_date=naive).field == "event_date"
    assert (
        rejected(CustomerRecord, customer_ref="C-1", registration_date=naive).field
        == "registration_date"
    )
    assert rejected(financial, event_date=date(2026, 10, 19)).field == "event_date"


def test_required_event_date_cannot_be_none() -> None:
    """event_date is required."""
    assert rejected(non_financial, event_date=None).field == "event_date"


def test_date_fields_reject_datetimes_and_strings() -> None:
    """Calendar date fields accept only datetime.date."""
    assert rejected(CustomerRecord, customer_ref="C-1", birth_date=WHEN).field == "birth_date"
    assert (
        rejected(CustomerRecord, customer_ref="C-1", birth_date="1990-01-02").field == "birth_date"
    )


@pytest.mark.parametrize("country", ["deu", "DE", "DEUT", "D1U", "  DEU"])
def test_country_codes_must_be_alpha3_upper_case(country: str) -> None:
    """Country codes are upper-case ISO 3166-1 alpha-3.

    Args:
        country: The invalid code.
    """
    assert rejected(CustomerRecord, customer_ref="C-1", nationality=country).field == "nationality"
    assert rejected(Counterparty, country=country).field == "country"


@pytest.mark.parametrize("address", ["192.0.2.300", "example.com", "fe80::1%eth0", " 192.0.2.1"])
def test_invalid_ip_addresses_are_rejected(address: str) -> None:
    """IP addresses are validated with the ipaddress module.

    Args:
        address: The invalid address.
    """
    assert rejected(non_financial, ip_address=address).field == "ip_address"


def test_valid_ip_addresses_are_accepted() -> None:
    """IPv4 and IPv6 addresses are accepted unchanged."""
    assert non_financial(ip_address="203.0.113.7").ip_address == "203.0.113.7"
    assert non_financial(ip_address="2001:db8::1").ip_address == "2001:db8::1"


def test_enum_fields_accept_members_and_raw_values() -> None:
    """Raw values are coerced to enum members; unknown values are rejected."""
    event = financial(io_type=0, channel=3)
    assert event.io_type is IoType.CREDIT
    assert event.channel is Channel.ANDROID
    raw_type: Any = 2
    raw_gender: Any = "female"
    customer = CustomerRecord(customer_ref="C-1", customer_type=raw_type, gender=raw_gender)
    assert customer.customer_type is CustomerType.CORPORATE
    assert customer.gender is Gender.FEMALE
    assert rejected(financial, io_type=2).field == "io_type"
    assert rejected(financial, io_type=True).field == "io_type"
    assert rejected(financial, io_type=None).field == "io_type"
    assert rejected(non_financial, channel=9).field == "channel"
    assert rejected(CustomerRecord, customer_ref="C-1", gender="Male").field == "gender"


def test_metadata_must_be_json_data() -> None:
    """Metadata and geo hold JSON objects or arrays of plain JSON data."""
    cycle: list[Any] = []
    cycle.append(cycle)
    for value in ({"tags": {"a", "b"}}, {"ratio": float("nan")}, {1: "x"}, "text", cycle):
        assert rejected(non_financial, metadata=value).field == "metadata"
    assert rejected(non_financial, geo={"at": WHEN}).field == "geo"
    shared = {"k": 1}
    event = non_financial(metadata={"a": shared, "b": shared, "c": (1, 2.5, None, True)})
    assert event.metadata is not None


def test_extra_keys_must_not_collide() -> None:
    """Extra keys may not shadow record fields or server-owned keys."""
    for key in ("amount", "source_holder", "destination_kind", "is_recover", "score", "id"):
        assert rejected(financial, extra={key: "x"}).field == "extra"
    for key in ("customer_ref", "migration", "sanction_score"):
        assert rejected(CustomerRecord, customer_ref="C-1", extra={key: 1}).field == "extra"
    assert rejected(non_financial, extra={"": 1}).field == "extra"
    assert rejected(non_financial, extra={"loyalty": object()}).field == "extra"
    assert rejected(non_financial, extra=[("loyalty", 1)]).field == "extra"


def test_flags_must_be_bools() -> None:
    """migration, is_recover and originator_verified accept only bools."""
    assert rejected(CustomerRecord, customer_ref="C-1", migration=1).field == "migration"
    assert rejected(financial, is_recover="yes").field == "is_recover"
    assert rejected(financial, originator_verified=0).field == "originator_verified"


def test_nested_objects_must_have_the_right_type() -> None:
    """source, destination and the declaration take their value objects only."""
    assert rejected(financial, source={"kind": "own_customer"}).field == "source"
    assert rejected(financial, counterparty_declaration={"name": "x"}).field == (
        "counterparty_declaration"
    )
    assert rejected(Counterparty, holder="H" * 129).field == "holder"
    assert rejected(Counterparty, kind="bank").field == "kind"
    assert rejected(CounterpartyDeclaration, declared_at=datetime(2026, 1, 1)).field == (
        "declared_at"
    )


def test_records_are_frozen() -> None:
    """Records cannot be changed after validation."""
    event = financial()
    attribute = "amount"
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(event, attribute, Decimal("-5"))


def test_financial_payload_rendering() -> None:
    """Every field kind renders to its documented JSON form."""
    event = financial(
        event_date=datetime(2026, 10, 19, 15, 0, 0, 125000, tzinfo=PLUS_THREE),
        amount=Decimal("9000"),
        fee_amount=Decimal("1.5"),
        fee_currency="EUR",
        channel=Channel.IOS,
        ip_address="203.0.113.7",
        status="success",
        metadata={"asset": "fake-asset"},
        source=Counterparty(kind=CounterpartyKind.OWN_CUSTOMER, ref="C-TEST-0001"),
        destination=Counterparty(
            kind=CounterpartyKind.FINANCIAL_INSTITUTION,
            holder="Jane Example",
            address="XX00TEST0000000000000001",
            institution="Example Bank",
            institution_code="EXAMPLEXXX",
            country="DEU",
        ),
        originator_verified=False,
        counterparty_declaration=CounterpartyDeclaration(
            name="Jane Example", declared_at=datetime(2026, 10, 1, 3, 0, tzinfo=PLUS_THREE)
        ),
        is_recover=True,
        extra={"loyalty_tier": "gold"},
    )
    assert event.to_payload() == {
        "customer_ref": "C-TEST-0001",
        "event_slug": "transfer_out",
        "transaction_ref": "TX-TEST-0001",
        "event_date": "2026-10-19T12:00:00.125000+00:00",
        "status": "success",
        "ip_address": "203.0.113.7",
        "channel": 4,
        "metadata": {"asset": "fake-asset"},
        "amount": "9000.00",
        "currency": "EUR",
        "io_type": 1,
        "fee_amount": "1.50",
        "fee_currency": "EUR",
        "originator_verified": False,
        "counterparty_declaration": {
            "name": "Jane Example",
            "declared_at": "2026-10-01T00:00:00+00:00",
        },
        "source_kind": "own_customer",
        "source_ref": "C-TEST-0001",
        "destination_kind": "financial_institution",
        "destination_holder": "Jane Example",
        "destination_address": "XX00TEST0000000000000001",
        "destination_institution": "Example Bank",
        "destination_institution_code": "EXAMPLEXXX",
        "destination_country": "DEU",
        "is_recover": True,
        "loyalty_tier": "gold",
    }


def test_flags_are_omitted_unless_true() -> None:
    """is_recover and migration are only sent when set."""
    assert "is_recover" not in financial().to_payload()
    assert "migration" not in CustomerRecord(customer_ref="C-1").to_payload()
    assert CustomerRecord(customer_ref="C-1", migration=True).to_payload()["migration"] is True


def test_customer_payload_rendering() -> None:
    """Customer dates, date-times and enums render as documented; None is omitted."""
    customer = CustomerRecord(
        customer_ref="C-TEST-0001",
        customer_type=CustomerType.INDIVIDUAL,
        first_name="Jane",
        last_name="Example",
        gender=Gender.FEMALE,
        birth_date=date(1990, 4, 17),
        nationality="DEU",
        email="jane@example.com",
        ip_address="198.51.100.4",
        registration_date=datetime(2026, 1, 15, 9, 30, tzinfo=UTC),
        founders=[{"role": "ubo", "full_name": "John Example", "share_pct": 60}],
    )
    assert customer.to_payload() == {
        "customer_ref": "C-TEST-0001",
        "customer_type": 1,
        "first_name": "Jane",
        "last_name": "Example",
        "gender": "female",
        "birth_date": "1990-04-17",
        "nationality": "DEU",
        "email": "jane@example.com",
        "ip_address": "198.51.100.4",
        "registration_date": "2026-01-15T09:30:00+00:00",
        "founders": [{"role": "ubo", "full_name": "John Example", "share_pct": 60}],
    }


def test_same_instant_renders_identical_text_whatever_the_offset() -> None:
    """Date-times are rendered in UTC, so equal instants give equal bytes."""
    in_utc = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
    in_plus_three = datetime(2026, 10, 19, 15, 0, tzinfo=PLUS_THREE)
    in_minus_five = datetime(2026, 10, 19, 7, 0, tzinfo=timezone(timedelta(hours=-5)))
    rendered = {
        json.dumps(financial(event_date=moment).to_payload(), sort_keys=True)
        for moment in (in_utc, in_plus_three, in_minus_five)
    }
    assert len(rendered) == 1
    customer = CustomerRecord(customer_ref="C-1", registration_date=in_minus_five)
    assert customer.to_payload()["registration_date"] == "2026-10-19T12:00:00+00:00"
    precise = in_plus_three.replace(microsecond=7)
    assert financial(event_date=precise).to_payload()["event_date"] == (
        "2026-10-19T12:00:00.000007+00:00"
    )


def test_datetimes_outside_the_utc_range_are_rejected() -> None:
    """A date-time that cannot be expressed in UTC is rejected."""
    earliest = datetime(1, 1, 1, 0, 0, tzinfo=PLUS_THREE)
    assert rejected(financial, event_date=earliest).field == "event_date"


def test_unchanged_records_render_identical_bytes() -> None:
    """Equal records produce equal payloads, with no volatile values added."""
    first = json.dumps(financial(metadata={"b": 1, "a": 2}).to_payload(), sort_keys=True)
    second = json.dumps(financial(metadata={"a": 2, "b": 1}).to_payload(), sort_keys=True)
    assert first == second


def test_counterparty_payload_uses_the_side_prefix() -> None:
    """Counterparty.to_payload prefixes every key with its side."""
    side = Counterparty(kind=CounterpartyKind.UNHOSTED_WALLET, address="fake-wallet-0001")
    assert side.to_payload("destination") == {
        "destination_kind": "unhosted_wallet",
        "destination_address": "fake-wallet-0001",
    }
    side_name: Any = "middle"
    with pytest.raises(InvalidArgumentError) as caught:
        side.to_payload(side_name)
    assert isinstance(caught.value, ValueError)


def test_non_financial_payload_rendering() -> None:
    """Non-financial events render their shared fields only."""
    event = non_financial(device_id="device-0001", channel=Channel.ANDROID, geo={"lat": 0.0})
    assert event.to_payload() == {
        "customer_ref": "C-TEST-0001",
        "event_slug": "failed_login",
        "transaction_ref": "EVT-TEST-0001",
        "event_date": "2026-10-19T12:00:00+00:00",
        "device_id": "device-0001",
        "channel": 3,
        "geo": {"lat": 0.0},
    }
