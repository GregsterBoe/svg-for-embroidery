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


XML_NS = "http://www.w3.org/XML/1998/namespace"

_XMLNS_RE = re.compile(
    r"""xmlns(?::([A-Za-z_][\w.\-]*))?\s*=\s*(?:"([^"]*)"|'([^']*)')"""
)
_ENCODING_RE = re.compile(r"""encoding\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_ROOT_NAME_RE = re.compile(r"<([^\s/>]+)")


@dataclass
class SourceFormat:
    """Everything about the original file that the XML tree does not carry.

    The parser throws this away — the XML declaration, the DOCTYPE, comments
    outside the root, line endings, the byte order mark and namespace
    declarations that nothing uses. :mod:`svg_embroidery.writer` puts it back.
    """

    #: Text before the root element, verbatim (XML declaration, DOCTYPE, comments).
    prologue: str = ""
    #: Text after the root element's closing tag, verbatim.
    epilogue: str = ""
    #: Namespace prefix ("" for the default namespace) -> URI, in source order.
    nsmap: Dict[str, str] = field(default_factory=dict)
    #: Line ending used by the file.
    newline: str = "\n"
    #: File started with a UTF-8 byte order mark.
    bom: bool = False
    #: Encoding declared in the XML declaration.
    encoding: str = "utf-8"
    #: The file declares entities in an internal DTD subset (see writer.py).
    has_internal_subset: bool = False


def _scan_prologue(source: str) -> Tuple[str, int, bool]:
    """Return ``(prologue, root_offset, has_internal_subset)``.

    Walks the declarations before the root element by hand, because the parser
    reports none of them.
    """
    index = 0
    has_internal_subset = False
    while True:
        start = source.find("<", index)
        if start < 0:
            raise SvgParseError("no root element found")
        if source.startswith("<?", start):
            end = source.find("?>", start)
            if end < 0:
                raise SvgParseError("unterminated processing instruction")
            index = end + 2
        elif source.startswith("<!--", start):
            end = source.find("-->", start)
            if end < 0:
                raise SvgParseError("unterminated comment")
            index = end + 3
        elif source[start : start + 9].upper() == "<!DOCTYPE":
            cursor = start
            in_subset = False
            while cursor < len(source):
                char = source[cursor]
                if char == "[":
                    in_subset = True
                    has_internal_subset = True
                elif char == "]":
                    in_subset = False
                elif char == ">" and not in_subset:
                    break
                cursor += 1
            if cursor >= len(source):
                raise SvgParseError("unterminated DOCTYPE")
            index = cursor + 1
        else:
            return source[:start], start, has_internal_subset


def _scan_epilogue(source: str, root_offset: int) -> str:
    """Text following the root element."""
    match = _ROOT_NAME_RE.match(source, root_offset)
    if not match:
        return ""
    start = source.rfind(f"</{match.group(1)}")
    if start >= 0:
        end = source.find(">", start)
        return source[end + 1 :] if end >= 0 else ""

    # A self-closing root has no end tag: find the end of its start tag.
    cursor, quote = root_offset + 1, ""
    while cursor < len(source):
        char = source[cursor]
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == ">":
            return source[cursor + 1 :]
        cursor += 1
    return ""


def _scan_namespaces(source: str) -> Dict[str, str]:
    """Every namespace declaration in the file, including unused ones.

    The parser drops declarations nothing references, and Inkscape/Illustrator
    files are full of them.
    """
    nsmap: Dict[str, str] = {}
    for match in _XMLNS_RE.finditer(source):
        prefix = match.group(1) or ""
        uri = match.group(2) if match.group(2) is not None else match.group(3)
        existing = nsmap.get(prefix)
        if existing is not None and existing != uri:
            raise SvgParseError(
                f"namespace prefix '{prefix or '(default)'}' is declared twice with "
                f"different URIs ('{existing}' and '{uri}'); refusing to guess"
            )
        nsmap[prefix] = uri
    return nsmap


@dataclass
class StartTag:
    """Where an element's start tag sits in the source, and how it was written."""

    start: int
    end: int
    self_closing: bool


def scan_start_tags(source: str) -> List[StartTag]:
    """Find every element start tag in document order.

    The XML parser records text and tail whitespace, so the only formatting it
    loses is *inside* start tags — the line breaks Inkscape and Illustrator put
    between attributes, the quote style, and whether an empty element was
    written ``<rect/>`` or ``<rect></rect>``. Keeping the source spans lets the
    writer reproduce untouched elements exactly.
    """
    tags: List[StartTag] = []
    index = 0
    length = len(source)
    while index < length:
        start = source.find("<", index)
        if start < 0:
            break
        if source.startswith("<!--", start):
            end = source.find("-->", start)
            index = length if end < 0 else end + 3
        elif source.startswith("<![CDATA[", start):
            end = source.find("]]>", start)
            index = length if end < 0 else end + 3
        elif source.startswith("<?", start):
            end = source.find("?>", start)
            index = length if end < 0 else end + 2
        elif source.startswith("</", start):
            end = source.find(">", start)
            index = length if end < 0 else end + 1
        elif source.startswith("<!", start):
            cursor, in_subset = start, False
            while cursor < length:
                char = source[cursor]
                if char == "[":
                    in_subset = True
                elif char == "]":
                    in_subset = False
                elif char == ">" and not in_subset:
                    break
                cursor += 1
            index = cursor + 1
        else:
            cursor = start + 1
            quote = ""
            while cursor < length:
                char = source[cursor]
                if quote:
                    if char == quote:
                        quote = ""
                elif char in "\"'":
                    quote = char
                elif char == ">":
                    break
                cursor += 1
            if cursor >= length:
                break
            self_closing = source[start:cursor].rstrip().endswith("/")
            tags.append(StartTag(start=start, end=cursor + 1, self_closing=self_closing))
            index = cursor + 1
    return tags


def _elements_in_order(element: ElementTree.Element) -> Iterator[ElementTree.Element]:
    """Pre-order traversal of real elements, skipping comments and PIs."""
    if isinstance(element.tag, str):
        yield element
    for child in element:
        if isinstance(child.tag, str):
            yield from _elements_in_order(child)


@dataclass
class VerbatimIndex:
    """Original start-tag text per element, and the attributes it encoded.

    An element may be reproduced verbatim only while its attributes still match
    what the source said — once a fixer edits them, the tag is rewritten.
    """

    source: str
    spans: Dict[int, StartTag] = field(default_factory=dict)
    attributes: Dict[int, Dict[str, str]] = field(default_factory=dict)

    def unchanged(self, element: ElementTree.Element) -> bool:
        key = id(element)
        return key in self.spans and self.attributes.get(key) == element.attrib

    def removed_attributes(self, element: ElementTree.Element) -> Optional[List[str]]:
        """Attribute names dropped since parsing, if that is the *only* change.

        Deleting an attribute is the common shape of a safe fix (stripping
        editor metadata, dropping a filter reference), and it can be done to the
        original tag text with scissors — keeping the author's line breaks
        instead of reflowing a fourteen-line ``<svg>`` onto one line. Returns
        ``None`` when anything was added or modified, since that needs a rebuild.
        """
        original = self.attributes.get(id(element))
        if original is None or id(element) not in self.spans:
            return None
        current = element.attrib
        for key, value in current.items():
            if key not in original or original[key] != value:
                return None
        dropped = [key for key in original if key not in current]
        return dropped or None

    def text_for(self, element: ElementTree.Element) -> Tuple[str, bool]:
        span = self.spans[id(element)]
        return self.source[span.start : span.end], span.self_closing


def build_verbatim_index(root: ElementTree.Element, source: str) -> Optional[VerbatimIndex]:
    """Pair parsed elements with their source spans, or give up safely.

    Both sequences are in document order, so they line up one to one. If they
    ever don't, return ``None`` and let the writer fall back to reformatting —
    guessing here would corrupt files.
    """
    tags = scan_start_tags(source)
    elements = list(_elements_in_order(root))
    if len(tags) != len(elements):
        return None

    index = VerbatimIndex(source=source)
    for element, tag in zip(elements, tags):
        index.spans[id(element)] = tag
        index.attributes[id(element)] = dict(element.attrib)
    return index


def analyze_source(source: str) -> Tuple[SourceFormat, str]:
    """Split a raw document into its :class:`SourceFormat` and normalised text.

    The returned text has no BOM and uses ``\\n``, matching what the parser
    produces, so comparisons are not confused by line endings.
    """
    bom = source.startswith("﻿")
    if bom:
        source = source[1:]

    newline = "\r\n" if "\r\n" in source else "\n"
    normalised = source.replace("\r\n", "\n").replace("\r", "\n")

    prologue, root_offset, has_internal_subset = _scan_prologue(normalised)
    encoding_match = _ENCODING_RE.search(prologue)
    encoding = "utf-8"
    if encoding_match:
        encoding = (encoding_match.group(1) or encoding_match.group(2) or "utf-8").lower()

    fmt = SourceFormat(
        prologue=prologue,
        epilogue=_scan_epilogue(normalised, root_offset),
        nsmap=_scan_namespaces(normalised),
        newline=newline,
        bom=bom,
        encoding=encoding,
        has_internal_subset=has_internal_subset,
    )
    return fmt, normalised


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
    #: Formatting details the XML tree cannot carry; used when writing back.
    format: SourceFormat = field(default_factory=SourceFormat)
    #: Original start-tag text per element; ``None`` when it could not be built.
    verbatim: Optional["VerbatimIndex"] = None

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
    if isinstance(source, bytes):
        source = source.decode("utf-8")
    fmt, source = analyze_source(source)

    # insert_comments/insert_pis keep comments and processing instructions in
    # the tree, so writing the document back does not delete them.
    parser = ElementTree.XMLParser(
        target=ElementTree.TreeBuilder(insert_comments=True, insert_pis=True)
    )
    try:
        parser.feed(source)
        root = parser.close()
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
        verbatim=build_verbatim_index(root, source),
        source=source,
        nodes=nodes,
        width_mm=width_mm,
        height_mm=height_mm,
        user_unit_mm=user_unit_mm,
        size_source=size_source,
        format=fmt,
    )


def load_svg(path) -> SvgDocument:
    """Read and parse an SVG file from disk.

    Reads bytes rather than text: ``read_text`` silently rewrites CRLF to LF,
    which would make the writer change every line of a Windows-authored file.
    """
    file_path = Path(path)
    try:
        raw = file_path.read_bytes()
    except OSError as exc:
        raise SvgParseError(f"cannot read file: {exc}") from exc

    encoding = "utf-8"
    declared = _ENCODING_RE.search(raw[:200].decode("ascii", "replace"))
    if declared:
        encoding = (declared.group(1) or declared.group(2) or "utf-8").lower()
    try:
        text = raw.decode(encoding)
    except (UnicodeDecodeError, LookupError) as exc:
        raise SvgParseError(f"cannot decode file as {encoding}: {exc}") from exc
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
