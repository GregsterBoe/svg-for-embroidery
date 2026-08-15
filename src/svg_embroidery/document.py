"""The parsed SVG document model that rules run against.

Rules never touch the raw XML tree directly: they get an :class:`SvgDocument`
with resolved geometry (millimetres), resolved presentation attributes
(inheritance + ``style`` applied) and a flat list of drawable nodes.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
from xml.etree import ElementTree

from .colors import is_paint_reference, normalize_color
from .units import MM_PER_INCH, USER_UNITS_PER_INCH, parse_length

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"

#: Tags that actually paint something on the canvas.
DRAWABLE_TAGS = frozenset(
    {"path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "text", "image", "use"}
)

#: Presentation attributes we resolve down the tree.
INHERITED_PROPERTIES = (
    "fill",
    "stroke",
    "stroke-width",
    "stroke-linecap",
    "stroke-linejoin",
    "fill-rule",
    "color",
    "visibility",
)

DEFAULT_STYLE = {"fill": "black", "stroke": "none", "stroke-width": "1"}

_TRANSFORM_RE = re.compile(r"(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^)]*)\)")
_NUMBER_RE = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")


class SvgParseError(Exception):
    """Raised when a file cannot be parsed as SVG."""


def local_name(tag: str) -> str:
    """``{http://...}path`` -> ``path``."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) and "}" in tag else tag


@dataclass
class Node:
    """A single element with its resolved rendering context."""

    element: ElementTree.Element
    tag: str
    location: str
    style: Dict[str, str]
    transform_scale: float
    depth: int
    parent: Optional["Node"] = None
    #: True when this element or an ancestor is display:none.
    hidden: bool = False
    #: Position in the document-order node list.
    index: int = 0

    @property
    def element_id(self) -> Optional[str]:
        return self.element.get("id")

    @property
    def label(self) -> str:
        ident = self.element_id
        return f"<{self.tag} id=\"{ident}\">" if ident else f"<{self.tag}>"

    @property
    def is_drawable(self) -> bool:
        return self.tag in DRAWABLE_TAGS

    @property
    def is_visible(self) -> bool:
        if self.hidden:
            return False
        # visibility is inherited but a child may override it back to visible.
        return self.style.get("visibility", "").strip().lower() not in ("hidden", "collapse")

    def paint(self, prop: str) -> Optional[str]:
        """Normalised ``fill`` / ``stroke`` colour, or ``None`` when unpainted."""
        return normalize_color(self.style.get(prop))

    def paint_reference(self, prop: str) -> Optional[str]:
        value = self.style.get(prop)
        return is_paint_reference(value) if value else None

    def stroke_width_mm(self, user_unit_mm: float) -> Optional[float]:
        """Effective stroke width in mm, including ancestor transforms."""
        if self.paint("stroke") is None and self.paint_reference("stroke") is None:
            return None
        length = parse_length(self.style.get("stroke-width", "1"))
        if length is None or length.is_relative:
            return None
        base = length.to_mm(user_unit_mm)
        if base is None:
            return None
        return base * self.transform_scale


def _parse_numbers(raw: str) -> List[float]:
    return [float(m.group()) for m in _NUMBER_RE.finditer(raw)]


def _multiply(a: Sequence[float], b: Sequence[float]) -> Tuple[float, ...]:
    """Multiply two SVG matrices given as ``(a, b, c, d, e, f)``."""
    a0, a1, a2, a3, a4, a5 = a
    b0, b1, b2, b3, b4, b5 = b
    return (
        a0 * b0 + a2 * b1,
        a1 * b0 + a3 * b1,
        a0 * b2 + a2 * b3,
        a1 * b2 + a3 * b3,
        a0 * b4 + a2 * b5 + a4,
        a1 * b4 + a3 * b5 + a5,
    )


def transform_matrix(value: Optional[str]) -> Tuple[float, ...]:
    """Parse a ``transform`` attribute into a single matrix."""
    matrix: Tuple[float, ...] = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    if not value:
        return matrix
    for op, raw_args in _TRANSFORM_RE.findall(value):
        args = _parse_numbers(raw_args)
        if op == "matrix" and len(args) >= 6:
            step = tuple(args[:6])
        elif op == "translate" and args:
            step = (1.0, 0.0, 0.0, 1.0, args[0], args[1] if len(args) > 1 else 0.0)
        elif op == "scale" and args:
            sx = args[0]
            sy = args[1] if len(args) > 1 else sx
            step = (sx, 0.0, 0.0, sy, 0.0, 0.0)
        elif op == "rotate" and args:
            angle = math.radians(args[0])
            cos, sin = math.cos(angle), math.sin(angle)
            step = (cos, sin, -sin, cos, 0.0, 0.0)
            if len(args) >= 3:  # rotate(a, cx, cy)
                cx, cy = args[1], args[2]
                step = _multiply(_multiply((1, 0, 0, 1, cx, cy), step), (1, 0, 0, 1, -cx, -cy))
        elif op == "skewX" and args:
            step = (1.0, 0.0, math.tan(math.radians(args[0])), 1.0, 0.0, 0.0)
        elif op == "skewY" and args:
            step = (1.0, math.tan(math.radians(args[0])), 0.0, 1.0, 0.0, 0.0)
        else:
            continue
        matrix = _multiply(matrix, step)
    return matrix


def matrix_scale(matrix: Sequence[float]) -> float:
    """Uniform scale factor of a matrix (geometric mean of the axes)."""
    determinant = abs(matrix[0] * matrix[3] - matrix[1] * matrix[2])
    return math.sqrt(determinant) if determinant > 0 else 1.0


def parse_style(value: Optional[str]) -> Dict[str, str]:
    """``"fill:#f00;stroke:none"`` -> ``{"fill": "#f00", "stroke": "none"}``."""
    result: Dict[str, str] = {}
    if not value:
        return result
    for declaration in value.split(";"):
        if ":" not in declaration:
            continue
        prop, _, val = declaration.partition(":")
        prop = prop.strip().lower()
        val = val.strip()
        if prop and val:
            result[prop] = val
    return result


def element_style(element: ElementTree.Element) -> Dict[str, str]:
    """Own presentation attributes of an element, ``style`` taking precedence."""
    own: Dict[str, str] = {}
    for prop in INHERITED_PROPERTIES + ("display", "opacity", "fill-opacity", "stroke-opacity"):
        value = element.get(prop)
        if value is not None:
            own[prop] = value
    own.update(parse_style(element.get("style")))
    return own


@dataclass
class SvgDocument:
    """A parsed SVG file with everything the rules need."""

    path: Optional[Path]
    root: ElementTree.Element
    source: str
    nodes: List[Node] = field(default_factory=list)
    width_mm: Optional[float] = None
    height_mm: Optional[float] = None
    #: Size of one user unit in mm (viewBox aware); ``None`` if undeterminable.
    user_unit_mm: Optional[float] = None
    #: How the physical size was derived: ``attributes``, ``viewbox`` or ``unknown``.
    size_source: str = "unknown"

    # -- lookups -----------------------------------------------------------
    def by_tag(self, *tags: str) -> List[Node]:
        wanted = set(tags)
        return [node for node in self.nodes if node.tag in wanted]

    @property
    def drawables(self) -> List[Node]:
        return [node for node in self.nodes if node.is_drawable and node.is_visible]

    def layers(self) -> List[Node]:
        """Top-level groups, i.e. what design tools call layers."""
        inkscape_mode = f"{{{INKSCAPE_NS}}}groupmode"
        explicit = [
            node
            for node in self.nodes
            if node.tag == "g" and node.element.get(inkscape_mode) == "layer"
        ]
        if explicit:
            return explicit
        return [node for node in self.nodes if node.tag == "g" and node.depth == 1]

    def descendants(self, node: Node) -> Iterator[Node]:
        """All nodes below ``node``.

        ``nodes`` is in DFS pre-order, so a subtree is the contiguous run of
        deeper nodes following its root.
        """
        for candidate in self.nodes[node.index + 1 :]:
            if candidate.depth <= node.depth:
                return
            yield candidate

    def colors(self, include_stroke: bool = True) -> Dict[str, List[Node]]:
        """Map of normalised colour -> nodes using it (visible drawables only)."""
        found: Dict[str, List[Node]] = {}
        props = ("fill", "stroke") if include_stroke else ("fill",)
        for node in self.drawables:
            for prop in props:
                color = node.paint(prop)
                if color is not None:
                    found.setdefault(color, []).append(node)
        return found

    @property
    def unit_scale(self) -> float:
        """User unit size in mm, falling back to the CSS default (1/96 in)."""
        return self.user_unit_mm or (MM_PER_INCH / USER_UNITS_PER_INCH)


def _viewbox(root: ElementTree.Element) -> Optional[Tuple[float, float, float, float]]:
    raw = root.get("viewBox")
    if not raw:
        return None
    parts = _parse_numbers(raw)
    if len(parts) != 4:
        return None
    return (parts[0], parts[1], parts[2], parts[3])


def _resolve_size(root: ElementTree.Element) -> Tuple[Optional[float], Optional[float], Optional[float], str]:
    """Return ``(width_mm, height_mm, user_unit_mm, source)``."""
    width = parse_length(root.get("width"))
    height = parse_length(root.get("height"))
    box = _viewbox(root)

    has_absolute = (
        width is not None
        and height is not None
        and not width.is_relative
        and not height.is_relative
    )

    if has_absolute:
        width_mm = width.to_mm()
        height_mm = height.to_mm()
        user_unit_mm = None
        if box and box[2] > 0 and width_mm is not None:
            user_unit_mm = width_mm / box[2]
        elif width.unit in ("", "px"):
            user_unit_mm = MM_PER_INCH / USER_UNITS_PER_INCH
        source = "attributes"
        return width_mm, height_mm, user_unit_mm, source

    if box and box[2] > 0 and box[3] > 0:
        # No usable width/height: user units are px by convention.
        user_unit_mm = MM_PER_INCH / USER_UNITS_PER_INCH
        return box[2] * user_unit_mm, box[3] * user_unit_mm, user_unit_mm, "viewbox"

    return None, None, None, "unknown"


def _walk(
    element: ElementTree.Element,
    inherited_style: Dict[str, str],
    inherited_scale: float,
    location: str,
    depth: int,
    parent: Optional[Node],
    hidden: bool,
    out: List[Node],
) -> None:
    counters: Dict[str, int] = {}
    for child in element:
        if not isinstance(child.tag, str):  # comments, processing instructions
            continue
        tag = local_name(child.tag)
        counters[tag] = counters.get(tag, 0) + 1
        child_location = f"{location}/{tag}[{counters[tag]}]"
        own = element_style(child)
        style = dict(inherited_style)
        style.update(own)
        scale = inherited_scale * matrix_scale(transform_matrix(child.get("transform")))
        # display is not inherited, but a hidden ancestor hides the subtree.
        child_hidden = hidden or own.get("display", "").strip().lower() == "none"
        node = Node(
            element=child,
            tag=tag,
            location=child_location,
            style=style,
            transform_scale=scale,
            depth=depth,
            parent=parent,
            hidden=child_hidden,
            index=len(out),
        )
        out.append(node)
        _walk(child, style, scale, child_location, depth + 1, node, child_hidden, out)


def parse_svg(source: str, path: Optional[Path] = None) -> SvgDocument:
    """Parse SVG markup into an :class:`SvgDocument`."""
    try:
        root = ElementTree.fromstring(source.encode("utf-8") if isinstance(source, str) else source)
    except ElementTree.ParseError as exc:
        raise SvgParseError(str(exc)) from exc

    if local_name(root.tag) != "svg":
        raise SvgParseError(f"root element is <{local_name(root.tag)}>, expected <svg>")

    width_mm, height_mm, user_unit_mm, size_source = _resolve_size(root)
    root_style = dict(DEFAULT_STYLE)
    root_style.update(element_style(root))
    root_scale = matrix_scale(transform_matrix(root.get("transform")))

    nodes: List[Node] = []
    root_hidden = root_style.get("display", "").strip().lower() == "none"
    _walk(root, root_style, root_scale, "/svg", 1, None, root_hidden, nodes)

    return SvgDocument(
        path=path,
        root=root,
        source=source,
        nodes=nodes,
        width_mm=width_mm,
        height_mm=height_mm,
        user_unit_mm=user_unit_mm,
        size_source=size_source,
    )


def load_svg(path) -> SvgDocument:
    """Read and parse an SVG file from disk."""
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SvgParseError(f"cannot read file: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise SvgParseError(f"file is not valid UTF-8 text: {exc}") from exc
    return parse_svg(text, path=file_path)


def iter_subpaths(d_attribute: str) -> Iterable[str]:
    """Split a path ``d`` attribute into its subpaths (each starting at M/m)."""
    if not d_attribute:
        return []
    chunks: List[str] = []
    current: List[str] = []
    for token in re.findall(r"[MmZzLlHhVvCcSsQqTtAa]|[^MmZzLlHhVvCcSsQqTtAa]+", d_attribute):
        if token in ("M", "m") and current:
            chunks.append("".join(current))
            current = [token]
        else:
            current.append(token)
    if current:
        chunks.append("".join(current))
    return [chunk.strip() for chunk in chunks if chunk.strip()]
