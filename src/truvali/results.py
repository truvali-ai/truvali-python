"""Typed results of the Truvali API calls.

Upsert results come back one per record, in the order the records were sent,
skipped records included. Branch on :attr:`RecordResult.action` (or the
:attr:`RecordResult.ok` shortcut), not on the decision: a skipped record carries
``REJECT`` only so that clients ignoring ``error`` still fail safe.

Responses without the documented shape raise
:class:`~truvali.UnexpectedResponseError`. Values a newer server may add are
tolerated instead: an unknown action, decision or delivery status becomes the
``UNKNOWN`` member of its enum, with the original text kept in ``raw``, and
unknown keys and dataset names are kept as received.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, ClassVar, Generic, TypeVar

from truvali import _parsing
from truvali._enum import TextEnum
from truvali.errors import UnexpectedResponseError
from truvali.webhooks import WebhookEnvelope

CONCURRENT_DUPLICATE = "concurrent_duplicate"

_CUSTOMER_MISSING = re.compile(r"customer_ref '.*?' not found")
_ONLY_CUSTOMER_MISSING = re.compile(r"Valid customer_ref '[^']*' not found\.?")

E = TypeVar("E", bound=Enum)
R = TypeVar("R", bound="RecordResult")


class UpsertAction(TextEnum):
    """What the server did with a record.

    ``UNKNOWN`` stands for an action this SDK version does not know; such a
    record is neither ok nor skipped, and ``raw["action"]`` holds the text.
    """

    CREATED = "created"
    UPDATED = "updated"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"


class Decision(TextEnum):
    """Risk decision of a record.

    ``UNKNOWN`` stands for a decision this SDK version does not know;
    ``raw["decision"]`` holds the text.
    """

    ACCEPT = "ACCEPT"
    REVIEW = "REVIEW"
    REJECT = "REJECT"
    UNKNOWN = "UNKNOWN"


class DeliveryStatus(TextEnum):
    """Delivery state of an event feed entry.

    ``UNKNOWN`` stands for a status this SDK version does not know;
    :attr:`Delivery.raw` holds the original object.
    """

    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    DEAD = "dead"
    UNKNOWN = "unknown"


_STORED_ACTIONS = frozenset({UpsertAction.CREATED, UpsertAction.UPDATED})


class Dataset(TextEnum):
    """A record collection counted by the reconciliation endpoint.

    Members compare equal to the plain names, so they can be used to look up
    :attr:`ReconciliationReport.datasets`.
    """

    CUSTOMER = "customer"
    FINANCIAL = "financial"
    NON_FINANCIAL = "nonfinancial"


class DateBasis(TextEnum):
    """The date a reconciliation count is based on."""

    CREATE_DATE = "create_date"
    EVENT_DATE = "event_date"
    REGISTRATION_DATE = "registration_date"


def _member(choices: type[E], value: str, unknown: E) -> E:
    """Convert a raw string into an enum member, tolerating unknown values.

    Args:
        choices: The enum class.
        value: The raw string.
        unknown: The member returned for a value the enum does not know.

    Returns:
        The matching member, or ``unknown``.
    """
    try:
        return choices(value)
    except ValueError:
        return unknown


def _unexpected(what: str, exc: Exception) -> UnexpectedResponseError:
    """Build the error raised for a malformed response.

    Args:
        what: Which part of the response was malformed.
        exc: The underlying shape error.

    Returns:
        The error to raise.
    """
    return UnexpectedResponseError(f"Malformed {what} in the response: {exc}")


@dataclass(frozen=True, slots=True, kw_only=True)
class Challenge:
    """Extra verification a rule requires before a transaction may proceed.

    A challenge holds the event at ``REVIEW`` until it is resolved; the outcome
    arrives through the ``challenge_resolved`` webhook.

    Attributes:
        id: Challenge id, used to open the verification session.
        type: Challenge type, for example ``"verification_flow"``.
        target: What has to be verified, for example ``"step_up_selfie"``.
        status: ``pending``, ``accepted``, ``rejected`` or ``review``.
        rule: Name of the rule that asked for it.
        session_id: Verification session to complete, when already open.
        complete_by: Deadline of the verification, when there is one.
    """

    id: str
    type: str | None
    target: str | None
    status: str | None
    rule: str | None
    session_id: str | None
    complete_by: datetime | None

    @classmethod
    def _from_api(cls, document: Mapping[str, Any]) -> Challenge:
        """Build a challenge from its JSON object.

        Args:
            document: The ``challenge`` object of a record result.

        Returns:
            The challenge.

        Raises:
            UnexpectedResponseError: If the object is malformed.
        """
        try:
            return cls(
                id=_parsing.required_str(document, "id"),
                type=_parsing.optional_str(document, "type"),
                target=_parsing.optional_str(document, "target"),
                status=_parsing.optional_str(document, "status"),
                rule=_parsing.optional_str(document, "rule"),
                session_id=_parsing.optional_str(document, "session_id"),
                complete_by=_parsing.parse_datetime(document.get("complete_by"), "complete_by"),
            )
        except _parsing.ShapeError as exc:
            raise _unexpected("challenge", exc) from exc


def _record_values(
    item: object, ref_key: str, request_id: str | None
) -> tuple[Mapping[str, Any], dict[str, Any]]:
    """Read the keys every record result shares.

    Args:
        item: One element of ``data.results``.
        ref_key: ``"customer_ref"`` or ``"transaction_ref"``.
        request_id: Request id of the call.

    Returns:
        The result object and the keyword arguments of :class:`RecordResult`.

    Raises:
        ShapeError: If a key is missing or has the wrong type.
    """
    document = _parsing.mapping(item, "result")
    action = _member(UpsertAction, _parsing.required_str(document, "action"), UpsertAction.UNKNOWN)
    changed = _parsing.optional_bool(document, "changed")
    decision = _parsing.optional_str(document, "decision")
    challenge = _parsing.optional_mapping(document, "challenge")
    return document, {
        "ref": _parsing.optional_str(document, ref_key),
        "id": _parsing.optional_str(document, "id"),
        "action": action,
        "changed": action in _STORED_ACTIONS if changed is None else changed,
        "score": _parsing.optional_decimal(document, "score"),
        "decision": None if decision is None else _member(Decision, decision, Decision.UNKNOWN),
        "rule_check": _parsing.optional_bool(document, "rule_check"),
        "log_id": _parsing.optional_str(document, "log_id"),
        "warnings": _parsing.str_tuple(document, "warnings"),
        "error": _parsing.optional_str(document, "error"),
        "challenge": None if challenge is None else Challenge._from_api(challenge),
        "request_id": request_id,
        "raw": document,
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordResult:
    """Outcome of one record of an upsert call.

    Used as is for non-financial events; customers and financial events add their
    own keys in :class:`CustomerResult` and :class:`FinancialResult`.

    Events whose ``event_date`` is 24 hours old or more are stored without rule
    evaluation (``rule_check`` is ``False``). A new non-financial event of that age
    comes back with score 0 and ``ACCEPT``; a new financial event of that age comes
    back with ``decision`` ``None``, unless it was sent with ``is_recover``. An
    unchanged resend is not evaluated either and returns the stored score and
    decision with ``changed`` and ``rule_check`` ``False``.

    Attributes:
        ref: The ``customer_ref`` or ``transaction_ref`` of the record, as stored.
        id: Truvali's id of the record; ``None`` when skipped.
        action: Created, updated, skipped, or ``UNKNOWN`` for an action this SDK
            version does not know (the text stays in ``raw``).
        changed: ``False`` when nothing stored changed (an unchanged resend, not
            evaluated again) and on skipped records.
        score: Risk score 0 to 100; skipped records carry 100 and a new
            non-financial event older than 24 hours carries 0.
        decision: Risk decision. ``None`` only for a new financial event older than
            24 hours; ``UNKNOWN`` for a decision this SDK version does not know.
        rule_check: Whether the rules ran for this record: ``False`` for events 24
            hours old or more and for unchanged resends; ``None`` when absent, as on
            customer results and skipped records.
        log_id: Id of the rule execution log of the last evaluation.
        warnings: Values the server normalised or ignored.
        error: Why the record was skipped; several errors are joined by spaces.
        challenge: Extra verification required by a rule, if any.
        request_id: Request id of the call that returned this result.
        raw: The result object exactly as received.
    """

    ref: str | None
    id: str | None
    action: UpsertAction
    changed: bool
    score: Decimal | None
    decision: Decision | None
    rule_check: bool | None
    log_id: str | None
    warnings: tuple[str, ...]
    error: str | None
    challenge: Challenge | None
    request_id: str | None
    raw: Mapping[str, Any] = field(repr=False, compare=False)

    _REF_KEY: ClassVar[str] = "transaction_ref"

    @classmethod
    def _from_api(cls, item: Mapping[str, Any], *, request_id: str | None = None) -> RecordResult:
        """Build a result from one element of ``data.results``.

        Args:
            item: The result object.
            request_id: Request id of the call.

        Returns:
            The parsed result.

        Raises:
            UnexpectedResponseError: If the object is malformed.
        """
        try:
            _, values = _record_values(item, cls._REF_KEY, request_id)
        except _parsing.ShapeError as exc:
            raise _unexpected("record result", exc) from exc
        return cls(**values)

    @property
    def ok(self) -> bool:
        """Whether the record was stored (created or updated).

        Returns:
            ``True`` for created and updated records; ``False`` for skipped records
            and for an unknown action.
        """
        return self.action in _STORED_ACTIONS

    @property
    def skipped(self) -> bool:
        """Whether the record was rejected and nothing was stored.

        Returns:
            ``True`` for skipped records; ``False`` for an unknown action.
        """
        return self.action is UpsertAction.SKIPPED

    @property
    def customer_missing(self) -> bool:
        """Whether one of the errors is that the referenced customer does not exist.

        Returns:
            ``True`` when ``error`` contains the ``customer_ref ... not found`` text.
        """
        return self.error is not None and _CUSTOMER_MISSING.search(self.error) is not None

    @property
    def concurrent_duplicate(self) -> bool:
        """Whether the record lost a create race with a concurrent request.

        Returns:
            ``True`` when ``error`` is the ``concurrent_duplicate`` code.
        """
        return self.error is not None and self.error.strip() == CONCURRENT_DUPLICATE

    @property
    def retryable(self) -> bool:
        """Whether resending the same record unchanged can succeed.

        True for a skipped record that lost a create race, and for a skipped record
        whose only error is a missing customer (send the customer first). False
        when any other error is present and for an unknown action.

        Returns:
            ``True`` when a resend of the same record is worthwhile.
        """
        if self.action is not UpsertAction.SKIPPED or self.error is None:
            return False
        text = self.error.strip()
        return text == CONCURRENT_DUPLICATE or _ONLY_CUSTOMER_MISSING.fullmatch(text) is not None


@dataclass(frozen=True, slots=True, kw_only=True)
class CustomerResult(RecordResult):
    """Outcome of one customer record, with the screening and scoring keys.

    Attributes:
        sanction_score: The customer's own screening score, 0 to 100.
        sanction_topic: Topics of the screening matches.
        sanction_status: ``screened``, ``error``, ``not_due``,
            ``skipped_migration``, ``not_configured`` or ``skipped_no_name``;
            ``None`` on skipped records.
        sanction_screened_at: Time of the last successful screening.
        subjects: Screening results of a company's founders entries.
        subjects_sanction_status: ``screened``, ``error`` or ``incomplete``.
        subjects_unscreened_count: Founders entries that could not be screened.
        ip_address_score: Risk score of the registration IP address.
        email_risk_score: Risk score of the e-mail address.
    """

    sanction_score: Decimal | None
    sanction_topic: tuple[str, ...]
    sanction_status: str | None
    sanction_screened_at: datetime | None
    subjects: tuple[Mapping[str, Any], ...]
    subjects_sanction_status: str | None
    subjects_unscreened_count: int | None
    ip_address_score: Decimal | None
    email_risk_score: Decimal | None

    _REF_KEY: ClassVar[str] = "customer_ref"

    @classmethod
    def _from_api(cls, item: Mapping[str, Any], *, request_id: str | None = None) -> CustomerResult:
        """Build a customer result from one element of ``data.results``.

        Args:
            item: The result object.
            request_id: Request id of the call.

        Returns:
            The parsed result.

        Raises:
            UnexpectedResponseError: If the object is malformed.
        """
        try:
            document, values = _record_values(item, cls._REF_KEY, request_id)
            return cls(
                **values,
                sanction_score=_parsing.optional_decimal(document, "sanction_score"),
                sanction_topic=_parsing.str_tuple(document, "sanction_topic"),
                sanction_status=_parsing.optional_str(document, "sanction_status"),
                sanction_screened_at=_parsing.parse_datetime(
                    document.get("sanction_screened_at"), "sanction_screened_at"
                ),
                subjects=_parsing.mapping_tuple(document, "subjects"),
                subjects_sanction_status=_parsing.optional_str(
                    document, "subjects_sanction_status"
                ),
                subjects_unscreened_count=_parsing.optional_int(
                    document, "subjects_unscreened_count"
                ),
                ip_address_score=_parsing.optional_decimal(document, "ip_address_score"),
                email_risk_score=_parsing.optional_decimal(document, "email_risk_score"),
            )
        except _parsing.ShapeError as exc:
            raise _unexpected("customer result", exc) from exc


@dataclass(frozen=True, slots=True, kw_only=True)
class FinancialResult(RecordResult):
    """Outcome of one financial event, with Travel Rule and counterparty screening.

    Attributes:
        travel_rule: ``status`` (complete, incomplete, not_required), ``missing``
            item codes, ``regime`` and the matched inbound ``message``, if any.
        counterparty_sanction: Name screening of an external counterparty: ``score``
            (``None`` when not screened) and ``side``.
        counterparty_wallet_sanction: Wallet address screening, same keys.
    """

    travel_rule: Mapping[str, Any] | None
    counterparty_sanction: Mapping[str, Any] | None
    counterparty_wallet_sanction: Mapping[str, Any] | None

    @classmethod
    def _from_api(
        cls, item: Mapping[str, Any], *, request_id: str | None = None
    ) -> FinancialResult:
        """Build a financial result from one element of ``data.results``.

        Args:
            item: The result object.
            request_id: Request id of the call.

        Returns:
            The parsed result.

        Raises:
            UnexpectedResponseError: If the object is malformed.
        """
        try:
            document, values = _record_values(item, cls._REF_KEY, request_id)
            return cls(
                **values,
                travel_rule=_parsing.optional_mapping(document, "travel_rule"),
                counterparty_sanction=_parsing.optional_mapping(document, "counterparty_sanction"),
                counterparty_wallet_sanction=_parsing.optional_mapping(
                    document, "counterparty_wallet_sanction"
                ),
            )
        except _parsing.ShapeError as exc:
            raise _unexpected("financial result", exc) from exc


@dataclass(frozen=True, slots=True)
class UpsertResponse(Generic[R]):
    """Response of an upsert call: one result per record, in input order.

    Iterating yields the results; ``len()`` is the number of records sent.

    Attributes:
        request_id: Request id of the call; quote it to support.
        message: The server's summary message.
        results: The record results, in the order the records were sent.
    """

    request_id: str | None
    message: str
    results: tuple[R, ...]
    _by_ref: dict[str, R] | None = field(default=None, init=False, repr=False, compare=False)

    @classmethod
    def _from_api(
        cls,
        data: Mapping[str, Any],
        *,
        request_id: str | None,
        message: str,
        expected: int,
        parse: Callable[..., R],
    ) -> UpsertResponse[R]:
        """Build the response from the ``data`` object of an upsert call.

        Args:
            data: The ``data`` object of the envelope.
            request_id: Request id of the call.
            message: The envelope message.
            expected: Number of records that were sent.
            parse: Builds one result; called as ``parse(item, request_id=...)``.

        Returns:
            The response.

        Raises:
            UnexpectedResponseError: If ``data.results`` is missing, malformed or its
                length differs from ``expected``.
        """
        items = data.get("results")
        if not isinstance(items, list):
            raise UnexpectedResponseError(
                "The upsert response has no results list.", request_id=request_id
            )
        if len(items) != expected:
            raise UnexpectedResponseError(
                f"The upsert response has {len(items)} results for {expected} records.",
                request_id=request_id,
            )
        try:
            results = tuple(parse(item, request_id=request_id) for item in items)
        except UnexpectedResponseError as exc:
            if exc.request_id is None:
                exc.request_id = request_id
            raise
        return cls(request_id=request_id, message=message, results=results)

    def __len__(self) -> int:
        """Return the number of results.

        Returns:
            The number of records sent.
        """
        return len(self.results)

    def __iter__(self) -> Iterator[R]:
        """Iterate over the results in input order.

        Returns:
            An iterator over the results.
        """
        return iter(self.results)

    def __getitem__(self, index: int) -> R:
        """Return the result at a position.

        Args:
            index: Position of the record in the batch.

        Returns:
            The result of that record.
        """
        return self.results[index]

    def by_ref(self, ref: str) -> R | None:
        """Look a result up by its ``customer_ref`` or ``transaction_ref``.

        The index is built on the first call. When a batch carried the same ref
        more than once, the last result (the final state) is returned.

        Args:
            ref: The reference to look up.

        Returns:
            The result, or ``None`` when no result carries that ref.
        """
        index = self._by_ref
        if index is None:
            index = {result.ref: result for result in self.results if result.ref is not None}
            object.__setattr__(self, "_by_ref", index)
        return index.get(ref)

    @property
    def all_ok(self) -> bool:
        """Whether every record was stored.

        Returns:
            ``True`` when every record was created or updated.
        """
        return all(result.ok for result in self.results)

    @property
    def skipped(self) -> tuple[R, ...]:
        """The results of the skipped records.

        Returns:
            The skipped results, in input order.
        """
        return tuple(result for result in self.results if result.skipped)


@dataclass(frozen=True, slots=True)
class AuthCheck:
    """Answer of the token check endpoint.

    Attributes:
        client: Name of the Source Service the token belongs to.
        session_id: Id of the API session behind the token.
    """

    client: str
    session_id: str

    @classmethod
    def _from_api(cls, data: Mapping[str, Any]) -> AuthCheck:
        """Build the answer from the ``data`` object.

        Args:
            data: The ``data`` object of the envelope.

        Returns:
            The parsed answer.

        Raises:
            UnexpectedResponseError: If a key is missing.
        """
        try:
            return cls(
                client=_parsing.required_str(data, "client"),
                session_id=_parsing.required_str(data, "session_id"),
            )
        except _parsing.ShapeError as exc:
            raise _unexpected("token check", exc) from exc


def _count_table(value: object, name: str) -> dict[str, int]:
    """Read a ``{label: count}`` object.

    Args:
        value: The raw object.
        name: Name of the object, used in the error message.

    Returns:
        The counts by label.

    Raises:
        ShapeError: If the value is not an object of integers.
    """
    table = _parsing.mapping(value, name)
    counts: dict[str, int] = {}
    for label, count in table.items():
        if isinstance(count, bool) or not isinstance(count, int):
            raise _parsing.ShapeError(f"'{name}.{label}' must be an integer.")
        counts[str(label)] = count
    return counts


@dataclass(frozen=True, slots=True, kw_only=True)
class ReconciliationWindow:
    """The window a reconciliation report counted.

    Attributes:
        period: ``total``, ``day`` or ``range``.
        date_from: First local day counted, or ``None`` for ``total``.
        date_to: Last local day counted (inclusive), or ``None`` for ``total``.
        date_field: The date field that was asked for.
        timezone: IANA time zone of the local days.
    """

    period: str
    date_from: date | None
    date_to: date | None
    date_field: str
    timezone: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DatasetCount:
    """Counts of one dataset in a reconciliation report.

    Attributes:
        count: Number of records in the window.
        date_field_used: The date field actually used for this dataset.
        note: Explanation when another date field than requested was used.
        scope: What the count covers, when the server states it.
        breakdown: Counts by ``status``, ``decision``, ``customer_type`` or
            ``io_type`` and, for customers, ``availability``; empty when disabled.
        by_day: Counts per local day; empty unless grouped by day.
    """

    count: int
    date_field_used: str
    note: str | None
    scope: str | None
    breakdown: Mapping[str, Mapping[str, int]]
    by_day: Mapping[date, int]

    @classmethod
    def _from(cls, document: Mapping[str, Any], name: str) -> DatasetCount:
        """Read one dataset entry.

        Args:
            document: The dataset object.
            name: Name of the dataset, used in error messages.

        Returns:
            The parsed counts.

        Raises:
            ShapeError: If the object is malformed.
        """
        count = _parsing.optional_int(document, "count")
        if count is None:
            raise _parsing.ShapeError(f"'datasets.{name}.count' is required.")
        breakdown = _parsing.optional_mapping(document, "breakdown") or {}
        by_day = _count_table(document.get("by_day") or {}, f"datasets.{name}.by_day")
        return cls(
            count=count,
            date_field_used=_parsing.required_str(document, "date_field_used"),
            note=_parsing.optional_str(document, "note"),
            scope=_parsing.optional_str(document, "scope"),
            breakdown={
                str(label): _count_table(table, f"datasets.{name}.breakdown.{label}")
                for label, table in breakdown.items()
            },
            by_day={
                _parsing.require_date(day, f"datasets.{name}.by_day"): total
                for day, total in by_day.items()
            },
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ReconciliationReport:
    """Record counts of your Source Service, for reconciliation.

    Attributes:
        data_source_id: Id of your Source Service.
        data_source_name: Name of your Source Service.
        generated_at: When the server computed the report.
        window: The counted window.
        datasets: Counts per dataset, keyed by the name the server sent.
            :class:`Dataset` members work as keys, for example
            ``report.datasets[Dataset.FINANCIAL]``; names a newer server may add
            are kept as plain strings.
        raw: The ``data`` object exactly as received.
    """

    data_source_id: str
    data_source_name: str | None
    generated_at: datetime | None
    window: ReconciliationWindow
    datasets: Mapping[str, DatasetCount]
    raw: Mapping[str, Any] = field(repr=False, compare=False)

    @classmethod
    def _from_api(cls, data: Mapping[str, Any]) -> ReconciliationReport:
        """Build the report from the ``data`` object.

        Args:
            data: The ``data`` object of the envelope.

        Returns:
            The parsed report.

        Raises:
            UnexpectedResponseError: If the object is malformed.
        """
        try:
            source = _parsing.mapping(data.get("data_source"), "data_source")
            window = _parsing.mapping(data.get("window"), "window")
            datasets = _parsing.mapping(data.get("datasets"), "datasets")
            return cls(
                data_source_id=_parsing.required_str(source, "id"),
                data_source_name=_parsing.optional_str(source, "name"),
                generated_at=_parsing.parse_datetime(data.get("generated_at"), "generated_at"),
                window=ReconciliationWindow(
                    period=_parsing.required_str(window, "period"),
                    date_from=_parsing.parse_date(window.get("date_from"), "date_from"),
                    date_to=_parsing.parse_date(window.get("date_to"), "date_to"),
                    date_field=_parsing.required_str(window, "date_field"),
                    timezone=_parsing.required_str(window, "timezone"),
                ),
                datasets={
                    str(name): DatasetCount._from(
                        _parsing.mapping(entry, f"datasets.{name}"), str(name)
                    )
                    for name, entry in datasets.items()
                },
                raw=data,
            )
        except _parsing.ShapeError as exc:
            raise _unexpected("reconciliation report", exc) from exc


@dataclass(frozen=True, slots=True, kw_only=True)
class Delivery:
    """Delivery state of one event feed entry.

    Attributes:
        status: Pending, success, failed (will be retried), dead (given up), or
            ``UNKNOWN`` for a status this SDK version does not know.
        attempts: Attempts of the current delivery cycle.
        last_attempt_at: Time of the last attempt, or ``None`` before the first.
        endpoint_id: Id of the receiving callback endpoint; ``None`` when removed.
        endpoint_name: Name of the receiving callback endpoint.
        raw: The ``delivery`` object exactly as received.
    """

    status: DeliveryStatus
    attempts: int
    last_attempt_at: datetime | None
    endpoint_id: str | None
    endpoint_name: str | None
    raw: Mapping[str, Any] = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class FeedEvent(WebhookEnvelope):
    """An event read back from the event feed: the envelope plus its delivery state.

    It is a :class:`~truvali.WebhookEnvelope` whose ``attempt`` is ``None``, so the
    same handler can process webhook deliveries and feed entries.

    Attributes:
        delivery: Delivery state of this copy of the event.
    """

    delivery: Delivery

    @classmethod
    def _from_feed(cls, document: object) -> FeedEvent:
        """Read one entry of ``data.events``.

        Args:
            document: The event object.

        Returns:
            The parsed event.

        Raises:
            ShapeError: If the object is malformed.
        """
        event = _parsing.mapping(document, "event")
        values = cls._envelope_values(event)
        delivery = _parsing.mapping(event.get("delivery"), "delivery")
        endpoint = _parsing.optional_mapping(delivery, "endpoint")
        attempts = _parsing.optional_int(delivery, "attempts")
        return cls(
            **values,
            delivery=Delivery(
                status=_member(
                    DeliveryStatus,
                    _parsing.required_str(delivery, "status"),
                    DeliveryStatus.UNKNOWN,
                ),
                attempts=attempts or 0,
                last_attempt_at=_parsing.parse_datetime(
                    delivery.get("last_attempt_at"), "last_attempt_at"
                ),
                endpoint_id=None if endpoint is None else _parsing.optional_str(endpoint, "id"),
                endpoint_name=(
                    None if endpoint is None else _parsing.optional_str(endpoint, "name")
                ),
                raw=delivery,
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class EventPage:
    """One page of the event feed.

    Iterating yields the events in creation order.

    Attributes:
        events: The events of this page.
        next_cursor: Cursor of the next page; ``None`` at the end of the feed.
        last_cursor: Cursor after the last event read so far. Store it and pass it
            as ``cursor`` later to read only newer events.
    """

    events: tuple[FeedEvent, ...]
    next_cursor: str | None
    last_cursor: str | None

    @classmethod
    def _from_api(cls, data: Mapping[str, Any]) -> EventPage:
        """Build a page from the ``data`` object.

        Args:
            data: The ``data`` object of the envelope.

        Returns:
            The parsed page.

        Raises:
            UnexpectedResponseError: If the object is malformed.
        """
        try:
            events = data.get("events")
            if not isinstance(events, list):
                raise _parsing.ShapeError("'events' must be a list.")
            return cls(
                events=tuple(FeedEvent._from_feed(event) for event in events),
                next_cursor=_parsing.optional_str(data, "next_cursor") or None,
                last_cursor=_parsing.optional_str(data, "last_cursor") or None,
            )
        except _parsing.ShapeError as exc:
            raise _unexpected("event feed page", exc) from exc

    @property
    def has_more(self) -> bool:
        """Whether more events follow this page.

        Returns:
            ``True`` when ``next_cursor`` is set.
        """
        return self.next_cursor is not None

    def __len__(self) -> int:
        """Return the number of events on the page.

        Returns:
            The number of events.
        """
        return len(self.events)

    def __iter__(self) -> Iterator[FeedEvent]:
        """Iterate over the events in creation order.

        Returns:
            An iterator over the events.
        """
        return iter(self.events)
