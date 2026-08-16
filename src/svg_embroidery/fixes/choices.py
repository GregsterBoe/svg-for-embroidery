"""A7: repairs that need an answer before they can run.

The other fixer modules are grouped by risk or by what they need. These are
grouped by the thing that actually distinguishes them: **they ask first.**

A rule can fail for a reason no tool may settle. The shop wants one colour per
layer and the artwork has none — so shapes have to be regrouped, and only the
person who drew it knows whether the order things overlap in is load-bearing.
Reporting "no automatic fix available" turns that into a dead end. Reporting
*the question, with the answers the tool can carry out*, does not.

Each answer prices itself. Keeping the drawing order costs nothing and is
provably pixel-identical; collapsing to one layer per colour reorders artwork
and is destructive. Same question, two honest answers, and the risk budget
decides which of them are even offered.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple
from xml.etree import ElementTree

from ..colors import hex_to_rgb, normalize_color, rgb_to_hex
from ..document import (
    SVG_NS,
    Node,
    SvgDocument,
    iter_subpaths,
    local_name,
    parse_style,
)
from ..units import format_mm
from ._dom import (
    is_unrendered,
    parent_map,
    prune_unreferenced,
    referenced_ids,
    remove_elements,
)
from ._path import close_subpath_text, rebuild, subpaths
from .base import Decision, FixOutcome, Fixer, Option, Risk, register_fixer

#: Elements at the top of a document that are not artwork and never move.
_NOT_ARTWORK = frozenset({"defs", "metadata", "title", "desc", "style", "script"})


def _is_artwork(element: ElementTree.Element) -> bool:
    return isinstance(element.tag, str) and local_name(element.tag) not in _NOT_ARTWORK


def _indent_of(text: Optional[str]) -> str:
    """The indentation a block of text ends with, for matching a neighbour."""
    if not text or "\n" not in text:
        return ""
    return text.rsplit("\n", 1)[1]


def _nest(group: ElementTree.Element, indent: str) -> None:
    """Lay a new group's children out one per line, indented under it."""
    inner = indent + "  "
    group.text = "\n" + inner
    children = list(group)
    for child in children[:-1]:
        child.tail = "\n" + inner
    if children:
        children[-1].tail = "\n" + indent


class _Unit:
    """One top-level thing that draws, and the colours it paints with."""

    def __init__(self, element: ElementTree.Element, colors: Set[str]) -> None:
        self.element = element
        self.colors = colors

    @property
    def color(self) -> str:
        return next(iter(self.colors)) if self.colors else ""


