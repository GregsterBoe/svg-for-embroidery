"""Safe fixes: repairs that cannot change the rendered image.

Every fixer here is held to the contract in :mod:`svg_embroidery.fixes.verify`
— fixes its rule, breaks nothing else, idempotent, and **zero pixels moved**.
That last one is checked against a real renderer, not asserted.

The recurring trick is to only remove things that were never drawing anything:
an unreferenced gradient, a filter nothing points at, editor state. Where a
repair would genuinely change the picture — deleting a visible ``<text>``,
quantising colours — the fixer declines and says why. Those are lossy fixes,
and they belong to step A4.
"""

from __future__ import annotations

import math
from typing import Optional

from ..document import SvgDocument, local_name
from ..rules.structure import _editor_namespace
from ..units import MM_PER_INCH, UNIT_TO_MM, USER_UNITS_PER_INCH, cm_to_mm, parse_length
from ._dom import (
    is_unrendered,
    parent_map,
    prune_unreferenced,
    referenced_ids,
    remove_elements,
)
from .base import FixOutcome, Fixer, Risk, register_fixer


def _format_number(value: float) -> str:
    """Compact decimal: ``453.543307`` not ``453.5433070866142``."""
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _implied_view_box(doc: SvgDocument) -> Optional[str]:
    """The viewBox a renderer already assumes from width/height, or ``None``."""
    width = parse_length(doc.root.get("width"))
    height = parse_length(doc.root.get("height"))
    if width is None or height is None or width.is_relative or height.is_relative:
        return None
    width_mm, height_mm = width.to_mm(), height.to_mm()
    if not width_mm or not height_mm:
        return None
    per_unit = MM_PER_INCH / USER_UNITS_PER_INCH
    return f"0 0 {_format_number(width_mm / per_unit)} {_format_number(height_mm / per_unit)}"


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

        view_box = _implied_view_box(doc)
        if view_box is None:
            return outcome.decline("width/height do not resolve to a physical size")

        doc.root.set("viewBox", view_box)
        outcome.add(f'added viewBox="{view_box}"', location="/svg")
        return outcome


def _round_up(value: float, places: int = 4) -> float:
    factor = 10 ** places
    return math.ceil(value * factor - 1e-9) / factor


def _round_down(value: float, places: int = 4) -> float:
    factor = 10 ** places
    return math.floor(value * factor + 1e-9) / factor


@register_fixer
class ScaleCanvas(Fixer):
    """Scale the canvas into the size range the shop accepts.

    Only ``width`` and ``height`` change; the viewBox is left alone, so the
    artwork keeps its proportions and every coordinate inside stays as drawn.
    The design is simply presented at a different physical size.

    A document with no viewBox gets one first (the same one the renderer was
    already assuming). Without that, changing width/height would enlarge the
    canvas around the artwork instead of scaling it — a visible change, and not
    what anyone means by "make it bigger".
    """

    rule_id = "geometry.canvas_size"
    risk = Risk.SAFE
    summary = "scale width/height into the allowed range"

    def _bounds_mm(self, axis: str):
        low = self.config[f"min_{axis}_cm"]
        high = self.config[f"max_{axis}_cm"]
        low = low if low is not None else self.config["min_cm"]
        high = high if high is not None else self.config["max_cm"]
        return (
            None if low is None else cm_to_mm(float(low)),
            None if high is None else cm_to_mm(float(high)),
        )

    def apply(self, doc: SvgDocument) -> FixOutcome:
        outcome = FixOutcome()
        if doc.width_mm is None or doc.height_mm is None:
            return outcome.decline("the physical size cannot be determined")

        width = parse_length(doc.root.get("width"))
        height = parse_length(doc.root.get("height"))
        if width is None or height is None or width.is_relative or height.is_relative:
            return outcome.decline("width/height are missing or relative")

        min_w, max_w = self._bounds_mm("width")
        min_h, max_h = self._bounds_mm("height")

        # Feasible scale factors, as an interval.
        lowest = max(
            (min_w / doc.width_mm) if min_w else 0.0,
            (min_h / doc.height_mm) if min_h else 0.0,
        )
        highest = min(
            (max_w / doc.width_mm) if max_w else math.inf,
            (max_h / doc.height_mm) if max_h else math.inf,
        )
        if lowest > highest:
            return outcome.decline(
                "no single scale satisfies both axes — the aspect ratio does not fit "
                "the shop's limits, so the design has to be recomposed"
            )

        # Scale as little as possible: stay at 1 if 1 is allowed.
        scale = min(max(1.0, lowest), highest)
        if abs(scale - 1.0) < 1e-9:
            return outcome.decline("the canvas is already inside the allowed range")

        if not doc.root.get("viewBox"):
            view_box = _implied_view_box(doc)
            if view_box is None:
                return outcome.decline(
                    "the document has no viewBox and none can be derived, so resizing "
                    "would move the artwork rather than scale it"
                )
            doc.root.set("viewBox", view_box)
            outcome.add(f'added viewBox="{view_box}" so the artwork scales with the canvas')

        rounder = _round_up if scale > 1 else _round_down
        for axis, length, size_mm in (
            ("width", width, doc.width_mm),
            ("height", height, doc.height_mm),
        ):
            unit = length.unit if length.unit in UNIT_TO_MM else "mm"
            per_unit = UNIT_TO_MM[unit] or 1.0
            new_value = rounder(size_mm * scale / per_unit)
            text = f"{_format_number(new_value)}{unit}"
            doc.root.set(axis, text)
            outcome.add(
                f"{axis} {length.value:g}{length.unit or 'px'} -> {text}", location="/svg"
            )
        return outcome


