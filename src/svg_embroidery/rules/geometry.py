"""Rules about the file itself and the canvas geometry.

The measurements that need real path maths live in
:mod:`svg_embroidery.geometry`; this module only asks it questions.
"""

from __future__ import annotations

from typing import Iterable, List

from ..document import SvgDocument
from ..findings import Finding, Severity
from ..units import cm_to_mm, format_cm, format_mm
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


#: Shapes whose outline we cannot know without a font or a resolved reference.
UNMEASURABLE_TAGS = ("text", "image", "use")


@register
class MinFeatureSizeRule(Rule):
    """A5: detail a needle cannot render, found by measuring rather than guessing.

    A shape is stitchable where a disc the width of the finest stitch fits
    inside it. Shrinking the shape by half that width and growing it back —
    a morphological opening — leaves exactly the part that passes, so whatever
    disappears is the part that would not survive.

    Two ways to fail, reported differently because they mean different things:
    a shape that vanishes entirely is a hairline nobody can stitch, while a
    shape that loses a fraction of itself has a spike or a waist on an
    otherwise fine body.

    Sharp corners lose a sliver to this test no matter how solid the shape is
    (a disc cannot reach into a corner), so :func:`geometry.corner_allowance`
    works out what the corners were always going to cost and only the rest
    counts against ``tolerance``. Without that, every small rectangle would be
    reported.

    Needs the geometry backend. Without it the rule measures nothing and says
    so, rather than failing the file or the run — a verdict must not depend on
    what happens to be installed.
    """

    id = "geometry.min_feature_size"
    summary = "Filled shapes must have no detail too fine to stitch"
    params = {"min_mm": 1.5, "tolerance": 0.01}

    def validate(self) -> None:
        if float(self.config["min_mm"]) <= 0:
            raise RuleConfigError(f"rule '{self.id}': min_mm must be positive")
        if not 0 <= float(self.config["tolerance"]) < 1:
            raise RuleConfigError(f"rule '{self.id}': tolerance must be between 0 and 1")

    def check(self, doc: SvgDocument) -> Iterable[Finding]:
        from ..geometry import (
            GeometryError,
            INSTALL_HINT,
            default_backend,
            measure_thinness,
            node_contours_mm,
        )

        backend = default_backend()
        if backend is None:
            return [
                self.ok(
                    "Minimum feature size was not measured — this machine has no path "
                    f"geometry backend. To measure it, {INSTALL_HINT}.",
                    measured=False,
                )
            ]

        minimum = float(self.config["min_mm"])
        tolerance = float(self.config["tolerance"])
        scale = doc.unit_scale
        findings: List[Finding] = []
        checked = 0
        unmeasured = 0

        for node in doc.drawables:
            if node.paint("fill") is None and node.paint_reference("fill") is None:
                continue  # nothing is painted inside the outline; nothing to thin
            contours = node_contours_mm(node, scale)
            if not contours:
                if node.tag in UNMEASURABLE_TAGS:
                    unmeasured += 1
                continue

            # A stroke paints outside the fill, so a hairline path carrying a
            # fat stroke is not a thin feature. Measure what ends up on the
            # fabric, not what the fill alone covers.
            stroke_mm = node.stroke_width_mm(scale) or 0.0
            try:
                thinness = measure_thinness(
                    contours,
                    minimum,
                    backend=backend,
                    stroke_width=stroke_mm,
                    cap=node.style.get("stroke-linecap", "butt"),
                    join=node.style.get("stroke-linejoin", "miter"),
                )
            except GeometryError as exc:
                findings.append(
                    self.warn(f"{node.label} could not be measured: {exc}", location=node.location)
                )
                continue
            if thinness is None:
                continue
            checked += 1

            if thinness.vanishes:
                findings.append(
                    self.fail(
                        f"{node.label} is finer than {format_mm(minimum)} throughout — none of "
                        "it would survive stitching.",
                        hint="Thicken the shape, or drop it from the design.",
                        location=node.location,
                        min_mm=minimum,
                        area_mm2=round(thinness.area, 3),
                    )
                )
            elif thinness.excess_ratio > tolerance:
                findings.append(
                    self.fail(
                        f"{node.label} has detail finer than {format_mm(minimum)} "
                        f"({thinness.excess_ratio:.0%} of its area).",
                        hint="Thicken the thin parts; a needle cannot render a line narrower "
                        "than the stitch it makes.",
                        location=node.location,
                        min_mm=minimum,
                        excess_ratio=round(thinness.excess_ratio, 4),
                        area_mm2=round(thinness.area, 3),
                    )
                )

        if doc.width_mm is None:
            findings.append(
                self.warn(
                    "Physical document size is unknown, so feature sizes were measured with "
                    "the CSS default of 96 px per inch.",
                )
            )
        if unmeasured:
            findings.append(
                self.warn(
                    f"{unmeasured} element(s) have no outline to measure "
                    f"({', '.join(UNMEASURABLE_TAGS)}).",
                )
            )
        if not findings:
            if checked == 0:
                return [self.ok("No filled shapes — nothing to measure.")]
            return [
                self.ok(
                    f"All {checked} filled shape(s) are at least {format_mm(minimum)} thick "
                    f"(measured with {backend.name}).",
                    measured=True,
                )
            ]
        return findings