@register_fixer
class SeparateColorLayers(Fixer):
    """Put every colour on its own layer — the way the answer says to.

    Shops ask for this so they can separate the design into one stitch file per
    thread. There are two ways to give it to them and they are not equivalent:

    *Keep the drawing order* wraps each **run** of same-coloured shapes in its
    own group. Nothing moves, so the render is untouched — provably, since the
    engine renders before and after. A design that alternates red, blue, red
    comes out as three layers rather than two, which still satisfies the rule:
    it asks that no layer mix colours, not that no colour repeat.

    *One layer per colour* gives exactly one group per thread, which is what a
    shop's tooling usually wants, by moving shapes past each other. Wherever two
    colours overlap, the one on top changes. That is destructive and is offered
    as such.

    The class-level risk is the cheapest answer, because that is what the engine
    gates on before the question is asked; the answer chosen carries the real
    one.
    """

    rule_id = "structure.color_layers"
    risk = Risk.SAFE
    summary = "group shapes into one layer per colour"
    #: Reordering can repaint an overlap; keeping the order cannot move a pixel,
    #: and the engine holds each answer to the risk it declared.
    visual_budget = 0.35

    # -- reading the document ---------------------------------------------
    def _colors_of(self, doc: SvgDocument, node: Node) -> Set[str]:
        colors = set()
        candidates = [node] if node.is_drawable else list(doc.descendants(node))
        for candidate in candidates:
            if not candidate.is_drawable or not candidate.is_visible:
                continue
            for prop in ("fill", "stroke"):
                color = candidate.paint(prop)
                if color is not None:
                    colors.add(color)
        return colors

    def _units(self, doc: SvgDocument, parent: ElementTree.Element) -> Optional[List[_Unit]]:
        """The artwork directly under ``parent``, or ``None`` if we can't tell.

        ``None`` means something here paints in more than one colour and cannot
        be put on a single-colour layer without being taken apart — a different
        operation, and not one to do behind someone's back.
        """
        by_element = {id(node.element): node for node in doc.nodes}
        units: List[_Unit] = []
        for child in parent:
            if not _is_artwork(child):
                continue
            node = by_element.get(id(child))
            if node is None:  # pragma: no cover - every element is a node
                return None
            colors = self._colors_of(doc, node)
            if not colors:
                continue  # paints nothing; it can sit wherever it already is
            if len(colors) > 1:
                return None
            units.append(_Unit(child, colors))
        return units

    def _plan(self, doc: SvgDocument):
        """What has to change, or ``None`` when this fixer cannot help.

        Returns ``(parent, units, loose)`` where ``loose`` is the artwork not
        already sitting on a layer of its own.
        """
        units = self._units(doc, doc.root)
        if units is None or not units:
            return None
        loose = [unit for unit in units if local_name(unit.element.tag) != "g"]
        mixed = [
            unit
            for unit in units
            if local_name(unit.element.tag) == "g" and len(unit.colors) > 1
        ]
        if mixed:  # pragma: no cover - _units already rejects multi-colour units
            return None
        if not loose:
            return None  # every colour is already grouped; nothing to do here
        return doc.root, units, loose

    @staticmethod
    def _runs(units: Sequence[_Unit]) -> List[List[_Unit]]:
        """Consecutive same-coloured artwork, in the order it is painted."""
        runs: List[List[_Unit]] = []
        for unit in units:
            if runs and runs[-1][0].color == unit.color:
                runs[-1].append(unit)
            else:
                runs.append([unit])
        return runs

    # -- the question ------------------------------------------------------
    def decision(self, doc: SvgDocument) -> Optional[Decision]:
        plan = self._plan(doc)
        if plan is None:
            return None
        _, units, _ = plan
        runs = self._runs(units)
        colors = sorted({unit.color for unit in units})
        context = (
            f"{len(colors)} colours ({', '.join(colors)}) across "
            f"{len(units)} top-level shapes, in {len(runs)} runs of colour."
        )

        if len(runs) == len(colors):
            # Already sorted by colour: grouping moves nothing, so there is one
            # answer and it is free. Still worth asking — it restructures the
            # file — but not worth pretending it is a trade-off.
            return Decision(
                rule_id=self.rule_id,
                question="Wrap each colour in its own layer?",
                context=context,
                options=[
                    Option(
                        key="colors",
                        label=f"Yes — {len(colors)} layers, one per colour",
                        detail="The shapes are already ordered by colour, so nothing "
                        "moves and the design renders identically.",
                        risk=Risk.SAFE,
                        recommended=True,
                    )
                ],
            )

        return Decision(
            rule_id=self.rule_id,
            question="How should the colours be separated into layers?",
            context=context,
            options=[
                Option(
                    key="runs",
                    label=f"Keep the drawing order — {len(runs)} layers",
                    detail=f"Wraps each run of same-coloured shapes, so {len(colors)} "
                    f"colours become {len(runs)} layers and a colour may appear on "
                    "more than one. Nothing is reordered: the design renders "
                    "exactly as it does now.",
                    risk=Risk.SAFE,
                    recommended=True,
                ),
                Option(
                    key="colors",
                    label=f"One layer per colour — {len(colors)} layers",
                    detail="Exactly one layer per thread, which is what most shops' "
                    "tooling expects. Shapes move past each other to get there, so "
                    "wherever two colours overlap the one on top changes.",
                    risk=Risk.DESTRUCTIVE,
                ),
            ],
        )

    # -- carrying it out ---------------------------------------------------
    def apply(self, doc: SvgDocument) -> FixOutcome:
        outcome = FixOutcome()
        plan = self._plan(doc)
        if plan is None:
            return outcome.decline(
                "the top-level shapes do not sort into single-colour layers — "
                "something here paints in more than one colour, and splitting it "
                "is a change to the artwork, not to its structure"
            )
        parent, units, _ = plan
        if self.choice is None:  # pragma: no cover - the engine always answers
            return outcome.decline("no answer was given")

        groups = (
            self._runs(units)
            if self.choice == "runs"
            else [
                [unit for unit in units if unit.color == color]
                for color in _first_seen(unit.color for unit in units)
            ]
        )

        # Where the first piece of artwork sits, so the new layers land there.
        position = min(list(parent).index(unit.element) for unit in units)
        indent = _indent_of(parent.text)
        tail = units[-1].element.tail

        taken = {element.get("id") for element in doc.root.iter() if element.get("id")}
        for unit in units:
            parent.remove(unit.element)

        for offset, members in enumerate(groups):
            layer = ElementTree.Element(f"{{{SVG_NS}}}g")
            layer.set("id", _free_id(f"layer-{members[0].color.lstrip('#')}", taken))
            for unit in members:
                layer.append(unit.element)
            _nest(layer, indent)
            layer.tail = "\n" + indent
            parent.insert(position + offset, layer)
            outcome.add(
                f"layer {layer.get('id')}: {len(members)} shape(s) in {members[0].color}"
            )
        # The last new layer inherits whatever followed the artwork it replaced.
        if tail is not None:
            list(parent)[position + len(groups) - 1].tail = tail

        return outcome


