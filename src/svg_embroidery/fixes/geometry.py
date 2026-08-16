"""A5: the repairs that need real path maths.

The earlier batches were grouped by risk — :mod:`safe` cannot move a pixel,
:mod:`lossy` moves them on purpose. These three are grouped by what they *need*
instead, because that is the interesting thing about them: they all stand on
:mod:`svg_embroidery.geometry`, and two of the three are unavailable on a
machine with no backend. Keeping them together means there is one place to look
when the answer is "install the geometry extra", and one place where the
offsetting conventions live.

They still declare their own risk, and the engine treats them like any other:

``stroke.min_width`` [lossy]
    Thicken a hairline to the width the shop can stitch. Needs no backend at
    all — the arithmetic is a unit conversion — so the repair people actually
    hit most often keeps working on a phone.
``stroke.forbidden`` [lossy]
    Redraw the stroke as the filled shape it paints. This is what offsetting
    is for, and it is the conversion every "outlines must be filled paths"
    shop asks for by hand.
``geometry.min_feature_size`` [destructive]
    Open out detail too fine to stitch. Destructive because it deletes artwork
    the designer drew: there is no operation that makes a thin thing thick
    without inventing shape, so the honest repair is to remove what the needle
    was going to drop anyway — and to say so loudly.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree

from ..document import (
    SVG_NS,
    Node,
    SvgDocument,
    apply_matrix,
    invert_matrix,
    parse_style,
)
from ..geometry import (
    DEFAULT_TOLERANCE_MM,
    GeometryError,
    INSTALL_HINT,
    Contour,
    contours_to_path_data,
    default_backend,
    measure_thinness,
    node_contours,
    node_contours_mm,
    simplify_contours,
    stroke_outline,
    trim_thin_detail,
)
from ..units import UNIT_TO_MM, format_mm, parse_length
from ._dom import parent_map, remove_elements
from .base import FixOutcome, Fixer, Risk, register_fixer

#: Paint that is a reference (``url(#grad)``) rather than a colour. Offsetting
#: cannot carry a gradient across, so those are declined rather than flattened.
_NOT_A_COLOUR = "a paint server"


def _style_text(style: Dict[str, str]) -> str:
    return ";".join(f"{name}:{value}" for name, value in style.items())


def _set_property(element: ElementTree.Element, name: str, value: str) -> None:
    """Write a presentation property where this element already keeps it.

    An element that says ``style="stroke-width:0.2"`` must be edited in its
    ``style``; setting the attribute instead would be overridden by it and the
    fix would silently do nothing.
    """
    style = parse_style(element.get("style"))
    if name in style:
        style[name] = value
        element.set("style", _style_text(style))
    else:
        element.set(name, value)


def _new_path(d: str, tail: Optional[str] = None, **attributes: str) -> ElementTree.Element:
    """A fresh ``<path>`` in the SVG namespace, indented like its neighbour."""
    element = ElementTree.Element(f"{{{SVG_NS}}}path")
    for name, value in attributes.items():
        if value is not None:
            element.set(name.replace("_", "-"), value)
    element.set("d", d)
    if tail is not None:
        element.tail = tail
    return element


def _local_tolerance(node: Node, unit_scale: float) -> float:
    """``DEFAULT_TOLERANCE_MM`` expressed in this element's own units."""
    factor = unit_scale * node.transform_scale
    return DEFAULT_TOLERANCE_MM / factor if factor > 0 else DEFAULT_TOLERANCE_MM


@register_fixer
class WidenThinStrokes(Fixer):
    """Thicken every stroke up to the minimum the shop can stitch.

    The whole repair is a unit conversion, so it needs no geometry backend and
    works wherever the checker does. It writes the width where the element
    already keeps it — a ``style`` declaration beats an attribute, so editing
    the attribute of an element styled inline would change nothing at all.

    The budget is wide because thickening is the point: a line drawing whose
    every hairline triples in weight legitimately repaints a good part of
    itself. What the budget still catches is a fix that repainted the *canvas*
    — a width written in the wrong unit, which is the mistake this arithmetic
    can actually make.
    """

    rule_id = "stroke.min_width"
    risk = Risk.LOSSY
    summary = "thicken strokes up to the minimum stitchable width"
    visual_budget = 0.25

    def apply(self, doc: SvgDocument) -> FixOutcome:
        outcome = FixOutcome()
        minimum = float(self.config["min_mm"])
        scale = doc.unit_scale
        unresolved = 0

        for node in doc.drawables:
            width = node.stroke_width_mm(scale)
            if width is None:
                if node.paint("stroke") is not None or node.paint_reference("stroke"):
                    unresolved += 1
                continue
            if width >= minimum - 1e-9:
                continue

            length = parse_length(node.style.get("stroke-width", "1"))
            if length is None:  # pragma: no cover - stroke_width_mm already parsed it
                continue
            # Keep the unit the design uses. One unit is worth this many mm:
            per_unit = UNIT_TO_MM.get(length.unit) if length.unit not in ("", "px") else scale
            if not per_unit:
                unresolved += 1
                continue

            # Round up, so re-checking cannot land a hair under the limit.
            wanted = (minimum / node.transform_scale) / per_unit
            wanted = math.ceil(wanted * 1e4 - 1e-9) / 1e4
            text = f"{wanted:.4f}".rstrip("0").rstrip(".") + length.unit
            _set_property(node.element, "stroke-width", text)
            outcome.add(
                f"stroke {format_mm(width)} -> {format_mm(minimum)}", location=node.location
            )

        if not outcome.applied:
            if unresolved:
                return outcome.decline(
                    f"{unresolved} stroke width(s) are in units that cannot be resolved "
                    "(relative to a font or the viewport), so the target width is unknown"
                )
            return outcome.decline("every stroke is already wide enough")
        if unresolved:
            outcome.add(f"left {unresolved} unresolvable stroke width(s) alone")
        return outcome


