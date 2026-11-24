"""Records sent to the upsert endpoints, validated on construction.

Records are immutable and validated in ``__post_init__`` with the same rules the
server applies, so an invalid record raises :class:`~truvali.RecordValidationError`
locally instead of coming back as ``skipped``. Validation is deliberately stricter
than the server where the server would silently repair a value: amounts with more
than two decimal places, lower-case codes and naive date-times are rejected rather
than rounded, upper-cased or read as UTC.

:meth:`to_payload` renders the JSON object for the API. It never adds volatile
values such as a send time, and it renders every date-time converted to UTC, so
resending an unchanged record produces the same bytes whatever offset the caller
used, and the server recognises it as unchanged. Nested containers passed in
``metadata``, ``geo``, ``founders`` or ``extra`` are not copied; do not mutate them
after constructing the record.
"""

from __future__ import annotations

import ipaddress
import math
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Context, Decimal, InvalidOperation
from enum import Enum, IntEnum
from typing import Any, ClassVar, Literal

from truvali._enum import TextEnum
from truvali.errors import InvalidArgumentError, RecordValidationError

MAX_AMOUNT = Decimal("9999999999.99")

_CENT = Decimal("0.01")
_DECIMAL_CONTEXT = Context(prec=34)
_ALPHA3 = re.compile(r"[A-Z]{3}")
_SIDES = frozenset({"source", "destination"})
_DONE = object()


class Gender(TextEnum):
    """Gender of an individual customer."""

    MALE = "male"
    FEMALE = "female"
    OTHER = "other"
    UNKNOWN = "unknown"


class CustomerType(IntEnum):
    """Type of a customer."""

    INDIVIDUAL = 1
    CORPORATE = 2


class IoType(IntEnum):
    """Direction of a financial transaction: money in or money out."""

    CREDIT = 0
    DEBIT = 1


class Channel(IntEnum):
    """Channel through which an event was performed."""

    UNKNOWN = 0
    WEB = 1
    MOBILE_WEB = 2
    ANDROID = 3
    IOS = 4


class CounterpartyKind(TextEnum):
    """What the party on one side of a transfer is (Travel Rule semantics)."""

    OWN_CUSTOMER = "own_customer"
    FINANCIAL_INSTITUTION = "financial_institution"
    VASP = "vasp"
    UNHOSTED_WALLET = "unhosted_wallet"
    UNKNOWN = "unknown"


_Check = Callable[[str, "_Field", Any], Any]
_Render = Callable[[Any], Any]


def _fail(
    owner: str, name: str, problem: str, *, label: str | None = None
) -> RecordValidationError:
    """Build the validation error for one field.

    Args:
        owner: Name of the record class.
        name: Name of the field, stored in the error.
        problem: Description of the problem, starting with a verb.
        label: More precise location shown in the message instead of ``name``,
            for example one key of ``extra``.

    Returns:
        The error to raise.
    """
    return RecordValidationError(f"{owner}.{label or name} {problem}", field=name)


def _render_value(value: Any) -> Any:
    """Render a value that is already JSON-ready.

    Args:
        value: The value.

    Returns:
        The value, unchanged.
    """
    return value


def _render_enum(value: Enum) -> Any:
    """Render an enum member as its value.

    Args:
        value: The enum member.

    Returns:
        The member value.
    """
    return value.value


def _render_date(value: date) -> str:
    """Render a date as ``YYYY-MM-DD``.

    Args:
        value: The date.

    Returns:
        The ISO-8601 calendar date.
    """
    return value.isoformat()


def _render_amount(value: Decimal) -> str:
    """Render an amount with exactly two decimal places.

    Args:
        value: The validated amount.

    Returns:
        The amount as a string such as ``"1250.00"``.
    """
    return f"{value:.2f}"


def _render_nested(value: Any) -> Any:
    """Render a nested value object through its ``to_payload`` method.

    Args:
        value: The nested object.

    Returns:
        Its JSON-ready payload.
    """
    return value.to_payload()


@dataclass(frozen=True, slots=True)
class _Field:
    """Validation and rendering rule of one record field.

    Attributes:
        name: Attribute name, which is also the payload key.
        check: Validates a non-``None`` value and returns it, possibly coerced.
        render: Turns the validated value into its JSON form, or ``None`` when the
            owning class renders the field itself.
        required: Whether ``None`` is rejected.
        max_length: Longest accepted text, after trimming whitespace.
        kind: Enum or value-object class the field accepts, when relevant.
    """

    name: str
    check: _Check
    render: _Render | None = _render_value
    required: bool = False
    max_length: int | None = None
    kind: type[Any] | None = None