#: What to do instead, for the forbidden elements that have a better answer.
_ALTERNATIVES = {
    "text": "Converting the text to paths keeps the design looking the same; that "
    "needs the font file, which svgemb does not read, so it is Inkscape's "
    "Path ▸ Object to Path.",
    "image": "A bitmap cannot be stitched as-is. Tracing it into shapes is what "
    "Phase B of this project is for.",
}


@register_fixer
class RemoveForbiddenArtwork(Fixer):
    """Delete forbidden elements that are actually part of the picture.

    The safe fixer already removed the ones drawing nothing. What is left is
    visible artwork, and the honest position is that deleting it is a decision,
    not a repair — so this asks, once, naming what would go and what the better
    answer would have been. For ``<text>`` the better answer is converting to
    paths, which needs the font and is therefore Inkscape's job; saying that is
    more useful than silently deleting someone's lettering.
    """

    rule_id = "element.forbidden"
    risk = Risk.DESTRUCTIVE
    summary = "delete forbidden elements even when they are visible"
    #: Deleting artwork changes the image by definition, so the budget cannot
    #: police it. What the engine still checks is that nothing else broke.
    visual_budget = 1.0

    def _visible(self, doc: SvgDocument) -> List[Node]:
        parents = parent_map(doc.root)
        references = referenced_ids(doc.root)
        return [
            node
            for node in doc.by_tag(*[str(tag) for tag in self.config["tags"]])
            if is_unrendered(node.element, parents) is False
            or (node.element.get("id") or "") in references
        ]

    def decision(self, doc: SvgDocument) -> Optional[Decision]:
        alive = self._visible(doc)
        if not alive:
            return None
        counts: Dict[str, int] = {}
        for node in alive:
            counts[node.tag] = counts.get(node.tag, 0) + 1
        listed = ", ".join(f"{count} <{tag}>" for tag, count in sorted(counts.items()))
        advice = " ".join(_ALTERNATIVES[tag] for tag in sorted(counts) if tag in _ALTERNATIVES)
        return Decision(
            rule_id=self.rule_id,
            question=f"Delete the {listed} this profile forbids?",
            context=f"{listed} still on the canvas. " + advice,
            options=[
                Option(
                    key="delete",
                    label=f"Delete {listed}",
                    detail="The design loses that content for good. Everything else is "
                    "untouched, and the run is rejected if anything else breaks.",
                    risk=Risk.DESTRUCTIVE,
                    recommended=True,
                )
            ],
        )

    def apply(self, doc: SvgDocument) -> FixOutcome:
        outcome = FixOutcome()
        alive = self._visible(doc)
        if not alive:  # pragma: no cover - decision() would have returned None
            return outcome.decline("nothing visible to remove")
        remove_elements(doc.root, [node.element for node in alive])
        for node in alive:
            outcome.add(f"deleted <{node.tag}> from the artwork", location=node.location)
        return outcome