def _paint_of(node: Node, prop: str) -> Optional[str]:
    """The colour painted, or ``None`` when it is a reference or absent."""
    if node.paint_reference(prop) is not None:
        return None
    return node.paint(prop)


@register_fixer
class StrokeToOutline(Fixer):
    """Redraw each stroke as the filled shape it paints.

    What a shop means by "filled paths only" is this: the outline a plotter or
    a satin stitch follows has to be the boundary of an area, not a centreline
    with a width. Doing it by hand is Inkscape's *Path ▸ Stroke to Path*; doing
    it here is offsetting the flattened contour by half the stroke width, with
    the cap and join styles the design asked for.

    The new fill goes in as a sibling *after* the original, because that is
    where a stroke paints — over its own fill. An element that had no fill was
    only ever its stroke, so it is replaced outright rather than left behind
    painting nothing.

    Lossy rather than safe by a hair: curves are flattened to within
    :data:`geometry.DEFAULT_TOLERANCE_MM` and round joins become polygons, so
    edges land a fraction of a stitch away from where they were. The budget is
    tight enough that anything worse than that is caught.
    """

    rule_id = "stroke.forbidden"
    risk = Risk.LOSSY
    summary = "convert strokes into filled outlines"
    visual_budget = 0.05

    def apply(self, doc: SvgDocument) -> FixOutcome:
        outcome = FixOutcome()
        backend = default_backend()
        if backend is None:
            return outcome.decline(
                f"converting a stroke to an outline needs path geometry — {INSTALL_HINT}"
            )

        scale = doc.unit_scale
        declined: List[str] = []
        # Collect first, mutate after: inserting siblings while walking the
        # node list would have the fixer trip over its own output.
        work: List[Tuple[Node, ElementTree.Element]] = []

        for node in doc.drawables:
            if node.paint("stroke") is None and node.paint_reference("stroke") is None:
                continue
            colour = _paint_of(node, "stroke")
            if colour is None:
                declined.append(f"{node.label} strokes with {_NOT_A_COLOUR}")
                continue
            if node.style.get("stroke-dasharray", "none").strip().lower() != "none":
                declined.append(f"{node.label} has a dash pattern")
                continue

            width_mm = node.stroke_width_mm(scale)
            if width_mm is None or width_mm <= 0:
                declined.append(f"{node.label} has an unresolvable stroke width")
                continue
            local_width = width_mm / (scale * node.transform_scale)

            contours = node_contours(node, _local_tolerance(node, scale))
            if not contours:
                declined.append(f"{node.label} has no outline to offset")
                continue

            try:
                outline = stroke_outline(
                    contours,
                    local_width,
                    cap=node.style.get("stroke-linecap", "butt"),
                    join=node.style.get("stroke-linejoin", "miter"),
                    backend=backend,
                )
            except GeometryError as exc:
                declined.append(f"{node.label}: {exc}")
                continue
            d = contours_to_path_data(outline or [])
            if not d:
                declined.append(f"{node.label} produced an empty outline")
                continue

            work.append(
                (
                    node,
                    _new_path(
                        d,
                        tail=node.element.tail,
                        fill=colour,
                        fill_opacity=node.style.get("stroke-opacity"),
                        stroke="none",
                        transform=node.element.get("transform"),
                    ),
                )
            )

        if not work:
            if declined:
                return outcome.decline("; ".join(declined))
            return outcome.decline("nothing is stroked")

        parents = parent_map(doc.root)
        for node, outline_element in work:
            parent = parents.get(id(node.element))
            if parent is None:  # pragma: no cover - drawables always have a parent
                continue
            position = list(parent).index(node.element)
            keeps_fill = (
                node.paint("fill") is not None or node.paint_reference("fill") is not None
            )
            if keeps_fill:
                _set_property(node.element, "stroke", "none")
                parent.insert(position + 1, outline_element)
            else:
                outline_element.tail = node.element.tail
                parent.remove(node.element)
                parent.insert(position, outline_element)
            outcome.add(
                f"stroke of {format_mm(node.stroke_width_mm(scale) or 0.0)} "
                f"redrawn as a filled outline",
                location=node.location,
            )

        if declined:
            outcome.add(f"left {len(declined)} stroke(s) alone: {'; '.join(declined)}")
        return outcome