def _check_text(owner: str, spec: _Field, value: Any) -> Any:
    """Validate free text; the length is measured after trimming, like the server.

    Args:
        owner: Name of the record class.
        spec: Rule of the field.
        value: The value to validate.

    Returns:
        The value, unchanged.

    Raises:
        RecordValidationError: If the value is not a string, is blank while
            required, or is too long.
    """
    if not isinstance(value, str):
        raise _fail(owner, spec.name, "must be a string.")
    stripped = value.strip()
    if spec.required and not stripped:
        raise _fail(owner, spec.name, "is required.")
    if spec.max_length is not None and len(stripped) > spec.max_length:
        raise _fail(
            owner,
            spec.name,
            f"exceeds the maximum length of {spec.max_length} characters (got {len(stripped)}).",
        )
    return value


def _check_key(owner: str, spec: _Field, value: Any) -> Any:
    """Validate a reference or slug: non-empty, untrimmed and within its length.

    Args:
        owner: Name of the record class.
        spec: Rule of the field.
        value: The value to validate.

    Returns:
        The value, unchanged.

    Raises:
        RecordValidationError: If the value is not a string, is empty, has
            surrounding whitespace or is too long.
    """
    if not isinstance(value, str):
        raise _fail(owner, spec.name, "must be a string.")
    if not value:
        raise _fail(owner, spec.name, "is required.")
    if value[0].isspace() or value[-1].isspace():
        raise _fail(owner, spec.name, "must not have leading or trailing whitespace.")
    if spec.max_length is not None and len(value) > spec.max_length:
        raise _fail(
            owner,
            spec.name,
            f"exceeds the maximum length of {spec.max_length} characters (got {len(value)}).",
        )
    return value


def _check_alpha3(owner: str, spec: _Field, value: Any) -> Any:
    """Validate an ISO 3166-1 alpha-3 country code.

    Args:
        owner: Name of the record class.
        spec: Rule of the field.
        value: The value to validate.

    Returns:
        The value, unchanged.

    Raises:
        RecordValidationError: If the value is not three upper-case ASCII letters.
    """
    if not isinstance(value, str) or not _ALPHA3.fullmatch(value):
        raise _fail(
            owner, spec.name, "must be an upper-case ISO 3166-1 alpha-3 code such as 'DEU'."
        )
    return value


def _check_currency(owner: str, spec: _Field, value: Any) -> Any:
    """Validate an ISO 4217 currency code.

    Args:
        owner: Name of the record class.
        spec: Rule of the field.
        value: The value to validate.

    Returns:
        The value, unchanged.

    Raises:
        RecordValidationError: If the value is not three upper-case ASCII letters.
    """
    if not isinstance(value, str) or not _ALPHA3.fullmatch(value):
        raise _fail(owner, spec.name, "must be an upper-case ISO 4217 code such as 'EUR'.")
    return value


def _check_amount(owner: str, spec: _Field, value: Any) -> Any:
    """Validate a monetary amount without ever rounding it.

    Args:
        owner: Name of the record class.
        spec: Rule of the field.
        value: The value to validate, a ``Decimal`` or an ``int``.

    Returns:
        The amount as a ``Decimal``.

    Raises:
        RecordValidationError: If the value is a float or another type, is not
            finite, is negative, exceeds the maximum or has more than two decimal
            places.
    """
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        raise _fail(
            owner,
            spec.name,
            "must be a Decimal or an int; floats cannot represent cents exactly.",
        )
    amount = value if isinstance(value, Decimal) else Decimal(value)
    if not amount.is_finite():
        raise _fail(owner, spec.name, "must be a finite number.")
    if amount.is_signed():
        raise _fail(owner, spec.name, "must not be negative; io_type carries the direction.")
    if amount > MAX_AMOUNT:
        raise _fail(owner, spec.name, f"must not exceed {MAX_AMOUNT}.")
    try:
        exact = amount == amount.quantize(_CENT, context=_DECIMAL_CONTEXT)
    except InvalidOperation:
        exact = False
    if not exact:
        raise _fail(owner, spec.name, "must have at most 2 decimal places; it is never rounded.")
    return amount


def _check_date(owner: str, spec: _Field, value: Any) -> Any:
    """Validate a calendar date.

    Args:
        owner: Name of the record class.
        spec: Rule of the field.
        value: The value to validate.

    Returns:
        The value, unchanged.

    Raises:
        RecordValidationError: If the value is not a ``date`` or is a ``datetime``.
    """
    if not isinstance(value, date) or isinstance(value, datetime):
        raise _fail(owner, spec.name, "must be a datetime.date, not a datetime or a string.")
    return value


