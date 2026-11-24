"""Official Python SDK for the Truvali AML and fraud monitoring platform.

The package offers a synchronous API client (:class:`TruvaliClient`), validated
record types for the upsert endpoints, typed results, and helpers that verify and
parse the webhooks Truvali sends::

    from truvali import CustomerRecord, TruvaliClient, TruvaliConfig

    with TruvaliClient(TruvaliConfig.from_env()) as client:
        result = client.upsert_customer(CustomerRecord(customer_ref="C-1001"))

The library logs to the ``truvali`` logger, which has a ``NullHandler`` attached.
"""

from __future__ import annotations

import logging

from truvali._version import __version__
from truvali.client import TruvaliClient
from truvali.config import RetryPolicy, TruvaliConfig
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
    RecordValidationError,
    ServerError,
    TruvaliError,
    UnexpectedResponseError,
    WebhookEventIdMismatchError,
    WebhookMalformedBodyError,
    WebhookMissingHeaderError,
    WebhookSignatureMismatchError,
    WebhookTimestampError,
    WebhookVerificationError,
)
from truvali.records import (
    Channel,
    Counterparty,
    CounterpartyDeclaration,
    CounterpartyKind,
    CustomerRecord,
    CustomerType,
    FinancialEvent,
    Gender,
    IoType,
    NonFinancialEvent,
)
from truvali.results import (
    AuthCheck,
    Challenge,
    CustomerResult,
    Dataset,
    DatasetCount,
    DateBasis,
    Decision,
    Delivery,
    DeliveryStatus,
    EventPage,
    FeedEvent,
    FinancialResult,
    ReconciliationReport,
    ReconciliationWindow,
    RecordResult,
    UpsertAction,
    UpsertResponse,
)
from truvali.transport import AccessToken, MemoryTokenStore, TokenManager, TokenStore, Transport
from truvali.webhooks import (
    WebhookEnvelope,
    compute_signature,
    construct_event,
    parse_envelope,
    verify_signature,
)

logging.getLogger("truvali").addHandler(logging.NullHandler())

__all__ = [
    "AccessToken",
    "AuthCheck",
    "AuthenticationError",
    "BadRequestError",
    "Challenge",
    "Channel",
    "ConfigurationError",
    "Counterparty",
    "CounterpartyDeclaration",
    "CounterpartyKind",
    "CustomerRecord",
    "CustomerResult",
    "CustomerType",
    "Dataset",
    "DatasetCount",
    "DateBasis",
    "Decision",
    "Delivery",
    "DeliveryStatus",
    "ErrorCode",
    "EventPage",
    "FeedEvent",
    "FinancialEvent",
    "FinancialResult",
    "Gender",
    "InvalidArgumentError",
    "IoType",
    "MemoryTokenStore",
    "NetworkError",
    "NonFinancialEvent",
    "PayloadTooLargeError",
    "PermissionDeniedError",
    "RateLimitError",
    "ReconciliationReport",
    "ReconciliationWindow",
    "RecordResult",
    "RecordValidationError",
    "RetryPolicy",
    "ServerError",
    "TokenManager",
    "TokenStore",
    "Transport",
    "TruvaliClient",
    "TruvaliConfig",
    "TruvaliError",
    "UnexpectedResponseError",
    "UpsertAction",
    "UpsertResponse",
    "WebhookEnvelope",
    "WebhookEventIdMismatchError",
    "WebhookMalformedBodyError",
    "WebhookMissingHeaderError",
    "WebhookSignatureMismatchError",
    "WebhookTimestampError",
    "WebhookVerificationError",
    "__version__",
    "compute_signature",
    "construct_event",
    "parse_envelope",
    "verify_signature",
]
