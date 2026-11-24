"""The synchronous Truvali API client."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterable, Iterator, Sequence
from datetime import date, datetime
from datetime import timezone as dt_timezone
from enum import Enum
from types import TracebackType
from typing import Any, TypeVar

import requests

from truvali.config import SERVER_MAX_RECORDS, TruvaliConfig
from truvali.errors import (
    ConfigurationError,
    InvalidArgumentError,
    PayloadTooLargeError,
    RecordValidationError,
)
from truvali.records import CustomerRecord, FinancialEvent, NonFinancialEvent
from truvali.results import (
    AuthCheck,
    CustomerResult,
    Dataset,
    DateBasis,
    EventPage,
    FeedEvent,
    FinancialResult,
    ReconciliationReport,
    RecordResult,
    UpsertResponse,
)
from truvali.transport import TokenStore, Transport, validate_timeout

CUSTOMER_UPSERT_PATH = "/api/customer/upsert/"
FINANCIAL_UPSERT_PATH = "/api/transaction/financial/upsert/"
NON_FINANCIAL_UPSERT_PATH = "/api/transaction/non-financial/upsert/"
RECONCILIATION_PATH = "/api/reconciliation/"
EVENTS_PATH = "/api/engine/events/"
AUTH_CHECK_PATH = "/api/merchant/auth/check/"

MAX_BODY_BYTES = 1_048_576
MAX_FEED_LIMIT = 500
MAX_DAY_BUCKETS = 366

T = TypeVar("T", bound=RecordResult)
C = TypeVar("C", bound=Enum)


def _encode(payloads: list[dict[str, Any]]) -> bytes:
    """Serialise record payloads into a canonical JSON body.

    Keys are sorted and separators are compact, so equal records always produce
    identical bytes.

    Args:
        payloads: The rendered records.

    Returns:
        The UTF-8 encoded JSON array.
    """
    return json.dumps(
        payloads, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False
    ).encode("utf-8")


def _check_day(name: str, value: object) -> date | None:
    """Validate an optional calendar date argument.

    Args:
        name: Name of the argument.
        value: The value passed.

    Returns:
        The value as a date, or ``None``.

    Raises:
        InvalidArgumentError: If the value is not a ``datetime.date``.
    """
    if value is None:
        return None
    if not isinstance(value, date) or isinstance(value, datetime):
        raise InvalidArgumentError(f"{name} must be a datetime.date, not a datetime or a string.")
    return value


def _choice(choices: type[C], value: object, name: str) -> C:
    """Convert an argument into an enum member with a readable error.

    Args:
        choices: The enum class.
        value: A member or its plain value.
        name: Name of the argument, used in the error message.

    Returns:
        The matching member.

    Raises:
        InvalidArgumentError: If the value matches no member.
    """
    try:
        return choices(value)
    except ValueError as exc:
        allowed = ", ".join(str(member.value) for member in choices)
        raise InvalidArgumentError(f"{name} got {value!r}; expected one of: {allowed}.") from exc


def _reconciliation_params(
    *,
    datasets: Iterable[Dataset] | None,
    day: date | None,
    date_from: date | None,
    date_to: date | None,
    timezone: str | None,
    date_field: DateBasis | None,
    group_by_day: bool,
    breakdown: bool,
) -> dict[str, str]:
    """Validate the reconciliation arguments and build the query parameters.

    Args:
        datasets: Datasets to count; every dataset when ``None``.
        day: A single local day to count.
        date_from: First local day of a range (inclusive).
        date_to: Last local day of a range (inclusive).
        timezone: IANA time zone of the local days.
        date_field: The date the counts are based on.
        group_by_day: Whether to add per-day counts.
        breakdown: Whether to add counts by status, decision and direction.

    Returns:
        The query parameters.

    Raises:
        InvalidArgumentError: If the arguments conflict or are malformed.
    """
    params: dict[str, str] = {}
    if datasets is not None:
        if isinstance(datasets, (str, bytes)):
            raise InvalidArgumentError("datasets must be an iterable of Dataset members.")
        chosen = tuple(dict.fromkeys(_choice(Dataset, item, "datasets") for item in datasets))
        if not chosen:
            raise InvalidArgumentError("datasets must name at least one dataset.")
        params["dataset"] = ",".join(item.value for item in chosen)
    single = _check_day("day", day)
    start = _check_day("date_from", date_from)
    end = _check_day("date_to", date_to)
    if single is not None:
        if start is not None or end is not None:
            raise InvalidArgumentError("Pass either day or date_from and date_to, not both.")
        params["period"] = "day"
        params["date"] = single.isoformat()
        span = 1
    elif start is not None or end is not None:
        if start is None or end is None:
            raise InvalidArgumentError("date_from and date_to must be passed together.")
        if end < start:
            raise InvalidArgumentError("date_to must not be earlier than date_from.")
        params["period"] = "range"
        params["date_from"] = start.isoformat()
        params["date_to"] = end.isoformat()
        span = (end - start).days + 1
    else:
        params["period"] = "total"
        span = 0
    if timezone is not None:
        if not isinstance(timezone, str) or not timezone.strip():
            raise InvalidArgumentError(
                "timezone must be an IANA time zone name such as 'Europe/Berlin'."
            )
        params["timezone"] = timezone
    if date_field is not None:
        params["date_field"] = _choice(DateBasis, date_field, "date_field").value
    if group_by_day:
        if span == 0:
            raise InvalidArgumentError("group_by_day needs a day or a date range.")
        if span > MAX_DAY_BUCKETS:
            raise InvalidArgumentError(f"group_by_day supports at most {MAX_DAY_BUCKETS} days.")
        params["group_by"] = "day"
    params["breakdown"] = "1" if breakdown else "0"
    return params


def _feed_params(
    *,
    topic: str | None,
    since: datetime | None,
    endpoint: str | uuid.UUID | None,
    limit: int | None,
) -> dict[str, str]:
    """Validate the event feed filters and build the query parameters.

    Args:
        topic: Only events carrying this topic.
        since: Only events created at or after this timezone-aware moment; sent
            converted to UTC.
        endpoint: Only the copies for this callback endpoint id.
        limit: Page size from 1 to 500.

    Returns:
        The query parameters, without the cursor.

    Raises:
        InvalidArgumentError: If a filter is malformed.
    """
    params: dict[str, str] = {}
    if topic is not None:
        if not isinstance(topic, str) or not topic.strip():
            raise InvalidArgumentError("topic must be a non-empty string.")
        params["topic"] = topic
    if since is not None:
        if not isinstance(since, datetime) or since.utcoffset() is None:
            raise InvalidArgumentError("since must be a timezone-aware datetime.")
        try:
            params["since"] = since.astimezone(dt_timezone.utc).isoformat()
        except OverflowError as exc:
            raise InvalidArgumentError("since is out of range when converted to UTC.") from exc
    if endpoint is not None:
        try:
            endpoint_id = endpoint if isinstance(endpoint, uuid.UUID) else uuid.UUID(endpoint)
        except (TypeError, ValueError, AttributeError) as exc:
            raise InvalidArgumentError("endpoint must be a callback endpoint id (a UUID).") from exc
        params["endpoint"] = str(endpoint_id)
    if limit is not None:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_FEED_LIMIT
        ):
            raise InvalidArgumentError(f"limit must be an integer from 1 to {MAX_FEED_LIMIT}.")
        params["limit"] = str(limit)
    return params


def _check_cursor(cursor: str | None) -> str | None:
    """Validate an event feed cursor.

    Args:
        cursor: A ``next_cursor`` or ``last_cursor`` of an earlier page, or ``None``.

    Returns:
        The cursor, or ``None``.

    Raises:
        InvalidArgumentError: If the cursor is not a non-empty string.
    """
    if cursor is not None and (not isinstance(cursor, str) or not cursor.strip()):
        raise InvalidArgumentError("cursor must be a non-empty string returned by the feed.")
    return cursor


class TruvaliClient:
    """Synchronous client of the Truvali API.

    Create one client per process and egress IP address and reuse it: the access
    token is cached and renewed automatically. The client is a context manager;
    leaving the block closes it, and a closed client refuses every request with
    :class:`~truvali.ConfigurationError`.

    Every method that sends a request accepts ``timeout``, a read timeout in
    seconds for that call only. The public reference recommends about 10 seconds
    for a single record and about 60 seconds for a batch.

    Example::

        config = TruvaliConfig.from_env()
        with TruvaliClient(config) as client:
            result = client.upsert_customer(CustomerRecord(customer_ref="C-1001"))
            if result.skipped:
                print(result.error)
    """

    def __init__(
        self,
        config: TruvaliConfig,
        *,
        session: requests.Session | None = None,
        token_store: TokenStore | None = None,
        transport: Transport | None = None,
    ) -> None:
        """Initialise the client.

        Args:
            config: The client configuration.
            session: ``requests.Session`` to use; one is created when ``None``. A
                session you pass in stays yours: :meth:`close` and leaving the
                context manager do not close it.
            token_store: Where the access token is kept; process memory by default.
                Never share a store across hosts: tokens are bound to the caller IP.
            transport: A ready transport, mainly for tests. Excludes ``session`` and
                ``token_store`` and must use the same configuration.

        Raises:
            ConfigurationError: If ``transport`` is combined with ``session`` or
                ``token_store`` or carries another configuration.
        """
        if transport is not None:
            if session is not None or token_store is not None:
                raise ConfigurationError(
                    "Pass either transport or session and token_store, not both."
                )
            if transport.config != config:
                raise ConfigurationError("transport must use the same configuration.")
        self._config = config
        self._transport = transport or Transport(config, session=session, token_store=token_store)

    @property
    def config(self) -> TruvaliConfig:
        """The configuration of this client.

        Returns:
            The configuration.
        """
        return self._config

    @property
    def closed(self) -> bool:
        """Whether the client has been closed.

        Returns:
            ``True`` after :meth:`close` or after leaving the context manager.
        """
        return self._transport.closed

    def close(self) -> None:
        """Close the client.

        Later requests raise :class:`~truvali.ConfigurationError`. The HTTP session
        is closed only when the client created it; a session passed to the
        constructor is left open. Calling it again does nothing.
        """
        self._transport.close()

    def __enter__(self) -> TruvaliClient:
        """Enter the context.

        Returns:
            The client itself.
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the client when leaving the context.

        Args:
            exc_type: Type of the exception that ended the block, if any.
            exc: The exception that ended the block, if any.
            traceback: Its traceback, if any.
        """
        self.close()

    def _ensure_open(self) -> None:
        """Refuse to work on a closed client.

        Raises:
            ConfigurationError: If the client is closed.
        """
        if self._transport.closed:
            raise ConfigurationError(
                "The client is closed; create a new TruvaliClient to send requests."
            )

    def check_auth(self, *, timeout: float | None = None) -> AuthCheck:
        """Verify that the credentials work from this host.

        Args:
            timeout: Read timeout in seconds for this call; the configured
                ``read_timeout`` when ``None``.

        Returns:
            The Source Service name and session id of the token.

        Raises:
            ConfigurationError: If the client is closed.
            InvalidArgumentError: If ``timeout`` is not a number greater than 0.
            TruvaliError: A subclass describing a failure of the call.
        """
        self._ensure_open()
        response = self._transport.request("GET", AUTH_CHECK_PATH, timeout=timeout)
        return AuthCheck._from_api(response.data)

    def _upsert(
        self,
        records: Sequence[Any],
        record_type: type[Any],
        path: str,
        parse: Callable[..., T],
        timeout: float | None,
    ) -> UpsertResponse[T]:
        """Validate a batch, send it and parse the per-record results.

        Args:
            records: The records to send.
            record_type: The record class the endpoint accepts.
            path: Path of the upsert endpoint.
            parse: Builds one record result.
            timeout: Read timeout override in seconds, or ``None``.

        Returns:
            The response with one result per record, in input order.

        Raises:
            ConfigurationError: If the client is closed.
            InvalidArgumentError: If ``timeout`` is not a number greater than 0.
            RecordValidationError: If the batch is empty, not a sequence or mixes
                record types.
            PayloadTooLargeError: If the batch exceeds ``max_batch_size`` or 1 MiB.
            TruvaliError: A subclass describing a failure of the call.
        """
        self._ensure_open()
        validate_timeout(timeout)
        name = record_type.__name__
        if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
            raise RecordValidationError(
                f"Expected a sequence of {name}; use the single-record method for one record."
            )
        count = len(records)
        if count == 0:
            raise RecordValidationError("At least one record is required.")
        limit = self._config.max_batch_size
        if count > limit:
            raise PayloadTooLargeError(
                f"{count} records exceed max_batch_size={limit}; split the batch "
                f"(the server accepts at most {SERVER_MAX_RECORDS} records per request)."
            )
        payloads: list[dict[str, Any]] = []
        for index, record in enumerate(records):
            if not isinstance(record, record_type):
                raise RecordValidationError(
                    f"records[{index}] is a {type(record).__name__}; this method accepts "
                    f"{name} only and record types cannot be mixed."
                )
            payloads.append(record.to_payload())
        body = _encode(payloads)
        if len(body) > MAX_BODY_BYTES:
            raise PayloadTooLargeError(
                f"The request body is {len(body)} bytes; the server accepts at most "
                f"{MAX_BODY_BYTES} bytes (1 MiB). Send fewer records per call."
            )
        response = self._transport.request("POST", path, body=body, timeout=timeout)
        return UpsertResponse._from_api(
            response.data,
            request_id=response.request_id,
            message=response.message,
            expected=count,
            parse=parse,
        )

    def upsert_customers(
        self, records: Sequence[CustomerRecord], *, timeout: float | None = None
    ) -> UpsertResponse[CustomerResult]:
        """Create or update customers, keyed on ``customer_ref``.

        Args:
            records: Up to ``max_batch_size`` customer records.
            timeout: Read timeout in seconds for this call; the configured
                ``read_timeout`` when ``None``. About 60 seconds suits a batch.

        Returns:
            One result per record, in input order. Check each one: a ``200`` means
            the batch was accepted, not that every record was stored.

        Raises:
            ConfigurationError: If the client is closed.
            InvalidArgumentError: If ``timeout`` is not a number greater than 0.
            RecordValidationError: If the batch is empty or mixes record types.
            PayloadTooLargeError: If the batch is too large.
            TruvaliError: A subclass describing a failure of the call.
        """
        return self._upsert(
            records, CustomerRecord, CUSTOMER_UPSERT_PATH, CustomerResult._from_api, timeout
        )

    def upsert_financial_events(
        self, records: Sequence[FinancialEvent], *, timeout: float | None = None
    ) -> UpsertResponse[FinancialResult]:
        """Create or update financial transactions, keyed on ``transaction_ref``.

        Args:
            records: Up to ``max_batch_size`` financial events.
            timeout: Read timeout in seconds for this call; the configured
                ``read_timeout`` when ``None``. About 60 seconds suits a batch.

        Returns:
            One result per record, in input order. A new event older than 24 hours
            is stored without evaluation and comes back with ``decision`` ``None``
            unless it was sent with ``is_recover``.

        Raises:
            ConfigurationError: If the client is closed.
            InvalidArgumentError: If ``timeout`` is not a number greater than 0.
            RecordValidationError: If the batch is empty or mixes record types.
            PayloadTooLargeError: If the batch is too large.
            TruvaliError: A subclass describing a failure of the call.
        """
        return self._upsert(
            records, FinancialEvent, FINANCIAL_UPSERT_PATH, FinancialResult._from_api, timeout
        )

    def upsert_non_financial_events(
        self, records: Sequence[NonFinancialEvent], *, timeout: float | None = None
    ) -> UpsertResponse[RecordResult]:
        """Create or update non-financial events, keyed on ``transaction_ref``.

        Args:
            records: Up to ``max_batch_size`` non-financial events.
            timeout: Read timeout in seconds for this call; the configured
                ``read_timeout`` when ``None``. About 60 seconds suits a batch.

        Returns:
            One result per record, in input order. A new event older than 24 hours
            is stored without evaluation and comes back with score 0, ``ACCEPT``
            and ``rule_check`` ``False``.

        Raises:
            ConfigurationError: If the client is closed.
            InvalidArgumentError: If ``timeout`` is not a number greater than 0.
            RecordValidationError: If the batch is empty or mixes record types.
            PayloadTooLargeError: If the batch is too large.
            TruvaliError: A subclass describing a failure of the call.
        """
        return self._upsert(
            records, NonFinancialEvent, NON_FINANCIAL_UPSERT_PATH, RecordResult._from_api, timeout
        )

    def upsert_customer(
        self, record: CustomerRecord, *, timeout: float | None = None
    ) -> CustomerResult:
        """Create or update one customer.

        Args:
            record: The customer record.
            timeout: Read timeout in seconds for this call; the configured
                ``read_timeout`` when ``None``. About 10 seconds suits one record.

        Returns:
            Its result; ``result.request_id`` identifies the call.

        Raises:
            ConfigurationError: If the client is closed.
            InvalidArgumentError: If ``timeout`` is not a number greater than 0.
            RecordValidationError: If ``record`` is not a :class:`CustomerRecord`.
            PayloadTooLargeError: If the record alone exceeds 1 MiB.
            TruvaliError: A subclass describing a failure of the call.
        """
        return self.upsert_customers((record,), timeout=timeout).results[0]

    def upsert_financial_event(
        self, record: FinancialEvent, *, timeout: float | None = None
    ) -> FinancialResult:
        """Create or update one financial transaction.

        Args:
            record: The financial event.
            timeout: Read timeout in seconds for this call; the configured
                ``read_timeout`` when ``None``. About 10 seconds suits one record.

        Returns:
            Its result; ``result.request_id`` identifies the call.

        Raises:
            ConfigurationError: If the client is closed.
            InvalidArgumentError: If ``timeout`` is not a number greater than 0.
            RecordValidationError: If ``record`` is not a :class:`FinancialEvent`.
            PayloadTooLargeError: If the record alone exceeds 1 MiB.
            TruvaliError: A subclass describing a failure of the call.
        """
        return self.upsert_financial_events((record,), timeout=timeout).results[0]

    def upsert_non_financial_event(
        self, record: NonFinancialEvent, *, timeout: float | None = None
    ) -> RecordResult:
        """Create or update one non-financial event.

        Args:
            record: The non-financial event.
            timeout: Read timeout in seconds for this call; the configured
                ``read_timeout`` when ``None``. About 10 seconds suits one record.

        Returns:
            Its result; ``result.request_id`` identifies the call.

        Raises:
            ConfigurationError: If the client is closed.
            InvalidArgumentError: If ``timeout`` is not a number greater than 0.
            RecordValidationError: If ``record`` is not a :class:`NonFinancialEvent`.
            PayloadTooLargeError: If the record alone exceeds 1 MiB.
            TruvaliError: A subclass describing a failure of the call.
        """
        return self.upsert_non_financial_events((record,), timeout=timeout).results[0]

    def reconciliation(
        self,
        *,
        datasets: Iterable[Dataset] | None = None,
        day: date | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        timezone: str | None = None,
        date_field: DateBasis | None = None,
        group_by_day: bool = False,
        breakdown: bool = True,
        timeout: float | None = None,
    ) -> ReconciliationReport:
        """Count the records Truvali holds for your Source Service.

        Without ``day`` or a range the totals are counted. Days are local days in
        ``timezone`` (UTC by default) and ranges are inclusive.

        Args:
            datasets: Datasets to count; every dataset when ``None``.
            day: Count a single local day.
            date_from: First day of a range; requires ``date_to``.
            date_to: Last day of a range (inclusive); requires ``date_from``.
            timezone: IANA time zone name, for example ``"Europe/Berlin"``.
            date_field: The date the counts are based on; ``create_date`` (received
                by Truvali) by default.
            group_by_day: Add per-day counts (at most 366 days, not for totals).
            breakdown: Add counts by status, decision and direction.
            timeout: Read timeout in seconds for this call; the configured
                ``read_timeout`` when ``None``.

        Returns:
            The report.

        Raises:
            ConfigurationError: If the client is closed.
            InvalidArgumentError: If the arguments conflict or are malformed.
            TruvaliError: A subclass describing a failure of the call.
        """
        self._ensure_open()
        validate_timeout(timeout)
        params = _reconciliation_params(
            datasets=datasets,
            day=day,
            date_from=date_from,
            date_to=date_to,
            timezone=timezone,
            date_field=date_field,
            group_by_day=group_by_day,
            breakdown=breakdown,
        )
        response = self._transport.request(
            "GET", RECONCILIATION_PATH, params=params, timeout=timeout
        )
        return ReconciliationReport._from_api(response.data)

    def _fetch_page(
        self, params: dict[str, str], cursor: str | None, timeout: float | None
    ) -> EventPage:
        """Read one page of the event feed.

        Args:
            params: Validated filter parameters.
            cursor: Cursor to continue from, or ``None``.
            timeout: Read timeout override in seconds, or ``None``.

        Returns:
            The page.

        Raises:
            TruvaliError: A subclass describing a failure of the call.
        """
        query = params if cursor is None else {**params, "cursor": cursor}
        response = self._transport.request("GET", EVENTS_PATH, params=query, timeout=timeout)
        return EventPage._from_api(response.data)

    def events(
        self,
        *,
        topic: str | None = None,
        since: datetime | None = None,
        endpoint: str | uuid.UUID | None = None,
        cursor: str | None = None,
        limit: int | None = None,
        timeout: float | None = None,
    ) -> EventPage:
        """Read one page of the event feed: every callback created for you.

        Reading changes nothing and triggers no delivery. Pass the same filters
        with ``cursor`` when paging.

        Args:
            topic: Only events carrying this topic.
            since: Only events created at or after this timezone-aware moment; it
                is sent converted to UTC.
            endpoint: Only the copies for this callback endpoint id; recommended,
                because every endpoint gets its own copy of an event.
            cursor: ``next_cursor`` or a stored ``last_cursor`` of an earlier page.
            limit: Page size from 1 to 500; the server default is 100.
            timeout: Read timeout in seconds for this call; the configured
                ``read_timeout`` when ``None``.

        Returns:
            The page.

        Raises:
            ConfigurationError: If the client is closed.
            InvalidArgumentError: If a filter or ``timeout`` is malformed.
            TruvaliError: A subclass describing a failure of the call.
        """
        self._ensure_open()
        validate_timeout(timeout)
        params = _feed_params(topic=topic, since=since, endpoint=endpoint, limit=limit)
        return self._fetch_page(params, _check_cursor(cursor), timeout)

    def iter_event_pages(
        self,
        *,
        topic: str | None = None,
        since: datetime | None = None,
        endpoint: str | uuid.UUID | None = None,
        cursor: str | None = None,
        limit: int | None = None,
        timeout: float | None = None,
    ) -> Iterator[EventPage]:
        """Read the event feed page by page until its end.

        Store ``last_cursor`` of the final page and pass it as ``cursor`` next time
        to read only newer events.

        Args:
            topic: Only events carrying this topic.
            since: Only events created at or after this timezone-aware moment.
            endpoint: Only the copies for this callback endpoint id.
            cursor: Cursor to start from.
            limit: Page size from 1 to 500.
            timeout: Read timeout in seconds for each page request; the configured
                ``read_timeout`` when ``None``.

        Returns:
            An iterator over the pages; requests are sent lazily.

        Raises:
            ConfigurationError: If the client is closed; raised immediately.
            InvalidArgumentError: If a filter or ``timeout`` is malformed; raised
                immediately.
        """
        self._ensure_open()
        validate_timeout(timeout)
        params = _feed_params(topic=topic, since=since, endpoint=endpoint, limit=limit)
        return self._pages(params, _check_cursor(cursor), timeout)

    def _pages(
        self, params: dict[str, str], cursor: str | None, timeout: float | None
    ) -> Iterator[EventPage]:
        """Follow ``next_cursor`` until the end of the feed.

        Args:
            params: Validated filter parameters.
            cursor: Cursor to start from, or ``None``.
            timeout: Read timeout override in seconds, or ``None``.

        Yields:
            Each page in order.
        """
        while True:
            page = self._fetch_page(params, cursor, timeout)
            yield page
            if page.next_cursor is None:
                return
            cursor = page.next_cursor

    def iter_events(
        self,
        *,
        topic: str | None = None,
        since: datetime | None = None,
        endpoint: str | uuid.UUID | None = None,
        cursor: str | None = None,
        limit: int | None = None,
        timeout: float | None = None,
    ) -> Iterator[FeedEvent]:
        """Read the event feed event by event, following cursors until its end.

        Use :meth:`iter_event_pages` when you need ``last_cursor`` to resume later.

        Args:
            topic: Only events carrying this topic.
            since: Only events created at or after this timezone-aware moment.
            endpoint: Only the copies for this callback endpoint id.
            cursor: Cursor to start from.
            limit: Page size from 1 to 500.
            timeout: Read timeout in seconds for each page request; the configured
                ``read_timeout`` when ``None``.

        Returns:
            An iterator over the events in creation order; requests are sent lazily.

        Raises:
            ConfigurationError: If the client is closed; raised immediately.
            InvalidArgumentError: If a filter or ``timeout`` is malformed; raised
                immediately.
        """
        pages = self.iter_event_pages(
            topic=topic, since=since, endpoint=endpoint, cursor=cursor, limit=limit, timeout=timeout
        )
        return (event for page in pages for event in page.events)