def _check_datetime(owner: str, spec: _Field, value: Any) -> Any:
    """Validate a timezone-aware date-time.

    Args:
        owner: Name of the record class.
        spec: Rule of the field.
        value: The value to validate.

    Returns:
        The value, unchanged.

    Raises:
        RecordValidationError: If the value is not a ``datetime``, is naive or
            cannot be expressed in UTC.
    """
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise _fail(
            owner, spec.name, "must be a timezone-aware datetime; naive values are rejected."
        )
    try:
        value.astimezone(timezone.utc)
    except OverflowError as exc:
        raise _fail(owner, spec.name, "is out of range when converted to UTC.") from exc
    return value


def _render_datetime(value: datetime) -> str:
    """Render a date-time in UTC with a fixed layout.

    The same instant always yields the same text, whatever offset the caller
    used: ``YYYY-MM-DDTHH:MM:SS+00:00``, with ``.ffffff`` before the offset when
    the instant has microseconds.

    Args:
        value: The validated, timezone-aware date-time.

    Returns:
        The ISO-8601 text in UTC.
    """
    return value.astimezone(timezone.utc).isoformat()


def _check_ip(owner: str, spec: _Field, value: Any) -> Any:
    """Validate an IPv4 or IPv6 address.

    Args:
        owner: Name of the record class.
        spec: Rule of the field.
        value: The value to validate.

    Returns:
        The value, unchanged.

    Raises:
        RecordValidationError: If the value is not a valid address without a scope.
    """
    valid = isinstance(value, str) and "%" not in value
    if valid:
        try:
            ipaddress.ip_address(value)
        except ValueError:
            valid = False
    if not valid:
        raise _fail(owner, spec.name, "must be a valid IPv4 or IPv6 address.")
    return value


def _ensure_json(owner: str, name: str, value: object, *, label: str | None = None) -> None:
    """Check that a value is plain JSON data, without recursion.

    Accepted are ``None``, ``bool``, ``int``, finite ``float``, ``str``, ``list``,
    ``tuple`` and ``dict`` with string keys. Reference cycles are rejected. Runs in
    time and memory linear in the size of the value.

    Args:
        owner: Name of the record class.
        name: Name of the field.
        value: The value to inspect.
        label: More precise location shown in error messages, if any.

    Raises:
        RecordValidationError: If the value contains anything JSON cannot express.
    """
    active: set[int] = set()
    frames: list[tuple[int, Iterator[Any]]] = []
    item: object = value
    while True:
        if isinstance(item, float):
            if not math.isfinite(item):
                raise _fail(owner, name, "contains a NaN or infinite float.", label=label)
        elif isinstance(item, (dict, list, tuple)):
            marker = id(item)
            if marker in active:
                raise _fail(owner, name, "contains a reference cycle.", label=label)
            if isinstance(item, dict):
                if not all(isinstance(key, str) for key in item):
                    raise _fail(
                        owner, name, "contains a JSON object with a non-string key.", label=label
                    )
                children: Iterator[Any] = iter(item.values())
            else:
                children = iter(item)
            active.add(marker)
            frames.append((marker, children))
        elif item is not None and not isinstance(item, (str, int)):
            raise _fail(
                owner,
                name,
                f"contains a {type(item).__name__}, which is not JSON data; "
                "convert it to a string or a number first.",
                label=label,
            )
        while frames:
            marker, children = frames[-1]
            following = next(children, _DONE)
            if following is _DONE:
                frames.pop()
                active.discard(marker)
                continue
            item = following
            break
        else:
            return


def _check_json(owner: str, spec: _Field, value: Any) -> Any:
    """Validate a free-form JSON object or array.

    Args:
        owner: Name of the record class.
        spec: Rule of the field.
        value: The value to validate.

    Returns:
        The value, unchanged.

    Raises:
        RecordValidationError: If the value is not a dict, list or tuple of JSON data.
    """
    if not isinstance(value, (dict, list, tuple)):
        raise _fail(owner, spec.name, "must be a JSON object (dict) or array (list).")
    _ensure_json(owner, spec.name, value)
    return value


def _check_array(owner: str, spec: _Field, value: Any) -> Any:
    """Validate a JSON array.

    Args:
        owner: Name of the record class.
        spec: Rule of the field.
        value: The value to validate.

    Returns:
        The value, unchanged.

    Raises:
        RecordValidationError: If the value is not a list or tuple of JSON data.
    """
    if not isinstance(value, (list, tuple)):
        raise _fail(owner, spec.name, "must be a JSON array (list).")
    _ensure_json(owner, spec.name, value)
    return value


