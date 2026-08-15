"""Lossy fixes: repairs that change the picture, on purpose.

A safe fix has an easy test — nothing moved. These deliberately move something,
so each one declares a *budget*: how much of the image it may change before the
engine calls it a bug. And each one reports exactly what it did, because the
whole point is that the user can disagree.

None of these run unless asked for (``allow=[Risk.SAFE, Risk.LOSSY]``).
"""

from __future__ import annotations

from typing import Dict, List, Tuple
from xml.etree import ElementTree

from ..colors import (
    blend_over,
    nearest_color,
    normalize_color,
    reduce_palette,
)
from ..document import SvgDocument, iter_subpaths, parse_style
from ._path import close_subpath_text, rebuild, subpaths
from .base import FixOutcome, Fixer, Risk, register_fixer

#: What a page composites against when nothing sits behind a shape.
PAGE_BACKDROP = "#ffffff"


def _style_text(style: Dict[str, str]) -> str:
    return ";".join(f"{name}:{value}" for name, value in style.items())


def _recolor(root: ElementTree.Element, mapping: Dict[str, str]) -> Dict[Tuple[str, str], int]:
    """Rewrite every fill/stroke according to ``mapping``. Returns counts."""
    applied: Dict[Tuple[str, str], int] = {}
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue

        for attribute in ("fill", "stroke"):
            value = element.get(attribute)
            current = normalize_color(value) if value else None
            replacement = mapping.get(current) if current else None
            if replacement and replacement != current:
                element.set(attribute, replacement)
                applied[(current, replacement)] = applied.get((current, replacement), 0) + 1

        style = parse_style(element.get("style"))
        changed = False
        for attribute in ("fill", "stroke"):
            current = normalize_color(style.get(attribute)) if style.get(attribute) else None
            replacement = mapping.get(current) if current else None
            if replacement and replacement != current:
                style[attribute] = replacement
                changed = True
                applied[(current, replacement)] = applied.get((current, replacement), 0) + 1
        if changed:
            element.set("style", _style_text(style))
    return applied


def _report_mapping(outcome: FixOutcome, applied: Dict[Tuple[str, str], int]) -> None:
    for (old, new), count in sorted(applied.items()):
        outcome.add(f"{old} -> {new} ({count} place(s))")


@register_fixer
class QuantiseColours(Fixer):
    """Merge colours until the design is inside the shop's limit.

    Merging happens in CIE Lab, so the colours that disappear are the ones a
    person would call near-duplicates — not the ones that happen to be close in
    RGB. Survivors are always colours already in the design: averaging a cluster
    would invent a shade no thread matches.

    The budget is wide open, deliberately. Repainting is the entire point, so a
    pixel count says nothing useful here; the guards that matter are that no
    other rule breaks and that every substitution is listed for review.
    """

    rule_id = "color.max_count"
    risk = Risk.LOSSY
    summary = "merge near-duplicate colours down to the allowed count"
    visual_budget = 1.0

    def apply(self, doc: SvgDocument) -> FixOutcome:
        outcome = FixOutcome()
        limit = int(self.config["max_colors"])
        usage = doc.colors(include_stroke=bool(self.config["include_stroke"]))
        if len(usage) <= limit:
            return outcome.decline("the design is already inside the colour limit")

        weights = {color: float(len(nodes)) for color, nodes in usage.items()}
        mapping = reduce_palette(weights, limit)
        applied = _recolor(doc.root, mapping)
        if not applied:
            return outcome.decline("no colour could be rewritten")

        outcome.add(f"reduced {len(usage)} colours to {len(set(mapping.values()))}")
        _report_mapping(outcome, applied)
        return outcome


@register_fixer
class RemapToPalette(Fixer):
    """Snap every colour to the nearest one the shop actually stocks."""

    rule_id = "color.allowed_palette"
    risk = Risk.LOSSY
    summary = "snap colours to the nearest palette entry"
    visual_budget = 1.0

    def apply(self, doc: SvgDocument) -> FixOutcome:
        outcome = FixOutcome()
        allowed = [str(color) for color in self.config["colors"]]
        if not allowed:
            return outcome.decline("the profile lists no palette colours")

        mapping = {}
        for color in doc.colors():
            if color in allowed:
                continue
            nearest = nearest_color(color, allowed)
            if nearest is not None:
                mapping[color] = nearest
        if not mapping:
            return outcome.decline("every colour is already in the palette")

        applied = _recolor(doc.root, mapping)
        outcome.add(f"snapped {len(mapping)} colour(s) to the {self.config['palette_name']}")
        _report_mapping(outcome, applied)
        return outcome