@register_fixer
class CloseWideGaps(Fixer):
    """Close a contour whose ends are nowhere near each other.

    The safe fixer closes unstroked paths, where the fill already renders them
    shut and writing the ``Z`` changes nothing. The lossy one closes stroked
    gaps up to a millimetre, on the grounds that a hairline gap is a drawing
    slip. What is left is a gap wide enough that closing it draws a segment
    nobody drew — across a deliberately open shape, or where a piece of the
    outline is genuinely missing.

    So it asks, and tells you how wide. A 2 mm gap in a 100 mm outline is
    almost certainly a slip worth closing; a 40 mm one is a design.
    """

    rule_id = "path.closed"
    risk = Risk.DESTRUCTIVE
    summary = "close contours across a gap of any width"
    #: A straight segment across a gap is a thin sliver of the canvas, but the
    #: fill it completes may not be.
    visual_budget = 0.5

    def _open(self, doc: SvgDocument) -> List[Tuple[Node, float]]:
        """Every path with an unclosed subpath, and its widest gap in mm."""
        scale = doc.unit_scale
        found: List[Tuple[Node, float]] = []
        for node in doc.by_tag("path"):
            d = (node.element.get("d") or "").strip()
            if not d:
                continue
            gaps = [
                info.gap * scale * node.transform_scale
                for info in subpaths(d)
                if not info.closed
            ]
            if gaps:
                found.append((node, max(gaps)))
        return found

    def decision(self, doc: SvgDocument) -> Optional[Decision]:
        open_paths = self._open(doc)
        if not open_paths:
            return None
        widest = max(gap for _node, gap in open_paths)
        return Decision(
            rule_id=self.rule_id,
            question="Close the remaining contours, however wide the gap?",
            context=f"{len(open_paths)} path(s) are still open; the widest gap is "
            f"{format_mm(widest)}.",
            options=[
                Option(
                    key="close",
                    label="Draw a straight segment across each gap",
                    detail="Joins the last point back to the first. Where the shape "
                    "was meant to be open, this invents an edge and fills what was "
                    "behind it.",
                    risk=Risk.DESTRUCTIVE,
                    recommended=True,
                )
            ],
        )

    def apply(self, doc: SvgDocument) -> FixOutcome:
        outcome = FixOutcome()
        for node, widest in self._open(doc):
            d = (node.element.get("d") or "").strip()
            chunks = list(iter_subpaths(d))
            changed = False
            for info in subpaths(d):
                if info.closed or info.index >= len(chunks):
                    continue
                chunks[info.index] = close_subpath_text(chunks[info.index])
                changed = True
            if changed:
                node.element.set("d", rebuild(chunks))
                outcome.add(
                    f"closed a gap of up to {format_mm(widest)}", location=node.location
                )
        if not outcome.applied:  # pragma: no cover - decision() found them first
            return outcome.decline("every contour is already closed")
        return outcome


def _stops(element: ElementTree.Element) -> List[Tuple[float, str, float]]:
    """``(offset, colour, opacity)`` for each stop of a gradient."""
    found = []
    for stop in element.iter():
        if local_name(stop.tag) != "stop":
            continue
        style = parse_style(stop.get("style"))
        color = normalize_color(style.get("stop-color") or stop.get("stop-color"))
        if color is None:
            continue
        raw_offset = style.get("offset") or stop.get("offset") or "0"
        raw_alpha = style.get("stop-opacity") or stop.get("stop-opacity") or "1"
        try:
            offset = float(raw_offset.rstrip("%")) / (100 if "%" in raw_offset else 1)
            alpha = float(raw_alpha)
        except ValueError:  # pragma: no cover - malformed stops are rare
            offset, alpha = 0.0, 1.0
        found.append((offset, color, alpha))
    return sorted(found)


def _average_stop(stops: Sequence[Tuple[float, str, float]]) -> str:
    """The gradient's mean colour, weighted by how much of it each stop covers.

    A stop at 0.0 and one at 1.0 each own half the ramp; three evenly spaced
    stops own a quarter, a half and a quarter. Weighting by that span is what
    makes a gradient that is mostly dark average out dark, instead of being
    pulled to the midpoint by a sliver of highlight.
    """
    if len(stops) == 1:
        return stops[0][1]
    total = 0.0
    channels = [0.0, 0.0, 0.0]
    for index, (offset, color, _alpha) in enumerate(stops):
        low = stops[index - 1][0] if index else offset
        high = stops[index + 1][0] if index + 1 < len(stops) else offset
        weight = max((high - low) / 2.0, 1e-6)
        total += weight
        for channel, value in enumerate(hex_to_rgb(color)):
            channels[channel] += value * weight
    return rgb_to_hex(*(int(round(value / total)) for value in channels))