def _check_flag(owner: str, spec: _Field, value: Any) -> Any:
    """Validate a boolean.

    Args:
        owner: Name of the record class.
        spec: Rule of the field.
        value: The value to validate.

    Returns:
        The value, unchanged.

    Raises:
        RecordValidationError: If the value is not a ``bool``.
    """
    if not isinstance(value, bool):
        raise _fail(owner, spec.name, "must be a bool.")
    return value


def _check_choice(owner: str, spec: _Field, value: Any) -> Any:
    """Validate an enum field, accepting a member or its raw value.

    Args:
        owner: Name of the record class.
        spec: Rule of the field; ``spec.kind`` is the enum class.
        value: The value to validate, a member or a raw value.

    Returns:
        The matching enum member.

    Raises:
        RecordValidationError: If the value matches no member.
    """
    choices = spec.kind
    if choices is None or isinstance(value, bool):
        member = None
    else:
        try:
            member = choices(value)
        except ValueError:
            member = None
    if member is None:
        allowed = ", ".join(repr(item.value) for item in choices or ())
        raise _fail(owner, spec.name, f"must be one of {allowed}.")
    return member


def _check_instance(owner: str, spec: _Field, value: Any) -> Any:
    """Validate a nested value object.

    Args:
        owner: Name of the record class.
        spec: Rule of the field; ``spec.kind`` is the expected class.
        value: The value to validate.

    Returns:
        The value, unchanged.

    Raises:
        RecordValidationError: If the value is not an instance of the class.
    """
    if spec.kind is None or not isinstance(value, spec.kind):
        expected = spec.kind.__name__ if spec.kind is not None else "value object"
        raise _fail(owner, spec.name, f"must be a {expected}.")
    return value


def _text(name: str, max_length: int | None = None) -> _Field:
    """Rule for optional free text.

    Args:
        name: Field name.
        max_length: Longest accepted text, if limited.

    Returns:
        The rule.
    """
    return _Field(name, _check_text, max_length=max_length)


def _key(name: str, max_length: int | None = None) -> _Field:
    """Rule for a required reference or slug.

    Args:
        name: Field name.
        max_length: Longest accepted value, if limited.

    Returns:
        The rule.
    """
    return _Field(name, _check_key, required=True, max_length=max_length)


def _choice(name: str, choices: type[Enum], *, required: bool = False) -> _Field:
    """Rule for an enum field.

    Args:
        name: Field name.
        choices: The enum class.
        required: Whether the field is required.

    Returns:
        The rule.
    """
    return _Field(name, _check_choice, _render_enum, required=required, kind=choices)


def _datetime(name: str, *, required: bool = False) -> _Field:
    """Rule for a timezone-aware date-time.

    Args:
        name: Field name.
        required: Whether the field is required.

    Returns:
        The rule.
    """
    return _Field(name, _check_datetime, _render_datetime, required=required)


def _date(name: str) -> _Field:
    """Rule for an optional calendar date.

    Args:
        name: Field name.

    Returns:
        The rule.
    """
    return _Field(name, _check_date, _render_date)


def _amount(name: str, *, required: bool = False) -> _Field:
    """Rule for a monetary amount.

    Args:
        name: Field name.
        required: Whether the field is required.

    Returns:
        The rule.
    """
    return _Field(name, _check_amount, _render_amount, required=required)


def _validate(owner: str, record: object, fields: tuple[_Field, ...]) -> None:
    """Validate every field of a record and store coerced values.

    Args:
        owner: Name of the record class.
        record: The frozen record instance.
        fields: Rules of its fields.

    Raises:
        RecordValidationError: On the first invalid field.
    """
    for spec in fields:
        value = getattr(record, spec.name)
        if value is None:
            if spec.required:
                raise _fail(owner, spec.name, "is required.")
            continue
        checked = spec.check(owner, spec, value)
        if checked is not value:
            object.__setattr__(record, spec.name, checked)


def _render(
    record: object, fields: tuple[_Field, ...], payload: dict[str, Any], prefix: str = ""
) -> None:
    """Write the non-``None`` fields of a record into a payload.

    Args:
        record: The validated record.
        fields: Rules of its fields.
        payload: The payload being built, updated in place.
        prefix: Prefix added to every key, for counterparty fields.
    """
    for spec in fields:
        value = getattr(record, spec.name)
        if value is not None and spec.render is not None:
            payload[prefix + spec.name] = spec.render(value)


