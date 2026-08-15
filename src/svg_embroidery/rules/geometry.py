"""Rules about the file itself and the canvas geometry."""

from __future__ import annotations

from typing import Iterable, List

from ..document import SvgDocument
from ..findings import Finding, Severity
from ..units import cm_to_mm, format_cm
from .base import Rule, RuleConfigError, register


@register
class FileExtensionRule(Rule):
    id = "file.extension"
    summary = "File must use an accepted extension"
    params = {"allowed": [".svg"]}

    def check(self, doc: SvgDocument) -> Iterable[Finding]:
        if doc.path is None:
            return []
        allowed = [str(ext).lower() for ext in self.config["allowed"]]
        suffix = doc.path.suffix.lower()
        if suffix not in allowed:
            return [
                self.fail(
                    f"File extension '{suffix or '(none)'}' is not accepted "
                    f"(allowed: {', '.join(allowed)}).",
                    hint="Export the design as plain SVG.",
                )
            ]
        return [self.ok(f"File extension '{suffix}' is accepted.")]


@register
class CanvasSizeRule(Rule):
    id = "geometry.canvas_size"
    summary = "Canvas width/height must stay within the shop's limits"
    params = {"min_cm": None, "max_cm": None, "min_width_cm": None, "max_width_cm": None,
              "min_height_cm": None, "max_height_cm": None}

    def validate(self) -> None:
        for key, value in self.config.items():
            if value is not None and not isinstance(value, (int, float)):
                raise RuleConfigError(f"rule '{self.id}': {key} must be a number")

    def _bounds(self, axis: str):
        low = self.config[f"min_{axis}_cm"]
        high = self.config[f"max_{axis}_cm"]
        return (
            low if low is not None else self.config["min_cm"],
            high if high is not None else self.config["max_cm"],
        )

    def check(self, doc: SvgDocument) -> Iterable[Finding]:
        if doc.width_mm is None or doc.height_mm is None:
            return [
                self.warn(
                    "Could not determine the physical size (no usable width/height or viewBox).",
                    hint="Set width and height on <svg> with a real unit, e.g. width=\"12cm\".",
                )
            ]

        findings: List[Finding] = []
        for axis, label, value_mm in (
            ("width", "Width", doc.width_mm),
            ("height", "Height", doc.height_mm),
        ):
            low, high = self._bounds(axis)
            if low is not None and value_mm < cm_to_mm(low) - 1e-9:
                findings.append(
                    self.fail(
                        f"{label} {format_cm(value_mm)} is below the minimum of {low} cm.",
                        hint="Scale the artwork up, keeping the aspect ratio.",
                        axis=axis,
                        value_cm=round(value_mm / 10, 3),
                        limit_cm=low,
                    )
                )
            elif high is not None and value_mm > cm_to_mm(high) + 1e-9:
                findings.append(
                    self.fail(
                        f"{label} {format_cm(value_mm)} exceeds the maximum of {high} cm.",
                        hint="Scale the artwork down, keeping the aspect ratio.",
                        axis=axis,
                        value_cm=round(value_mm / 10, 3),
                        limit_cm=high,
                    )
                )

        if not findings:
            findings.append(
                self.ok(
                    f"Size OK: {format_cm(doc.width_mm)} × {format_cm(doc.height_mm)} "
                    f"(from {doc.size_source}).",
                    width_cm=round(doc.width_mm / 10, 3),
                    height_cm=round(doc.height_mm / 10, 3),
                )
            )
        if doc.size_source == "viewbox":
            findings.append(
                self.warn(
                    "Size was derived from the viewBox (96 px = 1 inch) because <svg> has no "
                    "width/height with a unit — the shop may interpret it differently.",
                    hint="Set explicit width/height in cm or mm.",
                )
            )
        return findings


@register
class AspectRatioRule(Rule):
    id = "geometry.aspect_ratio"
    summary = "Canvas must stay within a maximum width:height ratio"
    params = {"max_ratio": 2.0}

    def check(self, doc: SvgDocument) -> Iterable[Finding]:
        if not doc.width_mm or not doc.height_mm:
            return []
        ratio = max(doc.width_mm, doc.height_mm) / min(doc.width_mm, doc.height_mm)
        limit = float(self.config["max_ratio"])
        if ratio > limit + 1e-9:
            return [
                self.fail(
                    f"Aspect ratio {ratio:.2f}:1 exceeds the maximum of {limit:g}:1.",
                    hint="Add padding to the shorter side or recompose the design.",
                    ratio=round(ratio, 3),
                )
            ]
        return [self.ok(f"Aspect ratio OK: {ratio:.2f}:1.")]


@register
class ViewBoxRule(Rule):
    id = "geometry.require_viewbox"
    summary = "The <svg> element must carry a viewBox"
    default_severity = Severity.WARNING

    def check(self, doc: SvgDocument) -> Iterable[Finding]:
        if not doc.root.get("viewBox"):
            return [
                self.fail(
                    "<svg> has no viewBox attribute.",
                    hint="A viewBox keeps the design scalable without distortion.",
                )
            ]
        return [self.ok("viewBox present.")]