@register_fixer
class FlattenTransparency(Fixer):
    """Replace semi-transparent paint with the solid colour it looks like.

    A 50% red on a white page is a pink, and pink is what gets stitched. The
    fixer composites each colour against the page and drops the opacity.

    The approximation is deliberate and bounded: it composites against the
    *page*, not against whatever shape happens to sit underneath. Where shapes
    overlap, the flattened result differs from the original — which is what the
    budget is for. Group opacity is declined outright, because flattening it
    correctly means pushing it into every descendant.
    """

    rule_id = "color.no_transparency"
    risk = Risk.LOSSY
    summary = "composite semi-transparent paint onto the page"
    visual_budget = 0.10

    def _alpha(self, value: str) -> float:
        text = value.strip()
        try:
            return float(text[:-1]) / 100 if text.endswith("%") else float(text)
        except ValueError:
            return 1.0

    def apply(self, doc: SvgDocument) -> FixOutcome:
        outcome = FixOutcome()
        threshold = float(self.config["min_opacity"])
        declined: List[str] = []

        for node in doc.nodes:
            element = node.element
            own = dict(element.attrib)
            own_style = parse_style(element.get("style"))
            properties = {
                name: own_style.get(name, own.get(name))
                for name in ("opacity", "fill-opacity", "stroke-opacity")
                if own_style.get(name, own.get(name)) is not None
            }
            faded = {
                name: self._alpha(value)
                for name, value in properties.items()
                if self._alpha(value) < threshold - 1e-9
            }
            if not faded:
                continue

            if len(element) and "opacity" in faded:
                declined.append(node.label)
                continue

            group = faded.get("opacity", 1.0)
            for paint, key in (("fill", "fill-opacity"), ("stroke", "stroke-opacity")):
                alpha = group * faded.get(key, 1.0)
                if alpha >= 1.0 - 1e-9:
                    continue
                colour = node.paint(paint)
                if colour is None:
                    continue
                flattened = blend_over(colour, alpha, PAGE_BACKDROP)
                element.set(paint, flattened)
                outcome.add(
                    f"{colour} at {alpha:.0%} -> {flattened}", location=node.location
                )

            for name in faded:
                element.attrib.pop(name, None)
                if name in own_style:
                    del own_style[name]
                    if own_style:
                        element.set("style", _style_text(own_style))
                    else:
                        element.attrib.pop("style", None)

        if declined and not outcome.applied:
            return outcome.decline(
                f"{len(declined)} group(s) use opacity; flattening that means pushing it "
                "into every shape inside, which is a manual decision"
            )
        if not outcome.applied:
            return outcome.decline("nothing to flatten")
        if declined:
            outcome.add(f"left {len(declined)} group opacity setting(s) alone")
        return outcome


@register_fixer
class CloseSmallGaps(Fixer):
    """Close stroked contours whose ends nearly meet.

    Drawing the closing segment is a visible change, so it only happens when the
    gap is small enough to be a slip of the pen. A wide gap means the shape was
    never meant to be closed, and inventing an edge across it would be making
    up artwork — that gets reported instead.
    """

    rule_id = "path.closed"
    risk = Risk.LOSSY
    summary = "close stroked contours whose ends nearly meet"
    visual_budget = 0.02
    #: Widest gap worth closing. A fixer-level setting for now; exposing it
    #: through the profile belongs with the CLI work in A6.
    max_gap_mm = 1.0

    def apply(self, doc: SvgDocument) -> FixOutcome:
        outcome = FixOutcome()
        limit_mm = float(self.max_gap_mm)
        scale = doc.unit_scale
        too_wide = 0

        for node in doc.by_tag("path"):
            if node.paint("stroke") is None and node.paint_reference("stroke") is None:
                continue  # unstroked paths are the safe fixer's business
            d = (node.element.get("d") or "").strip()
            if not d:
                continue

            chunks = list(iter_subpaths(d))
            ends = subpaths(d)
            changed = False
            for info in ends:
                if info.closed or info.index >= len(chunks):
                    continue
                gap_mm = info.gap * scale * node.transform_scale
                if gap_mm <= limit_mm:
                    chunks[info.index] = close_subpath_text(chunks[info.index])
                    changed = True
                    outcome.add(
                        f"closed a {gap_mm:.2f} mm gap", location=node.location
                    )
                else:
                    too_wide += 1

            if changed:
                node.element.set("d", rebuild(chunks))

        if not outcome.applied:
            if too_wide:
                return outcome.decline(
                    f"{too_wide} contour(s) have gaps wider than {limit_mm:g} mm; closing "
                    "them would invent artwork, so that is a manual decision"
                )
            return outcome.decline("no stroked contour needs closing")
        if too_wide:
            outcome.add(f"left {too_wide} wide gap(s) alone")
        return outcome
