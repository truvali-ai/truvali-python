"""Private enum base shared by the public modules."""

from __future__ import annotations

from enum import Enum


class TextEnum(str, Enum):
    """String-valued enum whose ``str()`` and ``format()`` return the plain value.

    Python 3.11 changed how enums with a ``str`` mix-in render themselves. This base
    keeps the rendering identical on every supported Python version, so members can
    be logged or interpolated without surprises. Members compare equal to their
    string values.
    """

    def __str__(self) -> str:
        """Return the plain value of the member.

        Returns:
            The string value of the member.
        """
        return str(self.value)

    def __format__(self, format_spec: str) -> str:
        """Format the plain value of the member.

        Args:
            format_spec: The format specification applied to the value.

        Returns:
            The formatted string value of the member.
        """
        return format(str(self.value), format_spec)