def _check_extra(owner: str, extra: object, reserved: frozenset[str]) -> None:
    """Validate the ``extra`` mapping of schema-external keys.

    Args:
        owner: Name of the record class.
        extra: The mapping, or ``None``.
        reserved: Keys owned by the record schema or by the server.

    Raises:
        RecordValidationError: If ``extra`` is not a mapping of non-empty string keys
            to JSON data, or a key collides with a reserved name.
    """
    if extra is None:
        return
    if not isinstance(extra, Mapping):
        raise _fail(owner, "extra", "must be a mapping of field names to JSON values.")
    for key, value in extra.items():
        if not isinstance(key, str) or not key:
            raise _fail(owner, "extra", "keys must be non-empty strings.")
        if key in reserved:
            raise _fail(
                owner,
                "extra",
                f"key '{key}' collides with a field of the record or the server; "
                "use the dedicated attribute instead.",
            )
        _ensure_json(owner, "extra", value, label=f"extra['{key}']")


def _merge_extra(payload: dict[str, Any], extra: Mapping[str, Any] | None) -> None:
    """Copy the ``extra`` keys into a payload.

    Args:
        payload: The payload being built, updated in place.
        extra: The validated mapping, or ``None``.
    """
    if extra:
        payload.update(extra)


@dataclass(frozen=True, slots=True, kw_only=True)
class Counterparty:
    """One side of a transfer, for Travel Rule and counterparty screening.

    A financial event carries it as ``source`` (originator) or ``destination``
    (beneficiary); its fields are sent with the ``source_`` or ``destination_``
    prefix, for example ``destination_holder``.

    Attributes:
        kind: What the party is.
        ref: The ``customer_ref`` of the party when it is your own customer (64).
        holder: Name of the account holder or company (128).
        address: IBAN, account number or wallet address (64).
        identity: National id, passport or tax number (64).
        institution: Bank or VASP name (150).
        institution_code: BIC, LEI or VASP id of the institution (64).
        country: ISO 3166-1 alpha-3 country code.
        provider: Payment provider or network (64).
    """

    kind: CounterpartyKind | None = None
    ref: str | None = None
    holder: str | None = None
    address: str | None = None
    identity: str | None = None
    institution: str | None = None
    institution_code: str | None = None
    country: str | None = None
    provider: str | None = None

    _FIELDS: ClassVar[tuple[_Field, ...]] = (
        _choice("kind", CounterpartyKind),
        _text("ref", 64),
        _text("holder", 128),
        _text("address", 64),
        _text("identity", 64),
        _text("institution", 150),
        _text("institution_code", 64),
        _Field("country", _check_alpha3),
        _text("provider", 64),
    )

    def __post_init__(self) -> None:
        """Validate the fields.

        Raises:
            RecordValidationError: If a field is invalid.
        """
        _validate("Counterparty", self, self._FIELDS)

    def to_payload(self, side: Literal["source", "destination"]) -> dict[str, Any]:
        """Render the prefixed payload keys of this side.

        Args:
            side: ``"source"`` or ``"destination"``.

        Returns:
            The JSON-ready keys, for example ``{"source_kind": "own_customer"}``.

        Raises:
            InvalidArgumentError: If ``side`` is neither ``"source"`` nor
                ``"destination"``.
        """
        if side not in _SIDES:
            raise InvalidArgumentError("side must be 'source' or 'destination'.")
        payload: dict[str, Any] = {}
        _render(self, self._FIELDS, payload, f"{side}_")
        return payload


@dataclass(frozen=True, slots=True, kw_only=True)
class CounterpartyDeclaration:
    """The customer's own statement about the counterparty of an unhosted wallet.

    Attributes:
        name: Declared name of the counterparty.
        identifier_type: Type of the declared identifier, for example a national id.
        identifier: The declared identifier.
        declared_at: When the customer made the declaration (timezone-aware).
    """

    name: str | None = None
    identifier_type: str | None = None
    identifier: str | None = None
    declared_at: datetime | None = None

    _FIELDS: ClassVar[tuple[_Field, ...]] = (
        _text("name"),
        _text("identifier_type"),
        _text("identifier"),
        _datetime("declared_at"),
    )

    def __post_init__(self) -> None:
        """Validate the fields.

        Raises:
            RecordValidationError: If a field is invalid.
        """
        _validate("CounterpartyDeclaration", self, self._FIELDS)

    def to_payload(self) -> dict[str, Any]:
        """Render the declaration object.

        Returns:
            The JSON-ready object without ``None`` values.
        """
        payload: dict[str, Any] = {}
        _render(self, self._FIELDS, payload)
        return payload


