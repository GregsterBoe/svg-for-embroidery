"""B5: removing shapes too small to sew.

Grouped by what these repairs *do* rather than by risk or by dependency: both
delete whole shapes, both edit a ``d`` attribute as text, and neither needs a
geometry backend — the area of a flattened contour is arithmetic, so the repair
that cleans up a trace runs on the same phone that produced it.

That is the difference from :mod:`svg_embroidery.fixes.geometry`, whose
``geometry.min_feature_size`` repair reshapes a boundary with boolean
operations. Reshaping is how you rescue a shape that is *mostly* fine; this is
how you drop one that was never viable. On a traced document the distinction is
worth real stitches: a boolean result comes back as a polyline, so rebuilding a
whole colour layer to remove a fleck costs more than the fleck ever did, while
deleting the fleck's subpath leaves every surviving curve byte for byte as the
tracer drew it.

Both are ``DESTRUCTIVE``. Nothing makes a small shape big without inventing
artwork, so the only honest repair is removal — and A5's rule holds: say so
loudly, and make the user ask for it by name.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple
from xml.etree import ElementTree

from ..document import Node, SvgDocument, iter_subpaths, local_name
from ..geometry import (
    DEFAULT_TOLERANCE_MM,
    contour_area,
    flatten_path,
    node_contours_mm,
)
from ._dom import parent_map, remove_elements
from .base import Decision, FixOutcome, Fixer, Option, Risk, register_fixer

#: Position used for a shape that is the whole element — a ``<rect>`` has no
#: subpaths to single out, so removing it means removing the element.
WHOLE_ELEMENT = -1


@dataclass(frozen=True)
class Shape:
    """One thing the machine would fill, and how much fabric it covers."""

    node: Node
    #: Which subpath of the element, or :data:`WHOLE_ELEMENT`.
    index: int
    #: Millimetres squared, unsigned.
    area: float
    #: True when this contour cuts a hole in the element rather than adding to
    #: it. Read from the winding direction relative to the element's largest
    #: contour, so it does not depend on which way the exporter draws.
    hole: bool = False


def _scale_squared(node: Node, unit_scale: float) -> float:
    """Millimetres squared per unit of local area."""
    factor = unit_scale * node.transform_scale
    return factor * factor


def element_shapes(node: Node, unit_scale: float) -> List[Shape]:
    """Every separate shape one element draws, measured in mm².

    Subpaths are flattened one at a time rather than through
    :func:`geometry.node_contours_mm`, because the repair edits the ``d``
    attribute as text: the measurement has to line up with a *chunk of the
    string*, so that everything not dropped survives exactly as it was written.
    """
    if node.tag != "path":
        contours = node_contours_mm(node, unit_scale)
        area = sum(abs(contour_area(contour)) for contour in contours)
        return [Shape(node=node, index=WHOLE_ELEMENT, area=area)] if area > 0 else []

    factor = _scale_squared(node, unit_scale)
    if factor <= 0:  # pragma: no cover - a degenerate transform
        return []
    tolerance = DEFAULT_TOLERANCE_MM / (unit_scale * node.transform_scale)

    signed: List[Tuple[int, float]] = []
    for index, chunk in enumerate(iter_subpaths(node.element.get("d") or "")):
        area = sum(contour_area(contour) for contour in flatten_path(chunk, tolerance))
        if area:
            signed.append((index, area * factor))
    if not signed:
        return []

    # The largest contour is the shape itself; anything wound the other way is
    # cut out of it. Reading the direction rather than assuming one means a
    # design drawn clockwise is not reported as one enormous hole.
    outer = max(signed, key=lambda item: abs(item[1]))[1]
    return [
        Shape(node=node, index=index, area=abs(area), hole=(area * outer) < 0)
        for index, area in signed
    ]


def document_shapes(doc: SvgDocument) -> List[Shape]:
    """Every filled shape in the document, in document order."""
    scale = doc.unit_scale
    found: List[Shape] = []
    for node in doc.drawables:
        if node.paint("fill") is None and node.paint_reference("fill") is None:
            continue  # nothing is painted inside the outline
        found.extend(element_shapes(node, scale))
    return found


def _emptied_groups(
    removed: Sequence[ElementTree.Element], parents: Dict[int, ElementTree.Element]
) -> List[ElementTree.Element]:
    """Groups *this* removal has left with nothing to draw.

    A traced document is one ``<g>`` per colour, so dropping a layer that was
    entirely speckle would otherwise leave an empty group behind — a colour in
    the file that paints nothing, which is exactly the sort of leftover the
    structure rules are there to complain about.

    Only the ancestors of what was just removed are considered. A group that was
    already empty when the file arrived is somebody else's business: this fixer
    reports what it did, and "removed a group" has to mean this removal emptied
    it.
    """
    gone = {id(element) for element in removed}
    doomed: List[ElementTree.Element] = []
    for element in removed:
        parent = parents.get(id(element))
        while parent is not None and local_name(parent.tag) == "g":
            if id(parent) in gone or id(parent) not in parents:
                break  # already going, or the root itself
            if any(id(child) not in gone for child in parent):
                break  # something in it survives
            gone.add(id(parent))
            doomed.append(parent)
            parent = parents.get(id(parent))
    return doomed


def drop_shapes(doc: SvgDocument, shapes: Sequence[Shape]) -> List[str]:
    """Delete the given shapes. Returns one description per element touched.

    Subpaths are cut out of the ``d`` attribute as text, so every contour that
    stays keeps the exact numbers the tracer (or the designer) wrote for it —
    the same principle A0 established for start tags, applied one level down.
    """
    by_element: Dict[int, List[Shape]] = {}
    for shape in shapes:
        by_element.setdefault(id(shape.node.element), []).append(shape)

    descriptions: List[str] = []
    doomed: List[ElementTree.Element] = []

    for group in by_element.values():
        node = group[0].node
        dropped = {shape.index for shape in group}
        holes = sum(1 for shape in group if shape.hole)
        islands = len(group) - holes
        smallest = min(shape.area for shape in group)

        if WHOLE_ELEMENT in dropped:
            doomed.append(node.element)
            descriptions.append(
                f"removed {node.label} — {smallest:.2f} mm², too small to stitch"
            )
            continue

        chunks = list(iter_subpaths(node.element.get("d") or ""))
        kept = [chunk for index, chunk in enumerate(chunks) if index not in dropped]
        if not kept:
            doomed.append(node.element)
            descriptions.append(
                f"removed {node.label} — every one of its {len(chunks)} shape(s) "
                "was too small to stitch"
            )
            continue

        node.element.set("d", " ".join(kept))
        parts = []
        if islands:
            parts.append(f"{islands} speck(s)")
        if holes:
            parts.append(f"{holes} hole(s)")
        descriptions.append(
            f"dropped {' and '.join(parts)} from {node.label}, "
            f"the smallest {smallest:.2f} mm²"
        )

    if doomed:
        parents = parent_map(doc.root)
        remove_elements(doc.root, doomed)
        empties = _emptied_groups(doomed, parents)
        if empties:
            remove_elements(doc.root, empties)
            descriptions.append(f"removed {len(empties)} group(s) left with nothing in them")
    return descriptions


@register_fixer
class DropSpecks(Fixer):
    """Delete every shape smaller than a single stitch.

    The repair B4 asked for by name. Closing the seams between colour layers
    stopped potrace from silently discarding one- and two-pixel islands, which
    was never a favour — the tracer left a hole where each had been. Now they
    are in the document, the checker can see them, and dropping them is a
    decision the run makes on purpose and writes down.

    **It asks, even though there is only one answer.** Deleting part of a
    drawing is not something to do on the strength of a flag that means "go
    ahead generally": A6 refuses a bare ``--allow destructive`` for exactly
    that reason, and A7 supplied the alternative — a question whose one option
    states the price, which ``--choose`` names, the terminal offers and the web
    UI can put on a button. The price is worth stating here, because "29
    specks, the largest 2.2 mm²" and "everything you drew" are the same
    sentence until someone reads the numbers.

    The budget is wide for the same reason ``WidenThinStrokes``' is: on a
    design that *is* speckle — a crosshatch, a grainy scan — removing every
    fleck legitimately repaints a good part of the image. What it still catches
    is the mistake this arithmetic can actually make, which is comparing
    millimetres to pixels and deleting the artwork.
    """

    rule_id = "geometry.min_area"
    risk = Risk.DESTRUCTIVE
    summary = "delete shapes too small to hold a stitch"
    visual_budget = 0.25
    OPTION = "drop"

    def _too_small(self, doc: SvgDocument) -> List[Shape]:
        limit = float(self.config["min_mm2"])
        return [shape for shape in document_shapes(doc) if shape.area < limit]

    def decision(self, doc: SvgDocument) -> Optional[Decision]:
        specks = self._too_small(doc)
        if not specks:
            return None
        limit = float(self.config["min_mm2"])
        holes = sum(1 for shape in specks if shape.hole)
        biggest = max(shape.area for shape in specks)
        return Decision(
            rule_id=self.rule_id,
            question=f"delete {len(specks)} shape(s) too small to stitch?",
            context=f"the largest is {biggest:.2f} mm², against the {limit:g} mm² a "
            f"stitch covers"
            + (
                f"; {holes} of them are holes, which fill in rather than disappear"
                if holes
                else ""
            ),
            options=[
                Option(
                    key=self.OPTION,
                    label=f"drop all {len(specks)}",
                    detail="each one costs a trim, a knot and a jump the machine "
                    "makes for something nobody can see",
                    risk=Risk.DESTRUCTIVE,
                    recommended=True,
                )
            ],
        )

    def apply(self, doc: SvgDocument) -> FixOutcome:
        outcome = FixOutcome()
        if self.choice != self.OPTION:  # pragma: no cover - the engine gates this
            return outcome.decline("no answer was given")
        too_small = self._too_small(doc)
        if not too_small:
            return outcome.decline("every shape is already big enough to stitch")
        for description in drop_shapes(doc, too_small):
            outcome.add(description)
        return outcome


@register_fixer
class ReduceShapeCount(Fixer):
    """Cut the number of separate shapes down to what the profile allows.

    This one **asks**, and the reason is the price. A design over the limit is
    over it for one of two very different reasons: it is carrying speckle the
    quantiser invented, or it is genuinely that detailed. The repair is the
    same operation either way — drop the smallest shapes until the count fits —
    but in the first case it removes flecks nobody drew and in the second it
    starts taking pieces of the artwork. Only the person looking at it can say
    which, so the option names the largest thing it would delete and lets them
    decide. A7's mechanism, filling one of the four gaps that step left open.

    There is deliberately no second source of truth about how small is too
    small: the threshold is not a parameter here, it is whatever this document
    needs to get under this profile's ``max_paths``. ``geometry.min_area`` is
    the rule that owns "smaller than a stitch", and where a profile has both,
    it runs first and this usually finds nothing left to do.
    """

    rule_id = "path.max_count"
    risk = Risk.DESTRUCTIVE
    summary = "drop the smallest shapes until the design is within the limit"
    visual_budget = 0.25
    #: The only answer, but naming it keeps ``--choose`` honest and lets the
    #: report print the price next to the question.
    OPTION = "drop-smallest"

    def _casualties(self, doc: SvgDocument) -> List[Shape]:
        """The smallest shapes, enough of them to get under the limit.

        Empty when there are not enough to get there — most of a document can
        be shapes this repair cannot touch (an unfilled outline is not one of
        them), and deleting artwork to land *still* over the limit is the worst
        of both. Declining says so instead.
        """
        from ..rules.path_rules import shape_count

        limit = int(self.config["max_paths"])
        excess = shape_count(doc) - limit
        if excess <= 0:
            return []
        shapes = sorted(document_shapes(doc), key=lambda shape: shape.area)
        return shapes[:excess] if excess <= len(shapes) else []

    def decision(self, doc: SvgDocument) -> Optional[Decision]:
        casualties = self._casualties(doc)
        if not casualties:
            return None
        biggest = max(shape.area for shape in casualties)
        return Decision(
            rule_id=self.rule_id,
            question=f"drop the {len(casualties)} smallest shape(s) to get within "
            f"{self.config['max_paths']}?",
            context=f"the largest one removed would be {biggest:.2f} mm², and every "
            "shape removed is a trim and a jump the machine no longer makes",
            options=[
                Option(
                    key=self.OPTION,
                    label=f"drop the {len(casualties)} smallest",
                    detail=f"deletes shapes up to {biggest:.2f} mm²; anything bigger "
                    "is left alone even if the count is still over",
                    risk=Risk.DESTRUCTIVE,
                    recommended=True,
                )
            ],
        )

    def apply(self, doc: SvgDocument) -> FixOutcome:
        outcome = FixOutcome()
        if self.choice != self.OPTION:  # pragma: no cover - the engine gates this
            return outcome.decline("no answer was given")
        casualties = self._casualties(doc)
        if not casualties:
            return outcome.decline("the design is already within the limit")
        for description in drop_shapes(doc, casualties):
            outcome.add(description)
        return outcome
