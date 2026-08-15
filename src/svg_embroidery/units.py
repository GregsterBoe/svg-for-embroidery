"""Parsing and conversion of SVG length values."""

from __future__ import annotations

import re
from typing import NamedTuple, Optional

# Everything is normalised to millimetres internally; the CSS/SVG reference is
# 96 user units (px) == 1 inch == 25.4 mm.
MM_PER_INCH = 25.4
USER_UNITS_PER_INCH = 96.0

UNIT_TO_MM = {
    "mm": 1.0,
    "cm": 10.0,
    "m": 1000.0,
    "in": MM_PER_INCH,
    "pt": MM_PER_INCH / 72.0,
    "pc": MM_PER_INCH / 6.0,
    "q": MM_PER_INCH / 101.6,
    "px": MM_PER_INCH / USER_UNITS_PER_INCH,
    "": MM_PER_INCH / USER_UNITS_PER_INCH,  # no unit -> user units, assumed px
}

# Units we cannot resolve without a font context or a parent box.
RELATIVE_UNITS = {"%", "em", "ex", "ch", "rem", "vw", "vh", "vmin", "vmax"}

_LENGTH_RE = re.compile(
    r"^\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*([a-zA-Z%]*)\s*$"
)


class Length(NamedTuple):
    """A parsed SVG length: a number plus its (lower-cased) unit."""

    value: float
    unit: str

    @property
    def is_relative(self) -> bool:
        return self.unit in RELATIVE_UNITS

    def to_mm(self, user_unit_mm: Optional[float] = None) -> Optional[float]:
        """Convert to millimetres.

        ``user_unit_mm`` is the size of one user unit; pass it when the document
        scale is known (viewBox + physical width), otherwise the CSS default of
        1 px == 1/96 in is used. Relative units return ``None``.
        """
        if self.is_relative:
            return None
        if self.unit in ("", "px") and user_unit_mm is not None:
            return self.value * user_unit_mm
        factor = UNIT_TO_MM.get(self.unit)
        if factor is None:
            return None
        return self.value * factor


def parse_length(value: Optional[str]) -> Optional[Length]:
    """Parse an SVG length such as ``"350px"``, ``"12cm"`` or ``"42"``."""
    if value is None:
        return None
    match = _LENGTH_RE.match(value)
    if not match:
        return None
    number, unit = match.groups()
    try:
        return Length(float(number), unit.lower())
    except ValueError:  # pragma: no cover - regex already guarantees a number
        return None


def to_mm(value: Optional[str], user_unit_mm: Optional[float] = None) -> Optional[float]:
    """Convenience wrapper: parse ``value`` and convert it to millimetres."""
    length = parse_length(value)
    if length is None:
        return None
    return length.to_mm(user_unit_mm)


def mm_to_cm(value: float) -> float:
    return value / 10.0


def cm_to_mm(value: float) -> float:
    return value * 10.0


def format_mm(value: float) -> str:
    """Human readable millimetre value (``12.5 mm``)."""
    return f"{value:.2f}".rstrip("0").rstrip(".") + " mm"


def format_cm(value_mm: float) -> str:
    """Human readable centimetre value for a millimetre input (``1.25 cm``)."""
    return f"{value_mm / 10.0:.2f}".rstrip("0").rstrip(".") + " cm"
