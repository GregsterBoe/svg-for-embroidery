"""Rules about colours: how many, which ones, and no gradients."""

from __future__ import annotations

from typing import Iterable, List

from ..colors import normalize_color
from ..document import SvgDocument
from ..findings import Finding, Severity
from .base import Rule, RuleConfigError, register

GRADIENT_TAGS = ("linearGradient", "radialGradient", "meshgradient", "pattern")


@register
class MaxColorsRule(Rule):
    id = "color.max_count"
    summary = "Limit the number of distinct colours"
    params = {"max_colors": 3, "include_stroke": True}

    def validate(self) -> None:
        if not isinstance(self.config["max_colors"], int) or self.config["max_colors"] < 1:
            raise RuleConfigError(f"rule '{self.id}': max_colors must be a positive integer")

    def check(self, doc: SvgDocument) -> Iterable[Finding]:
        colors = doc.colors(include_stroke=bool(self.config["include_stroke"]))
        limit = int(self.config["max_colors"])
        names = sorted(colors)
        if len(colors) > limit:
            return [
                self.fail(
                    f"{len(colors)} distinct colours found, at most {limit} allowed: "
                    f"{', '.join(names)}.",
                    hint="Merge similar tones, or split the design into fewer colour areas.",
                    colors=names,
                )
            ]
        return [
            self.ok(
                f"Colour count OK: {len(colors)} of max {limit} "
                f"({', '.join(names) if names else 'no painted colours'}).",
                colors=names,
            )
        ]


@register
class ColorPaletteRule(Rule):
    id = "color.allowed_palette"
    summary = "Only colours from a fixed palette may be used"
    params = {"colors": [], "palette_name": "shop palette"}

    def validate(self) -> None:
        if not self.config["colors"]:
            raise RuleConfigError(f"rule '{self.id}': 'colors' must list at least one colour")
        normalised = []
        for entry in self.config["colors"]:
            color = normalize_color(str(entry))
            if color is None:
                raise RuleConfigError(f"rule '{self.id}': '{entry}' is not a valid colour")
            normalised.append(color)
        self.config["colors"] = normalised

    def check(self, doc: SvgDocument) -> Iterable[Finding]:
        allowed = set(self.config["colors"])
        used = doc.colors()
        offenders = sorted(set(used) - allowed)
        if offenders:
            return [
                self.fail(
                    f"{len(offenders)} colour(s) outside the {self.config['palette_name']}: "
                    f"{', '.join(offenders)}.",
                    hint=f"Allowed: {', '.join(sorted(allowed))}.",
                    location=used[offenders[0]][0].location,
                    colors=offenders,
                )
            ]
        return [self.ok(f"All colours are part of the {self.config['palette_name']}.")]


@register
class NoGradientsRule(Rule):
    id = "color.no_gradients"
    summary = "Gradients and pattern fills are not stitchable"
    params = {"tags": list(GRADIENT_TAGS)}

    def check(self, doc: SvgDocument) -> Iterable[Finding]:
        gradients = doc.by_tag(*[str(tag) for tag in self.config["tags"]])
        if gradients:
            kinds = sorted({node.tag for node in gradients})
            return [
                self.fail(
                    f"{len(gradients)} gradient/pattern definition(s) found ({', '.join(kinds)}).",
                    hint="Replace gradients with flat colour areas.",
                    location=gradients[0].location,
                    count=len(gradients),
                )
            ]
        return [self.ok("No gradients or pattern fills.")]


@register
class OpacityRule(Rule):
    id = "color.no_transparency"
    summary = "Partial transparency cannot be stitched or printed"
    params = {"min_opacity": 1.0}
    default_severity = Severity.WARNING

    def check(self, doc: SvgDocument) -> Iterable[Finding]:
        threshold = float(self.config["min_opacity"])
        findings: List[Finding] = []
        for node in doc.drawables:
            for prop in ("opacity", "fill-opacity", "stroke-opacity"):
                raw = node.style.get(prop)
                if raw is None:
                    continue
                try:
                    value = float(str(raw).strip().rstrip("%")) / (100 if "%" in str(raw) else 1)
                except ValueError:
                    continue
                if value < threshold - 1e-9:
                    findings.append(
                        self.fail(
                            f"{node.label} uses {prop}={value:g}.",
                            hint="Flatten transparency into solid colours.",
                            location=node.location,
                            value=value,
                        )
                    )
                    break
        if findings:
            return findings
        return [self.ok("No partial transparency found.")]
