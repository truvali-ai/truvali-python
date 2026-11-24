"""Private helpers that read typed values out of decoded JSON documents.

Every helper raises :class:`ShapeError` when a value has the wrong type. The public
entry points translate it into the error class that fits their context.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


class ShapeError(ValueError):
    """A decoded JSON document does not have the documented shape."""


def parse_datetime(value: object, name: str) -> datetime | None:
    """Parse an ISO-8601 date-time, accepting a trailing ``Z`` on Python 3.10.

    Args:
        value: The raw value, ``None`` or a string.
        name: Name of the value, used in the error message.

    Returns:
        The parsed date-time, or ``None`` when the value is ``None``.

    Raises:
        ShapeError: If the value is not a readable ISO-8601 date-time string.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ShapeError(f"'{name}' must be an ISO-8601 date-time string.")
    text = value[:-1] + "+00:00" if value[-1] in "Zz" else value
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise ShapeError(f"'{name}' is not an ISO-8601 date-time: {value!r}.") from exc


def parse_date(value: object, name: str) -> date | None:
    """Parse an ISO-8601 calendar date (``YYYY-MM-DD``).

    Args:
        value: The raw value, ``None`` or a string.
        name: Name of the value, used in the error message.

    Returns:
        The parsed date, or ``None`` when the value is ``None``.

    Raises:
        ShapeError: If the value is not a ``YYYY-MM-DD`` string.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ShapeError(f"'{name}' must be a YYYY-MM-DD string.")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ShapeError(f"'{name}' is not a YYYY-MM-DD date: {value!r}.") from exc


def require_date(value: object, name: str) -> date:
    """Parse a required ISO-8601 calendar date (``YYYY-MM-DD``).

    Args:
        value: The raw value.
        name: Name of the value, used in the error message.

    Returns:
        The parsed date.

    Raises:
        ShapeError: If the value is missing or not a ``YYYY-MM-DD`` string.
    """
    parsed = parse_date(value, name)
    if parsed is None:
        raise ShapeError(f"'{name}' is required.")
    return parsed


def mapping(value: object, name: str) -> Mapping[str, Any]:
    """Require a JSON object.

    Args:
        value: The raw value.
        name: Name of the value, used in the error message.

    Returns:
        The value as a mapping.

    Raises:
        ShapeError: If the value is not a JSON object.
    """
    if not isinstance(value, Mapping):
        raise ShapeError(f"'{name}' must be a JSON object.")
    return value


def optional_mapping(document: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    """Read an optional JSON object.

    Args:
        document: The JSON object to read from.
        key: The key to read.

    Returns:
        The object, or ``None`` when the key is absent or null.

    Raises:
        ShapeError: If the value is present but not a JSON object.
    """
    value = document.get(key)
    return None if value is None else mapping(value, key)


def optional_str(document: Mapping[str, Any], key: str) -> str | None:
    """Read an optional string; integers are accepted and rendered as text.

    Args:
        document: The JSON object to read from.
        key: The key to read.

    Returns:
        The string, or ``None`` when the key is absent or null.

    Raises:
        ShapeError: If the value is present but neither a string nor an integer.
    """
    value = document.get(key)
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    raise ShapeError(f"'{key}' must be a string.")


def required_str(document: Mapping[str, Any], key: str) -> str:
    """Read a required, non-empty string.

    Args:
        document: The JSON object to read from.
        key: The key to read.

    Returns:
        The string.

    Raises:
        ShapeError: If the value is absent, null, empty or not a string.
    """
    value = optional_str(document, key)
    if not value:
        raise ShapeError(f"'{key}' is required.")
    return value


def optional_int(document: Mapping[str, Any], key: str) -> int | None:
    """Read an optional integer.

    Args:
        document: The JSON object to read from.
        key: The key to read.

    Returns:
        The integer, or ``None`` when the key is absent or null.

    Raises:
        ShapeError: If the value is present but not an integer.
    """
    value = document.get(key)
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise ShapeError(f"'{key}' must be an integer.")


def optional_bool(document: Mapping[str, Any], key: str) -> bool | None:
    """Read an optional boolean.

    Args:
        document: The JSON object to read from.
        key: The key to read.

    Returns:
        The boolean, or ``None`` when the key is absent or null.

    Raises:
        ShapeError: If the value is present but not a boolean.
    """
    value = document.get(key)
    if value is None or isinstance(value, bool):
        return value
    raise ShapeError(f"'{key}' must be a boolean.")


def optional_decimal(document: Mapping[str, Any], key: str) -> Decimal | None:
    """Read an optional decimal sent either as a string or as a JSON number.

    The server renders decimals as strings such as ``"40.00"`` or ``"100.0"`` and
    zero sometimes as the number ``0``; all of them are accepted.

    Args:
        document: The JSON object to read from.
        key: The key to read.

    Returns:
        The value as a :class:`~decimal.Decimal`, or ``None`` when absent or null.

    Raises:
        ShapeError: If the value is present but not a finite number.
    """
    value = document.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ShapeError(f"'{key}' must be a number.")
    try:
        number = Decimal(value if isinstance(value, (str, int)) else repr(value))
    except InvalidOperation as exc:
        raise ShapeError(f"'{key}' is not a number: {value!r}.") from exc
    if not number.is_finite():
        raise ShapeError(f"'{key}' must be a finite number.")
    return number


def str_tuple(document: Mapping[str, Any], key: str) -> tuple[str, ...]:
    """Read an optional list of strings.

    Args:
        document: The JSON object to read from.
        key: The key to read.

    Returns:
        The strings as a tuple; empty when the key is absent or null.

    Raises:
        ShapeError: If the value is present but not a list of strings.
    """
    value = document.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ShapeError(f"'{key}' must be a list of strings.")
    return tuple(value)


def mapping_tuple(document: Mapping[str, Any], key: str) -> tuple[Mapping[str, Any], ...]:
    """Read an optional list of JSON objects.

    Args:
        document: The JSON object to read from.
        key: The key to read.

    Returns:
        The objects as a tuple; empty when the key is absent or null.

    Raises:
        ShapeError: If the value is present but not a list of JSON objects.
    """
    value = document.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise ShapeError(f"'{key}' must be a list of JSON objects.")
    return tuple(value)