_CUSTOMER_FIELDS: tuple[_Field, ...] = (
    _key("customer_ref", 48),
    _choice("customer_type", CustomerType),
    _text("first_name", 100),
    _text("last_name", 100),
    _choice("gender", Gender),
    _date("birth_date"),
    _text("identity_number", 100),
    _text("passport_number", 100),
    _Field("nationality", _check_alpha3),
    _Field("passport_issuing_country", _check_alpha3),
    _text("email"),
    _text("phone", 100),
    _text("country", 100),
    _text("occupation", 100),
    _Field("ip_address", _check_ip),
    _text("status", 24),
    _datetime("registration_date"),
    _datetime("email_verified_at"),
    _datetime("phone_verified_at"),
    _text("company_name", 255),
    _text("legal_form", 50),
    _text("tax_number", 100),
    _text("tax_office", 100),
    _text("registry_number", 100),
    _text("sector", 150),
    _text("lei_code", 20),
    _text("stock_ticker_symbol", 20),
    _text("website", 255),
    _date("incorporation_date"),
    _date("website_created_at"),
    _Field("country_of_origin", _check_alpha3),
    _Field("founders", _check_array),
    _Field("migration", _check_flag, None),
)

_CUSTOMER_SERVER_KEYS = frozenset(
    {
        "id",
        "score",
        "decision",
        "create_date",
        "kyc_risk",
        "sanction_score",
        "sanction_topic",
        "sanction_status",
        "sanction_updated_at",
        "sanction_screened_at",
        "subjects_sanction_score",
        "subjects_sanction_topic",
        "subjects_sanction_status",
        "subjects_unscreened_count",
        "ip_address_score",
        "email_risk_score",
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class CustomerRecord:
    """An individual or corporate customer, keyed on ``customer_ref``.

    Lengths in parentheses are maximum characters. Only ``customer_ref`` is
    required; the server applies ``customer_type`` 1 (individual) to a new customer
    that does not send one.

    Attributes:
        customer_ref: Your stable customer identifier (48).
        customer_type: Individual or corporate.
        first_name: First name (100).
        last_name: Last name (100).
        gender: Gender of an individual.
        birth_date: Date of birth.
        identity_number: National id number (100).
        passport_number: Passport number (100).
        nationality: ISO 3166-1 alpha-3 code.
        passport_issuing_country: ISO 3166-1 alpha-3 code.
        email: E-mail address; an invalid format is stored with a server warning.
        phone: Phone number (100).
        country: Country of residence as free text (100).
        occupation: Occupation (100).
        ip_address: Registration IP address.
        status: Your account status, for example ``"active"`` (24).
        registration_date: When the customer registered (timezone-aware).
        email_verified_at: When the e-mail was verified (timezone-aware).
        phone_verified_at: When the phone was verified (timezone-aware).
        company_name: Company name of a corporate customer (255).
        legal_form: Legal form (50).
        tax_number: Tax number (100).
        tax_office: Tax office (100).
        registry_number: Trade registry number (100).
        sector: Sector or NACE code (150).
        lei_code: Legal Entity Identifier (20).
        stock_ticker_symbol: Stock ticker (20).
        website: Website URL (255).
        incorporation_date: Date of incorporation.
        website_created_at: Date the website was created.
        country_of_origin: ISO 3166-1 alpha-3 country of incorporation.
        founders: Founders, beneficial owners and representatives, as a list of
            objects with ``role``, ``full_name`` and optional details.
        migration: ``True`` for historical loads: the server skips external scoring
            and sanctions screening; rules still run. Sent only when ``True``.
        extra: Schema-external keys, stored by the server and visible to rules.
            Keys must not collide with the fields above or server-owned fields.
    """

    customer_ref: str
    customer_type: CustomerType | None = None
    first_name: str | None = None
    last_name: str | None = None
    gender: Gender | None = None
    birth_date: date | None = None
    identity_number: str | None = None
    passport_number: str | None = None
    nationality: str | None = None
    passport_issuing_country: str | None = None
    email: str | None = None
    phone: str | None = None
    country: str | None = None
    occupation: str | None = None
    ip_address: str | None = None
    status: str | None = None
    registration_date: datetime | None = None
    email_verified_at: datetime | None = None
    phone_verified_at: datetime | None = None
    company_name: str | None = None
    legal_form: str | None = None
    tax_number: str | None = None
    tax_office: str | None = None
    registry_number: str | None = None
    sector: str | None = None
    lei_code: str | None = None
    stock_ticker_symbol: str | None = None
    website: str | None = None
    incorporation_date: date | None = None
    website_created_at: date | None = None
    country_of_origin: str | None = None
    founders: list[dict[str, Any]] | None = None
    migration: bool = False
    extra: Mapping[str, Any] | None = None

    _FIELDS: ClassVar[tuple[_Field, ...]] = _CUSTOMER_FIELDS
    _RESERVED: ClassVar[frozenset[str]] = (
        frozenset(spec.name for spec in _CUSTOMER_FIELDS) | _CUSTOMER_SERVER_KEYS
    )

    def __post_init__(self) -> None:
        """Validate the record.

        Raises:
            RecordValidationError: If a field is missing or invalid.
        """
        _validate("CustomerRecord", self, self._FIELDS)
        _check_extra("CustomerRecord", self.extra, self._RESERVED)

    def to_payload(self) -> dict[str, Any]:
        """Render the JSON object sent to ``/api/customer/upsert/``.

        Returns:
            A JSON-ready dict without ``None`` values. Dates are ``YYYY-MM-DD`` and
            date-times ISO-8601 converted to UTC (``+00:00``).
        """
        payload: dict[str, Any] = {}
        _render(self, self._FIELDS, payload)
        if self.migration:
            payload["migration"] = True
        _merge_extra(payload, self.extra)
        return payload


_EVENT_FIELDS: tuple[_Field, ...] = (
    _key("customer_ref", 48),
    _key("event_slug"),
    _key("transaction_ref", 64),
    _datetime("event_date", required=True),
    _text("status", 16),
    _text("event_description", 128),
    _text("session_id", 128),
    _text("device_id", 128),
    _Field("ip_address", _check_ip),
    _choice("channel", Channel),
    _text("user_agent"),
    _Field("geo", _check_json),
    _Field("metadata", _check_json),
)

_EVENT_SERVER_KEYS = frozenset(
    {
        "id",
        "client_id",
        "data_source",
        "data_source_id",
        "customer",
        "customer_id",
        "event",
        "event_id",
        "score",
        "decision",
        "create_date",
    }
)

_FINANCIAL_FIELDS: tuple[_Field, ...] = (
    *_EVENT_FIELDS,
    _amount("amount", required=True),
    _Field("currency", _check_currency, required=True),
    _choice("io_type", IoType, required=True),
    _amount("fee_amount"),
    _Field("fee_currency", _check_currency),
    _text("payment_method", 16),
    _Field("source", _check_instance, None, kind=Counterparty),
    _Field("destination", _check_instance, None, kind=Counterparty),
    _Field("originator_verified", _check_flag),
    _Field(
        "counterparty_declaration",
        _check_instance,
        _render_nested,
        kind=CounterpartyDeclaration,
    ),
    _text("purpose_code", 32),
    _text("purpose_text", 255),
    _Field("is_recover", _check_flag, None),
)

_COUNTERPARTY_KEYS = frozenset(
    f"{side}_{spec.name}" for side in _SIDES for spec in Counterparty._FIELDS
)


@dataclass(frozen=True, slots=True, kw_only=True)
class _EventRecord:
    """Fields shared by financial and non-financial events.

    Attributes:
        customer_ref: The customer who performed the event (48).
        event_slug: Slug of an active event definition of the matching type.
        transaction_ref: Your unique, deterministic id of the event (64). It is the
            idempotency key: resending the same ref updates instead of duplicating.
        event_date: When the event happened (timezone-aware).
        status: Your status of the event, for example ``"success"`` (16).
        event_description: Free description (128).
        session_id: Session id (128).
        device_id: Device id (128).
        ip_address: IP address of the end user.
        channel: Channel of the event.
        user_agent: User agent string of the end user.
        geo: Free-form geolocation data.
        metadata: Free-form data visible to rules. Never put volatile values such as
            a send time here, or every resend counts as a change.
    """

    customer_ref: str
    event_slug: str
    transaction_ref: str
    event_date: datetime
    status: str | None = None
    event_description: str | None = None
    session_id: str | None = None
    device_id: str | None = None
    ip_address: str | None = None
    channel: Channel | None = None
    user_agent: str | None = None
    geo: dict[str, Any] | list[Any] | None = None
    metadata: dict[str, Any] | list[Any] | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class FinancialEvent(_EventRecord):
    """A movement of value, keyed on ``transaction_ref`` within your Source Service.

    A new event whose ``event_date`` is 24 hours old or more is stored without rule
    evaluation and its result carries ``decision`` ``None``, unless ``is_recover``
    is set. Shares ``customer_ref``, ``event_slug``, ``transaction_ref``, ``event_date``,
    ``status``, ``event_description``, ``session_id``, ``device_id``,
    ``ip_address``, ``channel``, ``user_agent``, ``geo`` and ``metadata`` with
    :class:`NonFinancialEvent`. Lengths in parentheses are maximum characters.

    Attributes:
        amount: Amount, at least 0, at most 9999999999.99, with at most two decimal
            places. Floats are rejected.
        currency: ISO 4217 code such as ``"EUR"``.
        io_type: Credit (money in) or debit (money out).
        fee_amount: Fee, with the same rules as ``amount``.
        fee_currency: ISO 4217 code of the fee.
        payment_method: Payment method; ``"crypto"`` marks a crypto transfer (16).
        source: The originator side.
        destination: The beneficiary side.
        originator_verified: Whether the originating institution verified the sender.
        counterparty_declaration: The customer's declaration about the counterparty.
        purpose_code: Purpose code (32).
        purpose_text: Purpose text (255).
        is_recover: Forces rule evaluation of a newly created event older than one
            day. Sent only when ``True``.
        extra: Schema-external keys, stored by the server and visible to rules.
            Keys must not collide with the fields above or server-owned fields.
    """

    amount: Decimal
    currency: str
    io_type: IoType
    fee_amount: Decimal | None = None
    fee_currency: str | None = None
    payment_method: str | None = None
    source: Counterparty | None = None
    destination: Counterparty | None = None
    originator_verified: bool | None = None
    counterparty_declaration: CounterpartyDeclaration | None = None
    purpose_code: str | None = None
    purpose_text: str | None = None
    is_recover: bool = False
    extra: Mapping[str, Any] | None = None

    _FIELDS: ClassVar[tuple[_Field, ...]] = _FINANCIAL_FIELDS
    _RESERVED: ClassVar[frozenset[str]] = (
        frozenset(spec.name for spec in _FINANCIAL_FIELDS) | _COUNTERPARTY_KEYS | _EVENT_SERVER_KEYS
    )

    def __post_init__(self) -> None:
        """Validate the record.

        Raises:
            RecordValidationError: If a field is missing or invalid.
        """
        _validate("FinancialEvent", self, self._FIELDS)
        _check_extra("FinancialEvent", self.extra, self._RESERVED)

    def to_payload(self) -> dict[str, Any]:
        """Render the JSON object sent to ``/api/transaction/financial/upsert/``.

        Returns:
            A JSON-ready dict without ``None`` values. Amounts are strings with two
            decimal places and counterparty fields carry their side prefix.
        """
        payload: dict[str, Any] = {}
        _render(self, self._FIELDS, payload)
        if self.source is not None:
            _render(self.source, Counterparty._FIELDS, payload, "source_")
        if self.destination is not None:
            _render(self.destination, Counterparty._FIELDS, payload, "destination_")
        if self.is_recover:
            payload["is_recover"] = True
        _merge_extra(payload, self.extra)
        return payload


@dataclass(frozen=True, slots=True, kw_only=True)
class NonFinancialEvent(_EventRecord):
    """Behaviour without an amount, such as a login or a password change.

    ``transaction_ref`` is required here too, although the server accepts events
    without it: a deterministic ref is what keeps a retry from creating a duplicate.
    A new event whose ``event_date`` is 24 hours old or more is stored without rule
    evaluation; its result carries score 0, ``ACCEPT`` and ``rule_check`` ``False``.

    Attributes:
        customer_ref: The customer who performed the event (48).
        event_slug: Slug of an active non-financial event definition.
        transaction_ref: Your unique, deterministic id of the event (64).
        event_date: When the event happened (timezone-aware).
        status: Your status of the event (16).
        event_description: Free description (128).
        session_id: Session id (128).
        device_id: Device id (128).
        ip_address: IP address of the end user.
        channel: Channel of the event.
        user_agent: User agent string of the end user.
        geo: Free-form geolocation data.
        metadata: Free-form data visible to rules; never put volatile values here.
        extra: Schema-external keys, stored by the server and visible to rules.
            Keys must not collide with the fields above or server-owned fields.
    """

    extra: Mapping[str, Any] | None = None

    _FIELDS: ClassVar[tuple[_Field, ...]] = _EVENT_FIELDS
    _RESERVED: ClassVar[frozenset[str]] = (
        frozenset(spec.name for spec in _EVENT_FIELDS) | _EVENT_SERVER_KEYS
    )

    def __post_init__(self) -> None:
        """Validate the record.

        Raises:
            RecordValidationError: If a field is missing or invalid.
        """
        _validate("NonFinancialEvent", self, self._FIELDS)
        _check_extra("NonFinancialEvent", self.extra, self._RESERVED)

    def to_payload(self) -> dict[str, Any]:
        """Render the JSON object sent to ``/api/transaction/non-financial/upsert/``.

        Returns:
            A JSON-ready dict without ``None`` values.
        """
        payload: dict[str, Any] = {}
        _render(self, self._FIELDS, payload)
        _merge_extra(payload, self.extra)
        return payload