@register
class MinShapeAreaRule(Rule):
    """B5: shapes too small to hold a single stitch.

    The other half of :class:`MinFeatureSizeRule`, and it exists because that
    rule cannot see this defect. Thinness is measured over a whole element —
    one traced colour layer is one ``<path>`` — and forgiven up to a
    ``tolerance`` of its area, so fifty specks of a stitch each disappear into
    the rounding of a shape that is otherwise perfectly solid. **A tolerance is
    the right idea about a fraction of one shape and the wrong idea about a
    whole one:** every speck here is a thread trim, a knot and a jump stitch,
    whatever share of the design it represents.

    Two ways a shape gets too small, both reported, because a machine treats
    them the same way and a person does not:

    *An island* is a fleck of colour on its own — the quantiser rounding a few
    pixels the wrong way, or a scan's grain that survived denoising.
    *A hole* is a gap cut in a bigger shape. A needle cannot leave a hole
    smaller than the stitch it makes either, so it fills in — and until it
    does, the file says there is bare fabric there.

    Unlike ``geometry.min_feature_size`` this needs **no geometry backend**:
    the area of a flattened contour is the shoelace formula. So the check that
    catches what a tracer leaves behind is one that runs on a phone, which is
    why it can sit in a profile whose verdict must not vary by machine.
    """

    id = "geometry.min_area"
    summary = "Every shape must be big enough to hold a stitch"
    #: The square of the default minimum feature width: a stitch 1.5 mm wide
    #: needs at least that much in both directions to be worth making.
    params = {"min_mm2": 2.25}

    def validate(self) -> None:
        if float(self.config["min_mm2"]) <= 0:
            raise RuleConfigError(f"rule '{self.id}': min_mm2 must be positive")

    def check(self, doc: SvgDocument) -> Iterable[Finding]:
        from ..geometry import contour_area, node_contours_mm

        limit = float(self.config["min_mm2"])
        scale = doc.unit_scale
        findings: List[Finding] = []
        shapes = 0

        for node in doc.drawables:
            if node.paint("fill") is None and node.paint_reference("fill") is None:
                continue  # nothing is painted inside the outline
            contours = node_contours_mm(node, scale)
            if not contours:
                continue
            shapes += len(contours)

            areas = [contour_area(contour) for contour in contours]
            islands = [area for area in areas if 0 < area < limit]
            holes = [-area for area in areas if -limit < area < 0]
            if not islands and not holes:
                continue

            parts = []
            if islands:
                parts.append(f"{len(islands)} shape(s) as small as {min(islands):.2f} mm²")
            if holes:
                parts.append(f"{len(holes)} hole(s) as small as {min(holes):.2f} mm²")
            findings.append(
                self.fail(
                    f"{node.label} contains {' and '.join(parts)}, under the "
                    f"{limit:g} mm² a single stitch covers.",
                    hint="A speck that small stitches as a knot and a thread trim; "
                    "drop it, or draw it big enough to sew.",
                    location=node.location,
                    min_mm2=limit,
                    islands=len(islands),
                    holes=len(holes),
                )
            )

        if findings:
            if doc.width_mm is None:
                findings.append(
                    self.warn(
                        "Physical document size is unknown, so shape areas were measured "
                        "with the CSS default of 96 px per inch.",
                    )
                )
            return findings
        if shapes == 0:
            return [self.ok("No filled shapes — nothing to measure.")]
        return [
            self.ok(f"All {shapes} shape(s) are at least {limit:g} mm².", measured=True)
        ]


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
