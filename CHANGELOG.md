# Changelog

All notable changes to `truvali` are documented in this file. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the version is `0.x`, a minor release may contain breaking changes; they are listed under "Changed" or "Removed".

## [0.1.0] - Unreleased

First release of the official Python SDK for the Truvalı AML and fraud monitoring platform.

### Added

- `TruvaliClient`, a synchronous client that is a context manager. A closed client (`closed` is `True`) refuses every request with `ConfigurationError`; `close()` may be called more than once and never closes a session passed in by the caller. Its methods:
  - `upsert_customer` and `upsert_customers`
  - `upsert_financial_event` and `upsert_financial_events`
  - `upsert_non_financial_event` and `upsert_non_financial_events`
  - `check_auth` to verify the credentials from the current host
  - `reconciliation` for record counts by total, day or date range, with optional per-day counts and breakdowns
  - `events`, `iter_event_pages` and `iter_events` to read the event feed with cursors
  - a keyword-only `timeout` on every method that sends a request: a read timeout for that call only
- `TruvaliConfig` with `from_env` (`TRUVALI_BASE_URL`, `TRUVALI_API_KEY`, `TRUVALI_API_SECRET`), connect and read timeouts, `max_batch_size`, `user_agent_suffix` and a `RetryPolicy`. The API secret is hidden from `repr()`. A plain `http://` base URL for a host that is not local logs a warning.
- Record types validated on construction: `CustomerRecord`, `FinancialEvent`, `NonFinancialEvent`, `Counterparty` and `CounterpartyDeclaration`, with the enums `CustomerType`, `Gender`, `IoType`, `Channel` and `CounterpartyKind`. Amounts are `Decimal` or `int` and never rounded, date-times must be timezone-aware, and `extra` carries keys outside the schema.
- Deterministic request bodies (sorted keys, compact separators, no volatile values, date-times converted to UTC in one fixed layout), so that an unchanged resend is byte-identical.
- Local enforcement of the batch limits: `max_batch_size` (1 to 1000 records, 200 by default) and the 1 MiB body limit.
- Typed results: `UpsertResponse`, `RecordResult`, `CustomerResult`, `FinancialResult` and `Challenge`, with the enums `UpsertAction` and `Decision` (each with an `UNKNOWN` member for values this version does not know, so that additions on the server do not fail a response) and the helpers `ok`, `skipped`, `customer_missing`, `concurrent_duplicate`, `retryable`, `all_ok` and `by_ref`.
- `AuthCheck`, `ReconciliationReport`, `ReconciliationWindow`, `DatasetCount`, `Dataset`, `DateBasis`, `EventPage`, `FeedEvent`, `Delivery` and `DeliveryStatus` (with an `UNKNOWN` member). `ReconciliationReport.datasets` is keyed by the dataset name the server sent, so unknown datasets are kept.
- Token handling: lazy login, renewal before expiry, one new login and one repeat of the call when a token is rejected, a thread-safe `TokenManager`, and a pluggable `TokenStore` with `MemoryTokenStore` as the default. `AccessToken` hides its value from `repr()`.
- `Transport` with retries for connection errors, timeouts, HTTP 5xx and HTTP 429, exponential backoff and support for `Retry-After`. Redirects are not followed.
- Exception hierarchy rooted at `TruvaliError`: `ConfigurationError`, `InvalidArgumentError`, `RecordValidationError`, `PayloadTooLargeError`, `NetworkError`, `AuthenticationError`, `PermissionDeniedError`, `RateLimitError`, `BadRequestError`, `ServerError` and `UnexpectedResponseError`, plus the `ErrorCode` enum of the server error codes.
- Webhook helpers: `construct_event`, `verify_signature`, `parse_envelope` and `compute_signature`, the `WebhookEnvelope` type, support for several secrets and the `X-Truvali-Signature-Previous` header during a secret rotation, and `WebhookVerificationError` with the subclasses `WebhookMissingHeaderError`, `WebhookTimestampError`, `WebhookSignatureMismatchError`, `WebhookEventIdMismatchError` and `WebhookMalformedBodyError`.
- Logging to the `truvali` logger (method, path, status, request id and timing only; never bodies, tokens, secrets or personal data) with a `NullHandler` attached.
- Type hints throughout and a `py.typed` marker.
- Support for Python 3.10, 3.11, 3.12 and 3.13.
