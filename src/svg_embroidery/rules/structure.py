"""Rules about document structure: forbidden elements, layers, raster data."""

from __future__ import annotations

from typing import Dict, Iterable, List

from ..document import SVG_NS, XLINK_NS, SvgDocument
from ..findings import Finding, Severity
from .base import Rule, register

#: Extra guidance per element type, shown with the failure.
ELEMENT_HINTS: Dict[str, str] = {
    "text": "Convert text to paths (Inkscape: Path ▸ Object to Path).",
    "image": "Embroidery files must be vector only — remove embedded bitmaps.",
    "filter": "Filters (blur, shadow) cannot be stitched; flatten them away.",
    "use": "Expand <use> clones into real geometry before export.",
    "clipPath": "Apply clip paths to the geometry instead of referencing them.",
    "mask": "Masks are not supported; bake them into the shapes.",
    "switch": "Remove conditional <switch> containers.",
    "foreignObject": "Remove foreign objects; they are not vector geometry.",
}


@register
class ForbiddenElementsRule(Rule):
    id = "element.forbidden"
    summary = "Certain SVG elements must not appear in the file"
    params = {"tags": ["text", "image", "filter", "foreignObject"]}

    def check(self, doc: SvgDocument) -> Iterable[Finding]:
        forbidden = [str(tag) for tag in self.config["tags"]]
        findings: List[Finding] = []
        for tag in forbidden:
            nodes = doc.by_tag(tag)
            if nodes:
                findings.append(
                    self.fail(
                        f"{len(nodes)} <{tag}> element(s) found — not allowed.",
                        hint=ELEMENT_HINTS.get(tag),
                        location=nodes[0].location,
                        tag=tag,
                        count=len(nodes),
                    )
                )
        if findings:
            return findings
        return [self.ok(f"None of the forbidden elements present ({', '.join(forbidden)}).")]


@register
class NoRasterDataRule(Rule):
    id = "document.no_raster"
    summary = "No embedded bitmap data anywhere in the file"

    def check(self, doc: SvgDocument) -> Iterable[Finding]:
        findings: List[Finding] = []
        for node in doc.nodes:
            for attr in ("href", f"{{{XLINK_NS}}}href", f"{{{SVG_NS}}}href"):
                value = node.element.get(attr)
                if value and value.strip().lower().startswith("data:image"):
                    findings.append(
                        self.fail(
                            f"{node.label} embeds raster image data.",
                            hint="Vectorise the bitmap (trace it) and delete the embedded image.",
                            location=node.location,
                        )
                    )
                    break
        if findings:
            return findings
        return [self.ok("No embedded raster data.")]


@register
class ColorLayersRule(Rule):
    id = "structure.color_layers"
    summary = "Each colour must sit on its own layer / top-level group"
    params = {"require_layers": True, "max_colors_per_layer": 1}
    default_severity = Severity.ERROR

    def check(self, doc: SvgDocument) -> Iterable[Finding]:
        used_colors = doc.colors()
        layers = doc.layers()

        if not layers:
            if len(used_colors) <= 1:
                return [self.ok("Single colour design — no layer separation needed.")]
            if self.config["require_layers"]:
                return [
                    self.fail(
                        f"{len(used_colors)} colours but no layers/top-level groups found.",
                        hint="Put every colour on its own layer so the shop can separate them.",
                        colors=sorted(used_colors),
                    )
                ]
            return [self.warn("No layers found; colour separation cannot be verified.")]

        limit = int(self.config["max_colors_per_layer"])
        findings: List[Finding] = []
        for layer in layers:
            colors = set()
            for child in doc.descendants(layer):
                if not child.is_drawable or not child.is_visible:
                    continue
                for prop in ("fill", "stroke"):
                    color = child.paint(prop)
                    if color is not None:
                        colors.add(color)
            if len(colors) > limit:
                name = layer.element_id or layer.location
                findings.append(
                    self.fail(
                        f"Layer '{name}' mixes {len(colors)} colours "
                        f"(max {limit}): {', '.join(sorted(colors))}.",
                        hint="Split the layer so each colour is separate.",
                        location=layer.location,
                        colors=sorted(colors),
                    )
                )
        if findings:
            return findings
        return [self.ok(f"{len(layers)} layer(s), each within {limit} colour(s).")]