@register_fixer
class RemoveThinFeatures(Fixer):
    """Open out the detail a needle cannot render.

    Destructive on purpose, and it is the only fixer that is. Nothing turns a
    thin thing into a thick one without inventing shape the designer did not
    draw, so the repair is the other half of the check: take the part of the
    shape a stitch-wide disc can reach, and drop the rest. A shape that is *all*
    hairline goes altogether — it was never going to appear on the fabric.

    Two consequences, both stated rather than hidden. The shape comes back as
    straight segments, because a boolean result has no curves in it; and only
    the shapes that actually failed are touched, so the rest of the file keeps
    its beziers. A stroked shape is declined outright: its stroke is part of
    what gets painted, so the honest order is ``stroke.forbidden`` first and
    this afterwards.
    """

    rule_id = "geometry.min_feature_size"
    risk = Risk.DESTRUCTIVE
    summary = "remove detail too fine to stitch"
    visual_budget = 0.25

    def apply(self, doc: SvgDocument) -> FixOutcome:
        outcome = FixOutcome()
        backend = default_backend()
        if backend is None:
            return outcome.decline(
                f"finding fine detail needs path geometry — {INSTALL_HINT}"
            )

        minimum = float(self.config["min_mm"])
        tolerance = float(self.config["tolerance"])
        scale = doc.unit_scale
        declined: List[str] = []
        doomed: List[ElementTree.Element] = []
        reshaped: List[Tuple[Node, str]] = []

        for node in doc.drawables:
            if node.paint("fill") is None and node.paint_reference("fill") is None:
                continue
            contours = node_contours_mm(node, scale)
            if not contours:
                continue
            if node.paint("stroke") is not None or node.paint_reference("stroke") is not None:
                declined.append(f"{node.label} is stroked")
                continue

            try:
                thinness = measure_thinness(contours, minimum, backend=backend)
                if thinness is None:
                    continue
                if thinness.vanishes:
                    doomed.append(node.element)
                    outcome.add(
                        f"removed {node.label} — finer than {format_mm(minimum)} throughout",
                        location=node.location,
                    )
                    continue
                if thinness.excess_ratio <= tolerance:
                    continue
                # Simplify what comes back (B5). A boolean result is a polygon
                # at whatever density its input had, and its input was a curve
                # flattened to a fiftieth of a millimetre — so every boundary
                # the repair *did not* touch returns carrying a point every
                # fraction of a stitch. Thinning them at the tolerance the
                # shape was measured at adds no error the measurement did not
                # already have, and each point saved is a stitch.
                survivors = simplify_contours(
                    trim_thin_detail(contours, minimum, backend=backend) or [],
                    DEFAULT_TOLERANCE_MM,
                )
            except GeometryError as exc:
                declined.append(f"{node.label}: {exc}")
                continue

            local = self._to_local(node, survivors or [], scale)
            if local is None:
                declined.append(f"{node.label} has a transform that cannot be inverted")
                continue
            d = contours_to_path_data(local)
            if not d:  # pragma: no cover - a non-vanishing shape has survivors
                continue
            reshaped.append((node, d))
            outcome.add(
                f"removed {thinness.excess_ratio:.0%} of {node.label} that was finer "
                f"than {format_mm(minimum)}",
                location=node.location,
            )

        if not outcome.applied:
            if declined:
                return outcome.decline("; ".join(declined))
            return outcome.decline("no shape has detail below the limit")

        parents = parent_map(doc.root)
        for node, d in reshaped:
            self._rewrite(node, d, parents)
        remove_elements(doc.root, doomed)

        if declined:
            outcome.add(f"left {len(declined)} shape(s) alone: {'; '.join(declined)}")
        return outcome

    def _to_local(
        self, node: Node, contours: List[Contour], scale: float
    ) -> Optional[List[Contour]]:
        """Bring a millimetre result back into the element's own coordinates."""
        inverse = invert_matrix(node.matrix)
        if inverse is None or scale <= 0:
            return None
        result: List[Contour] = []
        for contour in contours:
            points = [
                apply_matrix(inverse, (x / scale, y / scale)) for x, y in contour.points
            ]
            result.append(Contour(points=points, closed=True))
        return result

    def _rewrite(self, node: Node, d: str, parents) -> None:
        """Put the opened shape back, as a ``<path>`` whatever it started as."""
        if node.tag == "path":
            node.element.set("d", d)
            return
        parent = parents.get(id(node.element))
        if parent is None:  # pragma: no cover - drawables always have a parent
            return
        replacement = _new_path(d, tail=node.element.tail)
        # Carry over everything that is not the shape's own geometry; the
        # geometry is exactly what we just replaced.
        geometry_attributes = {
            "d", "x", "y", "width", "height", "rx", "ry", "cx", "cy", "r",
            "x1", "y1", "x2", "y2", "points",
        }
        for name, value in node.element.attrib.items():
            if name not in geometry_attributes:
                replacement.set(name, value)
        replacement.set("d", d)
        position = list(parent).index(node.element)
        parent.remove(node.element)
        parent.insert(position, replacement)