@register_fixer
class FlattenLiveGradients(Fixer):
    """Replace a gradient that something still paints with, with flat colour.

    The safe fixer already deleted the gradients nothing referenced. These are
    load-bearing: a shape is painted with them, so replacing one changes the
    picture — which is the whole point, because a needle cannot stitch a ramp.
    What it cannot do is decide *which* flat colour, so it offers the two
    answers that are defensible and shows the actual hex of each.

    Both are lossy rather than destructive: the shape keeps its position, its
    outline and its place in the stacking order, and only its paint changes.
    """

    rule_id = "color.no_gradients"
    risk = Risk.LOSSY
    summary = "replace referenced gradients with a flat colour"
    #: A gradient usually fills a large area, so repainting it legitimately
    #: moves a lot of pixels. What the budget still catches is repainting the
    #: whole canvas — the mistake a bad id lookup would make.
    visual_budget = 0.6

    def _live(self, doc: SvgDocument) -> Dict[str, ElementTree.Element]:
        """Referenced gradient definitions, by id."""
        tags = [str(tag) for tag in self.config["tags"]]
        references = referenced_ids(doc.root)
        return {
            node.element.get("id"): node.element
            for node in doc.by_tag(*tags)
            if node.element.get("id") in references and _stops(node.element)
        }

    def _replacements(self, doc: SvgDocument) -> Dict[str, Dict[str, str]]:
        """For each live gradient id, the flat colour each answer would use."""
        answers: Dict[str, Dict[str, str]] = {}
        for gradient_id, element in self._live(doc).items():
            stops = _stops(element)
            answers[gradient_id] = {
                "average": _average_stop(stops),
                "first": stops[0][1],
            }
        return answers

    def decision(self, doc: SvgDocument) -> Optional[Decision]:
        answers = self._replacements(doc)
        if not answers:
            return None
        listed = ", ".join(
            f"#{gradient_id} → {colors['average']} / {colors['first']}"
            for gradient_id, colors in sorted(answers.items())
        )
        return Decision(
            rule_id=self.rule_id,
            question="Which flat colour should replace each gradient?",
            context=f"{len(answers)} gradient(s) are still painted with: {listed}.",
            options=[
                Option(
                    key="average",
                    label="Its average colour, weighted across the ramp",
                    detail="Keeps the overall weight of the design closest to the "
                    "original — the usual choice when the gradient is shading.",
                    risk=Risk.LOSSY,
                    recommended=True,
                ),
                Option(
                    key="first",
                    label="Its first stop",
                    detail="Keeps one colour exactly as drawn. Better when the "
                    "gradient was a fade-out and the first stop is the real colour.",
                    risk=Risk.LOSSY,
                ),
            ],
        )

    def apply(self, doc: SvgDocument) -> FixOutcome:
        outcome = FixOutcome()
        answers = self._replacements(doc)
        if not answers:  # pragma: no cover - decision() would have returned None
            return outcome.decline("no referenced gradient has usable stops")
        key = self.choice or "average"

        painted = 0
        for node in doc.drawables:
            for prop in ("fill", "stroke"):
                gradient_id = node.paint_reference(prop)
                if gradient_id is None or gradient_id not in answers:
                    continue
                flat = answers[gradient_id][key]
                _set_paint(node.element, prop, flat)
                outcome.add(f"{prop} url(#{gradient_id}) -> {flat}", location=node.location)
                painted += 1
        if not painted:  # pragma: no cover - a live gradient has a painter
            return outcome.decline("nothing paints with those gradients after all")

        # With nothing referencing them any more, the definitions are dead
        # weight — and leaving them behind would keep the rule failing.
        for element in prune_unreferenced(doc.root, [str(tag) for tag in self.config["tags"]]):
            outcome.add(f"removed the now-unused <{local_name(element.tag)}>")
        return outcome


def _set_paint(element: ElementTree.Element, prop: str, value: str) -> None:
    """Write a colour where this element already keeps its paint."""
    style = parse_style(element.get("style"))
    if prop in style:
        style[prop] = value
        element.set("style", ";".join(f"{k}:{v}" for k, v in style.items()))
    else:
        element.set(prop, value)


def _first_seen(values: Iterable[str]) -> List[str]:
    """Unique values, in the order they first appear."""
    seen: List[str] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen


def _free_id(wanted: str, taken: Set[str]) -> str:
    """``wanted``, or the first numbered variant nothing else is using."""
    candidate, suffix = wanted, 1
    while candidate in taken:
        suffix += 1
        candidate = f"{wanted}-{suffix}"
    taken.add(candidate)
    return candidate