@register_fixer
class DropUnreferencedGradients(Fixer):
    """Delete gradient and pattern definitions nothing points at.

    Exporters leave these behind constantly. An unreferenced definition paints
    nothing, so removing it is invisible — and it is often the only reason the
    file trips the no-gradients rule at all. A gradient that *is* referenced
    stays: replacing it with a flat colour changes the picture, which makes it
    a lossy fix (step A4).
    """

    rule_id = "color.no_gradients"
    risk = Risk.SAFE
    summary = "delete gradient/pattern definitions nothing references"

    def apply(self, doc: SvgDocument) -> FixOutcome:
        outcome = FixOutcome()
        tags = [str(tag) for tag in self.config["tags"]]
        removed = prune_unreferenced(doc.root, tags)
        if not removed:
            live = [node for node in doc.by_tag(*tags)]
            if live:
                names = ", ".join(sorted({node.tag for node in live}))
                return outcome.decline(
                    f"{len(live)} {names} definition(s) are still referenced; replacing "
                    "them with flat colour would change the design"
                )
            return outcome.decline("no gradient definitions to remove")

        for element in removed:
            outcome.add(
                f"removed unreferenced <{local_name(element.tag)}>"
                + (f' id="{element.get("id")}"' if element.get("id") else "")
            )
        return outcome


@register_fixer
class DropUnrenderedForbiddenElements(Fixer):
    """Remove forbidden elements that were never being drawn.

    A ``<filter>`` in ``<defs>`` that nothing references, a hidden ``<image>``,
    a leftover ``<clipPath>`` — all dead weight, all safe to delete. Anything
    actually on the canvas is left alone with an explanation: deleting visible
    artwork is not a repair.
    """

    rule_id = "element.forbidden"
    risk = Risk.SAFE
    summary = "remove forbidden elements that draw nothing"

    def apply(self, doc: SvgDocument) -> FixOutcome:
        outcome = FixOutcome()
        tags = [str(tag) for tag in self.config["tags"]]
        parents = parent_map(doc.root)
        references = referenced_ids(doc.root)

        dead = []
        alive = []
        for node in doc.by_tag(*tags):
            element = node.element
            if not is_unrendered(element, parents):
                alive.append(node)
            elif element.get("id") and element.get("id") in references:
                alive.append(node)
            else:
                dead.append(node)

        if not dead:
            if alive:
                kinds = ", ".join(sorted({f"<{node.tag}>" for node in alive}))
                return outcome.decline(
                    f"{kinds} are part of the artwork; removing them would delete visible "
                    "content, so that is a manual decision"
                )
            return outcome.decline("nothing to remove")

        remove_elements(doc.root, [node.element for node in dead])
        for node in dead:
            outcome.add(f"removed unrendered <{node.tag}>", location=node.location)
        if alive:
            kinds = ", ".join(sorted({f"<{node.tag}>" for node in alive}))
            outcome.add(f"left {kinds} alone — they are visible artwork")
        return outcome


@register_fixer
class StripEditorMetadata(Fixer):
    """Drop editor state: ``<metadata>``, ``<sodipodi:namedview>``, ``inkscape:*``.

    None of it renders. It bloats the upload and can carry the file name and
    user paths from the machine it was drawn on. Structural annotations that
    other rules rely on — ``inkscape:groupmode``, ``inkscape:label``, which mark
    layers — are kept.
    """

    rule_id = "document.no_editor_metadata"
    risk = Risk.SAFE
    summary = "remove editor state, keeping layer annotations"

    def apply(self, doc: SvgDocument) -> FixOutcome:
        outcome = FixOutcome()
        keep = {str(name) for name in self.config["keep_attributes"]}

        doomed = [
            node.element
            for node in doc.nodes
            if node.tag == "metadata" or _editor_namespace(node.element.tag)
        ]
        # Only outermost elements: removing a parent takes its children with it.
        parents = parent_map(doc.root)
        doomed_ids = {id(element) for element in doomed}
        outermost = []
        for element in doomed:
            ancestor = parents.get(id(element))
            while ancestor is not None and id(ancestor) not in doomed_ids:
                ancestor = parents.get(id(ancestor))
            if ancestor is None:
                outermost.append(element)

        for element in outermost:
            outcome.add(f"removed <{local_name(element.tag)}>")
        remove_elements(doc.root, outermost)

        for element in [doc.root] + [node.element for node in doc.nodes]:
            stale = [
                name
                for name in element.attrib
                if _editor_namespace(name) and local_name(name) not in keep
            ]
            for name in stale:
                del element.attrib[name]
            if stale:
                outcome.add(
                    f"removed {len(stale)} editor attribute(s) from <{local_name(element.tag)}>"
                )

        if not outcome.applied:
            return outcome.decline("no editor metadata found")
        return outcome
