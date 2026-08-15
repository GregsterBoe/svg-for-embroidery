"""The reference fixer.

One real repair, to prove the protocol end to end. The batch of safe fixes —
canvas resizing, colour normalisation, dropping unused defs, stripping editor
metadata — is roadmap step A3, and every one of them is held to the contract in
:mod:`svg_embroidery.fixes.verify`.
"""

from __future__ import annotations

from ..document import SvgDocument
from ..units import MM_PER_INCH, USER_UNITS_PER_INCH, parse_length
from .base import FixOutcome, Fixer, Risk, register_fixer


def _format_number(value: float) -> str:
    """Compact decimal: ``453.543307`` not ``453.5433070866142``."""
    return f"{value:.6f}".rstrip("0").rstrip(".")


@register_fixer
class AddViewBox(Fixer):
    """Give an ``<svg>`` the viewBox implied by its own width and height.

    Safe by construction: the viewBox written is the one the renderer already
    assumed, so the image cannot move. It only makes the document scalable
    without a shop's importer guessing.
    """

    rule_id = "geometry.require_viewbox"
    risk = Risk.SAFE
    summary = "add the viewBox implied by width/height"

    def apply(self, doc: SvgDocument) -> FixOutcome:
        outcome = FixOutcome()
        if doc.root.get("viewBox"):
            return outcome.decline("the document already has a viewBox")

        width = parse_length(doc.root.get("width"))
        height = parse_length(doc.root.get("height"))
        if width is None or height is None:
            return outcome.decline("width/height are missing, so no viewBox can be derived")
        if width.is_relative or height.is_relative:
            return outcome.decline(
                "width/height are relative (%), so the viewBox cannot be derived"
            )

        # The viewBox is in user units, and one user unit is 1/96 inch.
        per_unit = MM_PER_INCH / USER_UNITS_PER_INCH
        width_mm = width.to_mm()
        height_mm = height.to_mm()
        if not width_mm or not height_mm:
            return outcome.decline("width/height do not resolve to a physical size")

        view_box = (
            f"0 0 {_format_number(width_mm / per_unit)} {_format_number(height_mm / per_unit)}"
        )
        doc.root.set("viewBox", view_box)
        outcome.add(f'added viewBox="{view_box}"', location="/svg")
        return outcome
