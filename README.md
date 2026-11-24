# truvali

`truvali` is the official Python SDK for the Truvalı AML and fraud monitoring platform. It gives your back end a synchronous API client that sends customers, financial transactions and non-financial events to Truvalı and returns typed risk results, plus helpers that verify and parse the webhooks Truvalı sends back. Records are validated before they leave your process, access tokens are obtained and renewed for you, transient failures are retried, and every failure is mapped to a small exception hierarchy.

This version wraps authentication, the three upsert endpoints, reconciliation, the event feed and webhook verification. Other endpoints of the platform are not wrapped yet.

## Contents

- [Requirements](#requirements)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Concepts](#concepts)
  - [Refs and idempotency](#refs-and-idempotency)
  - [The 24-hour rule and rule_check](#the-24-hour-rule-and-rule_check)
  - [Decisions only escalate](#decisions-only-escalate)
  - [Tokens](#tokens)
  - [Retries](#retries)
  - [Errors](#errors)
  - [Webhooks](#webhooks)
  - [Event feed](#event-feed)
  - [Reconciliation](#reconciliation)
  - [Logging](#logging)
- [Versioning and compatibility](#versioning-and-compatibility)
- [Security](#security)
- [Contributing](#contributing)
- [License](#license)

## Requirements

- Python 3.10, 3.11, 3.12 or 3.13
- [`requests`](https://pypi.org/project/requests/) 2.31 or newer (installed automatically)
- The base URL of your Truvalı installation and the `api_key` and `api_secret` of a Source Credential
- A host whose public IP address is on the allow list of that credential

The package is fully type-annotated and ships a `py.typed` marker.

## Installation

```bash
pip install truvali
```

## Quick start

### Configuration

Pass the endpoint and credentials to `TruvaliConfig`. Only `base_url`, `api_key` and `api_secret` are required; the other settings are shown with their defaults.

```python
from truvali import RetryPolicy, TruvaliConfig

config = TruvaliConfig(
    base_url="https://truvali.example.com",
    api_key="your-api-key",
    api_secret="your-api-secret",
    connect_timeout=5.0,
    read_timeout=30.0,
    max_batch_size=200,
    user_agent_suffix=None,
    retry=RetryPolicy(),
)
```

In a deployed service, keep the credentials out of the code and read them from the environment:

```python
from truvali import TruvaliConfig

config = TruvaliConfig.from_env()
```

`from_env` reads these variables and keeps the default for every other setting:

| Variable | Meaning |
| --- | --- |
| `TRUVALI_BASE_URL` | Root URL of your Truvalı installation, for example `https://truvali.example.com` |
| `TRUVALI_API_KEY` | The Source Credential key |
| `TRUVALI_API_SECRET` | The Source Credential secret |

To change another setting on top of the environment, derive a copy with `dataclasses.replace(config, read_timeout=60.0)`. The configuration is immutable, `api_secret` never appears in its `repr()`, and an invalid or missing value raises `ConfigurationError` (the message names the setting, never its value). Use an `https://` base URL: `http://` is accepted for local sandboxes, but logs a warning on the `truvali` logger unless the host is `localhost`, `127.0.0.1` or `::1`.

### Create a client

`TruvaliClient` is a context manager that closes its HTTP session on exit. Create one client per process and egress IP address and reuse it for every call: it caches the access token and keeps connections open.

```python
from truvali import TruvaliClient, TruvaliConfig

with TruvaliClient(TruvaliConfig.from_env()) as client:
    check = client.check_auth()
    print(check.client, check.session_id)
```

`check_auth()` verifies that the credentials work from this host and returns the name of your Source Service. In a long-running service, create the client once at start-up and call `client.close()` on shutdown instead of using `with`:

```python
from truvali import TruvaliClient, TruvaliConfig

client = TruvaliClient(TruvaliConfig.from_env())
```

The examples below use this `client`. Leaving the `with` block or calling `close()` closes the client for good: `client.closed` becomes `True` and every further request raises `ConfigurationError`, so create a new client instead of reusing a closed one. `close()` may be called more than once. A `requests.Session` that you pass in with `session=` is never closed by the client.

Every method that sends a request accepts a keyword-only `timeout`: a read timeout in seconds for that call only, instead of the configured `read_timeout` (30 seconds by default). The connect timeout is not affected, and a login triggered by the call keeps the configured timeouts. About 10 seconds suits a single record and about 60 seconds a batch.

### Send a customer

Customers are keyed on `customer_ref`, your own stable identifier. Only `customer_ref` is required. Sending the same reference again updates the customer.

```python
from datetime import date, datetime, timezone

from truvali import CustomerRecord, CustomerType

customer = CustomerRecord(
    customer_ref="C-1001",
    customer_type=CustomerType.INDIVIDUAL,
    first_name="Deniz",
    last_name="Kaya",
    birth_date=date(1990, 4, 17),
    nationality="TUR",
    email="deniz.kaya@example.com",
    phone="+905550000000",
    ip_address="203.0.113.7",
    registration_date=datetime(2026, 1, 15, 9, 30, tzinfo=timezone.utc),
)

result = client.upsert_customer(customer)
print(result.action, result.decision, result.score, result.sanction_status)
```

### Send a financial event

A financial event is a movement of value. `customer_ref`, `event_slug`, `transaction_ref`, `event_date`, `amount`, `currency` and `io_type` are required. The `event_slug` must name an active financial event definition of your installation, and the customer must already exist.

```python
from datetime import datetime, timezone
from decimal import Decimal

from truvali import Channel, Counterparty, CounterpartyKind, FinancialEvent, IoType

transfer = FinancialEvent(
    customer_ref="C-1001",
    event_slug="transfer_out",
    transaction_ref="TX-0001",
    event_date=datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc),
    amount=Decimal("9000.00"),
    currency="TRY",
    io_type=IoType.DEBIT,
    status="success",
    ip_address="203.0.113.7",
    channel=Channel.IOS,
    source=Counterparty(kind=CounterpartyKind.OWN_CUSTOMER, ref="C-1001"),
    destination=Counterparty(
        kind=CounterpartyKind.FINANCIAL_INSTITUTION,
        holder="Example Holder",
        address="TR000000000000000000000000",
        institution="Example Bank",
        institution_code="EXBKTR2A",
        country="TUR",
    ),
)

result = client.upsert_financial_event(transfer, timeout=10)
print(result.action, result.decision, result.score, result.rule_check)
```

`source` is the originator and `destination` the beneficiary; their fields are sent with the `source_` and `destination_` prefixes of the API. `Counterparty.ref` is the `customer_ref` of your own customer on that side; an IBAN, account number or wallet address goes into `address`.

### Send a non-financial event

A non-financial event is behaviour without an amount, such as a login or a password change. `customer_ref`, `event_slug`, `transaction_ref` and `event_date` are required.

```python
from datetime import datetime, timezone

from truvali import Channel, NonFinancialEvent

login = NonFinancialEvent(
    customer_ref="C-1001",
    event_slug="failed_login",
    transaction_ref="EVT-0001",
    event_date=datetime(2026, 11, 24, 12, 5, tzinfo=timezone.utc),
    ip_address="198.51.100.9",
    device_id="a91f3c7d20e4",
    channel=Channel.ANDROID,
)

result = client.upsert_non_financial_event(login)
print(result.action, result.decision, result.score)
```

### Client-side validation

Records are immutable and validated when you construct them, with the rules the server applies. An invalid record raises `RecordValidationError` (its `field` attribute names the offending field) and is never sent. Where the server would silently repair a value, the SDK is deliberately stricter:

- Amounts must be `Decimal` or `int`, not `float`, at least 0, at most 9999999999.99, with at most two decimal places. They are never rounded.
- Date-times must be timezone-aware. Naive values are rejected instead of being read as UTC. Aware values are converted to UTC when the record is sent.
- Country codes are upper-case ISO 3166-1 alpha-3 (`"DEU"`), currency codes upper-case ISO 4217 (`"EUR"`).
- Text longer than the field limit is rejected, not truncated.
- `geo`, `metadata`, `founders` and `extra` must be plain JSON data.

Keys outside the schema go into `extra` (a mapping). The server stores them and makes them visible to rules. An `extra` key that collides with a record field or a server-owned field is rejected.

### Read the results

An upsert that returns normally means the request was accepted, not that every record was stored. Each record has its own result; branch on `action` (or the `ok` and `skipped` shortcuts), not on the decision.

| Attribute | Meaning |
| --- | --- |
| `ref` | The `customer_ref` or `transaction_ref` of the record |
| `id` | Truvalı's id of the record; `None` when skipped |
| `action` | `UpsertAction.CREATED`, `UPDATED` or `SKIPPED`; `UNKNOWN` for an action this SDK version does not know |
| `ok`, `skipped` | Shortcuts: stored (created or updated), or rejected with nothing stored |
| `changed` | `False` for an unchanged resend (not evaluated again) and for skipped records |
| `decision` | `Decision.ACCEPT`, `REVIEW` or `REJECT`; `UNKNOWN` for a decision this SDK version does not know; `None` only for a new financial event older than 24 hours |
| `score` | Risk score from 0 to 100 as a `Decimal` |
| `rule_check` | Whether rules ran; `None` when the server does not send it (customers and skipped records) |
| `warnings` | Values the server normalised or ignored |
| `error` | Why the record was skipped; several errors are joined by spaces |
| `customer_missing` | `True` when one of the errors is that the referenced customer does not exist |
| `concurrent_duplicate` | `True` when the record lost a create race with a concurrent request |
| `retryable` | `True` when resending the same record unchanged can succeed |
| `challenge` | Extra verification a rule requires (a `Challenge`), if any |
| `log_id` | Id of the rule execution log of the last evaluation |
| `request_id` | Request id of the call; quote it to support |
| `raw` | The result object exactly as received |

A skipped record carries score 100 and `Decision.REJECT` only so that code which ignores `error` still fails safe. `retryable` is `True` for a skipped record that lost a create race and for a skipped record whose only error is a missing customer (send the customer first, then resend the event). It is `False` when any other error is present, because the same record would be skipped again.

A value a newer server may add does not fail the response: it becomes the `UNKNOWN` member of its enum and the original text stays in `raw`. A record with an unknown action is neither `ok` nor `skipped`, is not `retryable` and makes `all_ok` false, so treat it, like an unknown decision, with caution.

```python
from truvali import Decision, RecordResult


def handle(result: RecordResult) -> str:
    """Translate a record result into an action of your own system."""
    if result.skipped:
        if result.customer_missing:
            return "send-customer-then-resend"
        if result.retryable:
            return "resend"
        return f"fix-record: {result.error}"
    if not result.ok:
        return f"check-manually: unknown action {result.raw.get('action')}"
    for warning in result.warnings:
        print("warning:", warning)
    if not result.changed:
        return "unchanged"
    if result.rule_check is False:
        return "stored-without-rule-evaluation"
    if result.decision is Decision.REJECT:
        return "block"
    if result.decision in (Decision.REVIEW, Decision.UNKNOWN):
        return "hold"
    return "proceed"
```

Suggested handling of the decisions: proceed on `ACCEPT`; proceed or hold on `REVIEW`, depending on your risk appetite (the outcome of the review reaches you later by webhook); block or hold for manual release on `REJECT`.

`CustomerResult` adds the screening and scoring keys (`sanction_score`, `sanction_topic`, `sanction_status`, `sanction_screened_at`, `subjects`, `subjects_sanction_status`, `subjects_unscreened_count`, `ip_address_score`, `email_risk_score`). `FinancialResult` adds `travel_rule`, `counterparty_sanction` and `counterparty_wallet_sanction`.

### Send batches

`upsert_customers`, `upsert_financial_events` and `upsert_non_financial_events` take a list or tuple of records of one type and return an `UpsertResponse`: one result per record, in the order you sent them, skipped records included.

```python
from datetime import datetime, timezone
from decimal import Decimal

from truvali import FinancialEvent, IoType

events = [
    FinancialEvent(
        customer_ref="C-1001",
        event_slug="deposit",
        transaction_ref=f"TX-{number:04d}",
        event_date=datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc),
        amount=Decimal("125.50"),
        currency="EUR",
        io_type=IoType.CREDIT,
    )
    for number in range(100, 550)
]

size = client.config.max_batch_size
for start in range(0, len(events), size):
    response = client.upsert_financial_events(events[start : start + size], timeout=60)
    print(response.request_id, len(response), response.all_ok)
    for skipped in response.skipped:
        print(skipped.ref, skipped.error, skipped.retryable)
```

`UpsertResponse` supports `len()`, iteration and indexing, and offers `results`, `request_id`, `message`, `all_ok`, `skipped` and `by_ref(ref)` (when a batch carried the same ref more than once, `by_ref` returns the last result).

The limits are enforced locally, before anything is sent:

| Limit | Value | Raised when exceeded |
| --- | --- | --- |
| Records per call | `max_batch_size`: 200 by default, from 1 to 1000 (the server maximum) | `PayloadTooLargeError` |
| Request body | 1 MiB (1,048,576 bytes) | `PayloadTooLargeError` |
| Batch shape | At least one record, in a list or tuple, all of one record type | `RecordValidationError` |

Send real-time traffic one record per call, when the event happens. For back-fills, 100 to 200 records per batch is the recommended size, and `migration=True` on historical customers makes Truvalı skip external scoring and sanctions screening (rules still run). When a batch of large records exceeds the body limit, send fewer records per call. A batch takes longer than a single record, so pass a longer read timeout with the call, as `timeout=60` does above.

## Concepts

### Refs and idempotency

Every upsert is keyed on your own reference. Customers are keyed on `customer_ref`; financial and non-financial events are keyed on `transaction_ref` within your Source Service. Sending a ref again updates the stored record instead of creating a second one, which is what makes retrying after a timeout safe.

The SDK requires `transaction_ref` for every event, non-financial ones included. The API accepts non-financial events without a ref, but then nothing identifies a resend and every retry creates a duplicate. Derive the ref deterministically from your own data (for example your primary key of the transaction or of the login attempt), never from a random value or the send time.

A resend that changes nothing stored is recognised by the server as unchanged: rules do not run again, nothing is written, and the result carries `changed=False`, `rule_check=False` and the stored score and decision. This only works when a resend is byte-identical to the first send:

- The SDK renders a record deterministically (sorted keys, compact separators, amounts with exactly two decimal places) and never adds a value of its own, such as a send time. Equal records always produce identical bytes.
- Everything you put into `metadata`, `geo` and `extra` is compared too. Never put volatile values there: a send time, a retry counter, a random trace id or the current balance at send time make every resend look like a change, and every "change" is evaluated again.
- Date-times are converted to UTC and rendered in one fixed layout (`YYYY-MM-DDTHH:MM:SS+00:00`, with `.ffffff` before the offset when the instant has microseconds), so the same instant yields the same bytes whatever UTC offset your `datetime` carries. Dates are rendered as `YYYY-MM-DD`.
- Build a resend from the same stored values. A different instant, for example one read from a clock again instead of from your database, is a change.

```python
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from truvali import FinancialEvent, IoType


def build(amount: Decimal, event_date: datetime) -> FinancialEvent:
    """Build the same event from stored values."""
    return FinancialEvent(
        customer_ref="C-1001",
        event_slug="deposit",
        transaction_ref="TX-0002",
        event_date=event_date,
        amount=amount,
        currency="EUR",
        io_type=IoType.CREDIT,
        metadata={"order_id": "ORD-77"},
    )


in_utc = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
in_local_time = datetime(2026, 11, 24, 15, 0, tzinfo=timezone(timedelta(hours=3)))

first = build(Decimal("10"), in_utc).to_payload()
resend = build(Decimal("10.00"), in_local_time).to_payload()

assert first == resend
assert first["amount"] == "10.00"
assert first["event_date"] == "2026-11-24T12:00:00+00:00"
```

The customer, the event definition and the `transaction_ref` of a stored event cannot be changed by an update. Do not resend a record to make Truvalı send a webhook again; read missed webhooks from the [event feed](#event-feed).

### The 24-hour rule and rule_check

Rules are evaluated when a record is created or changed, with one exception: an event whose `event_date` is more than 24 hours in the past is stored without rule evaluation. For such an event `rule_check` is `False`. A newly created financial event of that age has no decision (`decision` is `None`); a newly created non-financial event of that age comes back with score 0 and `Decision.ACCEPT`. Read `rule_check` before you rely on such a decision: it was not produced by your rules.

`rule_check` is therefore `False` in two cases: the event is older than 24 hours, or the record is an unchanged resend. It is `None` where the server does not send the key: on customer results and on skipped records.

For a late financial event that must still be evaluated, set `is_recover=True` on the `FinancialEvent`. It forces rule evaluation of a newly created event older than 24 hours, which then carries a decision; it has no effect on an event that already exists.

### Decisions only escalate

The score and the decision of a stored record can rise on an update but never fall. An event that was answered with `REVIEW` does not return to `ACCEPT` because a later update looks harmless; outcomes decided after your API call, such as an analyst closing a case, reach you through [webhooks](#webhooks). Treat the highest decision you have received for a ref as the current one.

### Tokens

You never handle access tokens yourself:

- **Lazy login.** The client exchanges the API key and secret for a token on the first call, not when it is constructed.
- **Renewal.** The token is cached and renewed shortly before it expires (a tenth of its lifetime in advance, at most five minutes). The lifetime is read from the login response, never assumed. When a renewal fails for a transient reason and the cached token has not expired yet, the cached token keeps being used.
- **One new login per call.** When a call is answered with `401` (`token_expired`, `token_invalid`, `session_invalid`) or `403 ip_mismatch`, the client logs in again and repeats the call exactly once. If the error persists it is raised.
- **Thread safety.** Concurrent callers wait for a single login instead of logging in once each.

A token is bound to the IP address it was issued to. This has two consequences:

- Keep **one token per egress IP**. A process that reaches Truvalı through one IP address needs one client.
- **Never share a token across hosts.** A token used from another IP address is answered with `403 ip_mismatch`.

By default the token lives in process memory (`MemoryTokenStore`). To let the worker processes of one host share a token, pass your own `TokenStore`: any object with `get()`, `set(token)` and `clear()`. A store may outlive the process, but it must never be shared across hosts or egress IP addresses, and it holds a credential, so protect it like one.

```python
from truvali import AccessToken, TruvaliClient, TruvaliConfig


class HostCacheTokenStore:
    """Token store on a host-local cache with get, set and delete methods."""

    def __init__(self, cache, key="truvali-access-token"):
        """Remember the cache and the key the token is stored under."""
        self._cache = cache
        self._key = key

    def get(self):
        """Return the stored token, or None."""
        stored = self._cache.get(self._key)
        return None if stored is None else AccessToken(*stored)

    def set(self, token):
        """Store a token, replacing any previous one."""
        self._cache.set(self._key, (token.value, token.issued_at, token.expires_at))

    def clear(self):
        """Forget the stored token."""
        self._cache.delete(self._key)


client = TruvaliClient(TruvaliConfig.from_env(), token_store=HostCacheTokenStore(host_cache))
```

`host_cache` stands for a cache of your own that is local to the host. `AccessToken` never shows its value in `repr()`.

Errors that a new token cannot fix are raised immediately as `PermissionDeniedError`: `ip_not_allowed` (the egress IP is not on the allow list), `credential_inactive` and `blocked`. They need your administrator; do not retry them in a loop.

### Retries

The transport retries transient failures according to the `RetryPolicy` of the configuration:

| Setting | Default | Meaning |
| --- | --- | --- |
| `max_retries` | `2` | Retries after the first attempt; `0` disables retrying |
| `backoff_initial` | `0.5` | Delay in seconds before the first retry |
| `backoff_max` | `8.0` | Upper bound in seconds for any single delay |
| `retry_on_timeout` | `True` | Whether failures that may have reached the server (read timeouts, dropped connections) are retried |

- Retried: connection errors, timeouts, HTTP 5xx and HTTP 429. Every other 4xx is final.
- The delay before retry `n` (starting at 0) is `min(backoff_max, backoff_initial * 2 ** n)`: 0.5 s and 1 s with the defaults.
- A 429 or 5xx response with a `Retry-After` value waits `max(retry_after, delay)`. When `retry_after` is larger than `backoff_max`, the SDK does not wait at all and raises the error (for a 429, a `RateLimitError` with `retry_after` set) so that your code can decide.
- Failures to connect are retried also with `retry_on_timeout=False`, because the request never reached the server.
- Redirects are never followed.

Retrying upserts is safe because records are keyed by their refs. A call blocks while it waits between retries; lower `max_retries` or `backoff_max` for latency-sensitive paths.

```python
from truvali import RetryPolicy, TruvaliConfig

config = TruvaliConfig(
    base_url="https://truvali.example.com",
    api_key="your-api-key",
    api_secret="your-api-secret",
    retry=RetryPolicy(max_retries=4, backoff_initial=0.25, backoff_max=4.0),
)
```

### Errors

Every exception the SDK raises on purpose derives from `TruvaliError`, which carries `message`, `status_code`, `code` and `request_id` (each `None` when it does not apply). `code` is the machine-readable code of the server, an `ErrorCode` member when this version knows it and the plain string otherwise; members compare equal to their strings, so `error.code == "ip_not_allowed"` works. Branch on the exception class or on `code`, never on the message text.

| Exception | Raised when |
| --- | --- |
| `ConfigurationError` | A setting is missing or invalid, an environment variable is missing, no webhook secret was given, or a request was attempted on a closed client. Also a `ValueError`. |
| `InvalidArgumentError` | An argument of a method is malformed or conflicts with another one: the filters of `reconciliation()`, `events()`, `iter_event_pages()` and `iter_events()`, a `timeout` that is not a number greater than 0, a webhook `tolerance` that is not an integer of at least 0, or the `side` of `Counterparty.to_payload()`. Nothing was sent. Also a `ValueError`. |
| `RecordValidationError` | A record failed validation on construction, or a batch is empty, not a list or tuple, or mixes record types. Nothing was sent. Has `field`. Also a `ValueError`. |
| `PayloadTooLargeError` | A batch exceeds `max_batch_size` or 1 MiB (raised before sending), or the server answered HTTP 413. |
| `NetworkError` | No complete HTTP response arrived and the retries are used up. `may_have_reached_server` tells whether the server may have processed the request. |
| `AuthenticationError` | HTTP 401: `invalid_credentials` on login, or a token error that persists after one new login. |
| `PermissionDeniedError` | HTTP 403: `ip_not_allowed`, `credential_inactive`, `blocked`, or an `ip_mismatch` that persists after one new login. |
| `RateLimitError` | HTTP 429 after the retries are used up, or with a `Retry-After` above `backoff_max`. Has `retry_after` (seconds, or `None`). |
| `BadRequestError` | HTTP 400: the server rejected the request as a whole, for example `invalid_cursor`, `invalid_since`, `invalid_limit` or `invalid_endpoint` on the event feed. |
| `ServerError` | HTTP 5xx after the retries are used up, or the `internal_error` code. |
| `UnexpectedResponseError` | The response does not have the documented shape: not JSON, an unexpected status such as a redirect, or a result list whose length differs from the number of records sent. |
| `WebhookVerificationError` | A webhook delivery could not be verified. Has `reason`. Also a `ValueError`. See [Webhooks](#webhooks) for its subclasses. |

`ConfigurationError`, `InvalidArgumentError`, `RecordValidationError` and `WebhookVerificationError` also subclass `ValueError`, so both `except TruvaliError` and `except ValueError` catch them.

```python
from truvali import (
    AuthenticationError,
    NetworkError,
    PayloadTooLargeError,
    PermissionDeniedError,
    RateLimitError,
    RecordValidationError,
    TruvaliError,
)


def send(client, records):
    """Send a batch and translate failures into an outcome of your own job."""
    try:
        return client.upsert_financial_events(records, timeout=60)
    except RecordValidationError as error:
        return f"fix the batch: {error.message}"
    except PayloadTooLargeError:
        return "split the batch"
    except RateLimitError as error:
        return f"retry in {error.retry_after or 60} s"
    except NetworkError as error:
        return f"retry later (may have reached the server: {error.may_have_reached_server})"
    except (AuthenticationError, PermissionDeniedError) as error:
        return f"check credentials and IP allow list: {error.code}"
    except TruvaliError as error:
        return f"failed: {error} (request_id={error.request_id})"
```

Record-level problems are not exceptions: they come back as skipped results, see [Read the results](#read-the-results).

### Webhooks

Outcomes decided after your API call reach you as webhooks: an analyst's case decision (`compliance_decision`), a resolved challenge (`challenge_resolved`), a sanctions hit on a later re-screening (`sanction_rescreen_hit`), or a topic your own rules emit. Truvalı sends each one as a signed HTTP POST to the callback endpoint you registered.

`construct_event(body, headers, secrets)` verifies a delivery and parses it in one step. It checks that the timestamp header is present and within the tolerance (300 seconds by default; the keyword-only `tolerance` argument changes it), that a signature in `X-Truvali-Signature` or `X-Truvali-Signature-Previous` matches one of your secrets (constant-time comparison), and that `X-Truvali-Event-Id` equals `event_id` in the body. Header names are matched case-insensitively. It returns a `WebhookEnvelope` or raises a `WebhookVerificationError`.

A receiver must follow these rules:

1. **Verify, then parse.** Pass the raw body bytes exactly as received. A body that was parsed and serialised again no longer matches the signature. Never act on an unverified body.
2. **De-duplicate on `event_id`.** Delivery is at least once and retries reuse the `event_id`. Keep the ids you have stored for at least 48 hours.
3. **Answer 2xx only after persisting.** Store the event first, then acknowledge. Any other answer, redirects included, counts as a failed attempt and the delivery is retried.
4. **Process asynchronously.** Answer within 30 seconds and do the real work in a background job.
5. **Do not rely on order.** Deliveries may arrive out of order; use `occurred_at` to order events.

A framework-agnostic receiver:

```python
import json
import time

from truvali import WebhookVerificationError, compute_signature, construct_event

SECRETS = ["whsec-current-example"]
stored_events = {}


def receive(body: bytes, headers: dict) -> int:
    """Verify, de-duplicate and persist a delivery, then return the HTTP status."""
    try:
        event = construct_event(body, headers, SECRETS)
    except WebhookVerificationError as error:
        print("rejected:", error.reason)
        return 401
    if event.event_id not in stored_events:
        stored_events[event.event_id] = event
    return 204


body = json.dumps(
    {
        "event_id": "evt-0001",
        "topic": "compliance_decision",
        "topics": ["compliance_decision"],
        "action": None,
        "request_id": None,
        "data_source": None,
        "occurred_at": "2026-11-24T12:00:00Z",
        "attempt": 1,
        "data": {"customer_ref": "C-1001"},
    }
).encode("utf-8")
timestamp = str(int(time.time()))
headers = {
    "X-Truvali-Timestamp": timestamp,
    "X-Truvali-Signature": "sha256=" + compute_signature(SECRETS[0], timestamp, body),
    "X-Truvali-Event-Id": "evt-0001",
}

assert receive(body, headers) == 204
assert receive(body, headers) == 204
assert receive(body + b" ", headers) == 401
assert stored_events["evt-0001"].customer_ref == "C-1001"
```

`stored_events` stands for your database: insert under a unique constraint on `event_id` and let a background job pick the event up. The second half of the example signs a sample body with `compute_signature`, which is also how you test your receiver.

The same receiver as a Django view (`TruvaliEvent` is a model of your own with a unique `event_id`, `process_truvali_event` a task of your own queue):

```python
from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from myapp.models import TruvaliEvent
from myapp.tasks import process_truvali_event
from truvali import WebhookVerificationError, construct_event


@csrf_exempt
@require_POST
def truvali_webhook(request: HttpRequest) -> HttpResponse:
    """Verify a Truvalı delivery, store it once and hand it to a background task."""
    try:
        event = construct_event(request.body, request.headers, settings.TRUVALI_WEBHOOK_SECRETS)
    except WebhookVerificationError:
        return HttpResponse(status=401)
    _, created = TruvaliEvent.objects.get_or_create(
        event_id=event.event_id,
        defaults={"topic": event.topic, "body": request.body.decode("utf-8")},
    )
    if created:
        process_truvali_event.delay(event.event_id)
    return HttpResponse(status=204)
```

The background task reads the stored body back with `parse_envelope(body)`, which parses without verifying and is meant for bodies that were verified earlier.

**The envelope.** `WebhookEnvelope` has `event_id`, `topic` (the first topic), `topics` (every topic merged into the delivery), `action` (`None`, `"suspend"` or `"unsuspend"`), `request_id` (the API call that caused the event, when there is one), `data_source`, `occurred_at`, `attempt` and `data` (the topic-specific payload). Three shortcuts read common values out of `data`: `customer_ref`, `transaction_ref` and `callback_topics` (the topics of the rule callback actions that fired).

**Secret rotation.** Pass every secret you currently accept. While Truvalı rotates the secret of an endpoint, it signs with the new secret and sends the signature made with the previous one in `X-Truvali-Signature-Previous`; a delivery is accepted when any signature matches any of your secrets. Add the new secret to your list before the rotation and remove the old one afterwards:

```python
import time

from truvali import compute_signature, construct_event

body = b'{"event_id": "evt-0002", "topic": "challenge_resolved", "data": {}}'
timestamp = str(int(time.time()))
headers = {
    "X-Truvali-Timestamp": timestamp,
    "X-Truvali-Signature": "sha256=" + compute_signature("whsec-new-example", timestamp, body),
    "X-Truvali-Signature-Previous": "sha256="
    + compute_signature("whsec-old-example", timestamp, body),
}

event = construct_event(body, headers, ["whsec-new-example", "whsec-old-example"])
assert event.topics == ("challenge_resolved",)
assert construct_event(body, headers, ["whsec-old-example"]).event_id == "evt-0002"
```

**Rejections.** Every verification failure is a subclass of `WebhookVerificationError` with a machine-readable `reason`:

| Exception | `reason` |
| --- | --- |
| `WebhookMissingHeaderError` | `missing_timestamp`, `missing_signature` |
| `WebhookTimestampError` | `bad_timestamp`, `timestamp_out_of_tolerance` |
| `WebhookSignatureMismatchError` | `bad_signature` |
| `WebhookEventIdMismatchError` | `event_id_mismatch` |
| `WebhookMalformedBodyError` | `missing_event_id`, `malformed_body` |

Answer them with HTTP 401 or 400 and do not process the body. An empty secret list raises `ConfigurationError` and a malformed `tolerance` raises `InvalidArgumentError` instead: those are faults of your deployment, not of the delivery.

**Lower-level helpers.** `verify_signature(body, headers, secrets)` runs the same checks and returns only the event id. `compute_signature(secret, timestamp, body)` returns the lower-case hex HMAC-SHA256 of `"<timestamp>." + body`; the header value is `sha256=` followed by that digest. The deprecated `AUTHORIZATION` header of a delivery is never read by the SDK; do not rely on it.

### Event feed

The event feed is the pull side of webhooks: it lists every event that was created for your callback endpoints, in creation order, each exactly once. Use it to catch up after an outage of your receiver, or on a schedule as a safety net. Reading the feed changes nothing and triggers no delivery.

A `FeedEvent` is a `WebhookEnvelope` (its `attempt` is `None`) plus `delivery`: the `status` (`DeliveryStatus.PENDING`, `SUCCESS`, `FAILED` or `DEAD`; `UNKNOWN` for a status this SDK version does not know), `attempts`, `last_attempt_at`, `endpoint_id`, `endpoint_name` and `raw` (the delivery object exactly as received). The same handler can therefore process webhook deliveries and feed entries.

Paging works with cursors. Every page carries `next_cursor` (`None` at the end of the feed) and `last_cursor`, the position after the last event read so far. Store `last_cursor` yourself and pass it as `cursor` on the next run to read only newer events:

```python
from datetime import datetime, timedelta, timezone

cursor = load_cursor()
since = None if cursor else datetime.now(timezone.utc) - timedelta(days=1)

for page in client.iter_event_pages(endpoint=ENDPOINT_ID, cursor=cursor, since=since, limit=500):
    for event in page:
        if not already_stored(event.event_id):
            store(event)
    if page.last_cursor:
        save_cursor(page.last_cursor)
```

`load_cursor`, `save_cursor`, `already_stored` and `store` stand for your own persistence, `ENDPOINT_ID` for the id of your callback endpoint.

| Method | Returns |
| --- | --- |
| `client.events(...)` | One `EventPage` |
| `client.iter_event_pages(...)` | An iterator over the pages, following `next_cursor` to the end |
| `client.iter_events(...)` | An iterator over the events of all pages |

All three accept the same keyword filters: `topic`, `since` (a timezone-aware `datetime`), `endpoint` (the id of a callback endpoint, as `str` or `uuid.UUID`), `cursor`, `limit` (1 to 500; the server default is 100) and `timeout` (the read timeout of each page request). A `since` value is converted to UTC before it is sent. Filter on `endpoint` when you have more than one: every endpoint gets its own copy of an event. Requests are sent lazily while you iterate. De-duplicate on `event_id` exactly as for live deliveries, and keep the stored ids at least as far back as the range you read.

### Reconciliation

`client.reconciliation()` returns how many records Truvalı holds for your Source Service, so that you can compare the numbers with your own. It returns counts only, never record ids.

```python
from datetime import date

from truvali import Dataset, DateBasis

report = client.reconciliation(
    datasets=[Dataset.FINANCIAL, Dataset.NON_FINANCIAL],
    day=date(2026, 11, 23),
    timezone="Europe/Istanbul",
    date_field=DateBasis.EVENT_DATE,
)

financial = report.datasets[Dataset.FINANCIAL]
print(financial.count, financial.date_field_used, financial.breakdown.get("decision"))
```

| Argument | Meaning |
| --- | --- |
| `datasets` | `Dataset.CUSTOMER`, `FINANCIAL`, `NON_FINANCIAL`; every dataset when omitted |
| `day` | Count a single local day |
| `date_from`, `date_to` | Count an inclusive range of local days; pass both, and not together with `day` |
| `timezone` | IANA time zone of the local days; UTC when omitted |
| `date_field` | `DateBasis.CREATE_DATE` (received by Truvalı, the default), `EVENT_DATE` or `REGISTRATION_DATE` |
| `group_by_day` | Add per-day counts (`by_day`); needs a day or a range of at most 366 days |
| `breakdown` | Add counts by status, decision and direction; `True` by default |
| `timeout` | Read timeout in seconds for this call |

Without `day` or a range, the totals are counted. The `ReconciliationReport` has `data_source_id`, `data_source_name`, `generated_at`, `window` (the counted window), `raw` and `datasets`, a mapping from the dataset name the server sent to a `DatasetCount` with `count`, `date_field_used`, `note`, `scope`, `breakdown` and `by_day`. The keys are plain strings; `Dataset` members compare equal to them, so `report.datasets[Dataset.FINANCIAL]` works, and a dataset name a newer server may add is kept under its own name. Check `date_field_used` and `note`: they tell when a dataset was counted on another date field than the one you asked for.

### Logging

The SDK logs to the standard library logger named `truvali`. A `NullHandler` is attached, so nothing is printed unless you configure logging.

```python
import logging

logging.basicConfig(level=logging.INFO)
logging.getLogger("truvali").setLevel(logging.DEBUG)
```

| Level | What is logged |
| --- | --- |
| `DEBUG` | Every request: method, path, HTTP status, duration and request id |
| `INFO` | A successful login with the token lifetime; a rejected token followed by a new login |
| `WARNING` | Every retry with its reason and delay; a failed token renewal while the current token is still valid; a configuration whose `base_url` uses plain `http://` for a host that is not local |

Request and response bodies, query strings, tokens, API secrets, webhook secrets and personal data are never logged. Exception messages follow the same rule: configuration errors name the setting, not its value.

## Versioning and compatibility

`truvali` follows [Semantic Versioning](https://semver.org/). While the version is `0.x`, the API may still change: a minor release (`0.1` -> `0.2`) can contain breaking changes, a patch release (`0.1.0` -> `0.1.1`) never does. Pin accordingly, for example `truvali>=0.1,<0.2`. Every change is listed in [CHANGELOG.md](https://github.com/truvali-ai/truvali-python/blob/main/CHANGELOG.md).

- The public API is what `truvali.__all__` exports. Modules and names that start with an underscore are private and may change in any release.
- Supported Python versions are 3.10 to 3.13; each is tested on every change.
- The SDK tolerates additions to the API: unknown keys in a response are ignored and remain available in `raw`; an action, decision or delivery status this version does not know becomes the `UNKNOWN` member of its enum; an unknown reconciliation dataset is kept under its name; and an error code this version does not know is passed through as a plain string in `error.code`.
- `truvali.__version__` holds the installed version. The same version is sent in the `User-Agent` header; `user_agent_suffix` appends a product token of your own.

## Security

To report a vulnerability, follow [SECURITY.md](https://github.com/truvali-ai/truvali-python/blob/main/SECURITY.md); please do not open a public issue. The same file has short guidance on handling API secrets, access tokens and webhook secrets.

## Contributing

Bug reports and pull requests are welcome. [CONTRIBUTING.md](https://github.com/truvali-ai/truvali-python/blob/main/CONTRIBUTING.md) describes the development setup, the checks and the code conventions.

## License

Apache License 2.0. See [LICENSE](https://github.com/truvali-ai/truvali-python/blob/main/LICENSE).
