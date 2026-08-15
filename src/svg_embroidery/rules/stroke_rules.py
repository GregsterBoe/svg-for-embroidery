"""Rules about strokes / line weights."""

from __future__ import annotations

from typing import Iterable, List

from ..document import SvgDocument
from ..findings import Finding, Severity
from ..units import format_mm
from .base import Rule, register


@register
class MinStrokeWidthRule(Rule):
    id = "stroke.min_width"
    summary = "Strokes must be at least the minimum stitchable width"
    params = {"min_mm": 1.5}

    def check(self, doc: SvgDocument) -> Iterable[Finding]:
        minimum = float(self.config["min_mm"])
        scale = doc.unit_scale
        findings: List[Finding] = []
        checked = 0
        unresolved = 0

        for node in doc.drawables:
            if node.paint("stroke") is None and node.paint_reference("stroke") is None:
                continue
            width = node.stroke_width_mm(scale)
            if width is None:
                unresolved += 1
                continue
            checked += 1
            if width < minimum - 1e-9:
                findings.append(
                    self.fail(
                        f"{node.label} has an effective stroke width of {format_mm(width)}, "
                        f"below the minimum of {minimum:g} mm.",
                        hint="Thicken the line, or convert the stroke to a filled outline "
                        "(Inkscape: Path ▸ Stroke to Path) and widen it.",
                        location=node.location,
                        width_mm=round(width, 3),
                        min_mm=minimum,
                    )
                )

        if doc.width_mm is None:
            findings.append(
                self.warn(
                    "Physical document size is unknown, so stroke widths were measured with the "
                    "CSS default of 96 px per inch.",
                )
            )
        if unresolved:
            findings.append(
                self.warn(f"{unresolved} stroke width(s) could not be resolved (relative units).")
            )
        if not findings:
            if checked == 0:
                return [self.ok("No stroked elements — nothing to measure.")]
            return [self.ok(f"All {checked} stroke(s) are at least {minimum:g} mm wide.")]
        return findings


@register
class NoStrokesRule(Rule):
    id = "stroke.forbidden"
    summary = "Artwork must consist of filled shapes only, no strokes"

    def check(self, doc: SvgDocument) -> Iterable[Finding]:
        stroked = [
            node
            for node in doc.drawables
            if node.paint("stroke") is not None or node.paint_reference("stroke") is not None
        ]
        if stroked:
            return [
                self.fail(
                    f"{len(stroked)} element(s) use a stroke.",
                    hint="Convert strokes to filled paths (Inkscape: Path ▸ Stroke to Path).",
                    location=stroked[0].location,
                    count=len(stroked),
                )
            ]
        return [self.ok("No strokes — filled shapes only.")]


@register
class RequireFillRule(Rule):
    id = "fill.required"
    summary = "Every visible shape needs a fill colour"
    default_severity = Severity.WARNING

    def check(self, doc: SvgDocument) -> Iterable[Finding]:
        missing = [
            node
            for node in doc.drawables
            if node.tag not in ("image", "use")
            and node.paint("fill") is None
            and node.paint_reference("fill") is None
        ]
        if missing:
            return [
                self.fail(
                    f"{len(missing)} shape(s) have no fill colour.",
                    hint="Unfilled shapes are invisible in the stitched result.",
                    location=missing[0].location,
                    count=len(missing),
                )
            ]
        return [self.ok("All shapes are filled.")]
