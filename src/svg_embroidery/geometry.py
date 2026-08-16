"""A5: the geometry layer — turning drawings into polygons and measuring them.

Two questions cannot be answered by looking at attributes, and both of them
need the same machinery:

* *What does this stroke look like once it is a filled shape?* — offsetting.
* *Is any part of this shape too thin to stitch?* — a morphological opening.

Phase B needs the same layer (growing masks so neighbouring colours don't leave
hairline gaps, dropping detail a needle can't render), so it is built once,
here, rather than inside a fixer.

The module is split in two on purpose:

**Flattening is pure Python and always available.** Turning ``d="M0 0 C…"``
into a list of points is de Casteljau plus the endpoint-arc formula — little
enough code to keep in the core, the same judgement :mod:`svg_embroidery.visual`
made about decoding PNG. So *measuring* a design never needs a compiled
dependency; only reshaping it does.

**Offsetting and booleans need a backend.** That is the one genuinely fragile
tier on a phone, so it follows the A1 pattern exactly: :func:`default_backend`
returns ``None`` when nothing is installed, callers skip, and a missing backend
is a measurement you don't get rather than a crash.

Not to be confused with :mod:`svg_embroidery.rules.geometry`, which holds the
rules about the *canvas* (size, aspect ratio, viewBox).
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from typing import Iterable, Iterator, List, Optional, Sequence, Tuple

from .document import Node, apply_matrix

Point = Tuple[float, float]

#: Largest deviation a flattened curve may have from the true curve, in the
#: units it is flattened in. 0.02 mm is well under a stitch, so nothing the
#: rules measure can hinge on it.
DEFAULT_TOLERANCE_MM = 0.02

#: Cap on segments per curve. A tolerance that would need more than this means
#: something pathological; stopping keeps a broken file from hanging the run.
MAX_SEGMENTS = 256

#: Straight segments per quarter circle when offsetting. Higher is a closer
#: circle and a slower buffer; 16 puts the error near 0.2% of the radius.
QUARTER_CIRCLE_SEGMENTS = 16

#: Set this to pretend no backend is installed, so CI can prove the degraded
#: path works rather than assuming it does.
DISABLE_ENV_VAR = "SVGEMB_NO_GEOMETRY"


class GeometryError(Exception):
    """Raised when a backend is present but could not do the operation."""


@dataclass(frozen=True)
class Contour:
    """One flattened outline: a run of points, and whether it closes."""

    points: List[Point]
    closed: bool = False

    def __len__(self) -> int:  # pragma: no cover - convenience
        return len(self.points)


# --------------------------------------------------------------- parsing

#: How many numbers each path command takes.
ARGUMENT_COUNT = {
    "M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7, "Z": 0,
}

_TOKEN_RE = re.compile(
    r"([MmZzLlHhVvCcSsQqTtAa])|([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)"
)

_NUMBER_RE = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")


def iter_commands(d: str) -> Iterator[Tuple[str, List[float]]]:
    """Yield ``(command, args)`` pairs, expanding implicit repeats.

    ``M 0 0 1 1 2 2`` means a move followed by two line-tos, which is why the
    repeat of ``M`` becomes ``L`` (and ``m`` becomes ``l``).
    """
    tokens = _TOKEN_RE.findall(d or "")
    index = 0
    command = ""
    while index < len(tokens):
        letter, number = tokens[index]
        if letter:
            command = letter
            index += 1
        elif not command:
            index += 1  # numbers before any command: malformed, skip
            continue

        count = ARGUMENT_COUNT[command.upper()]
        args: List[float] = []
        while len(args) < count:
            if index >= len(tokens) or tokens[index][0]:
                return  # ran out of numbers: stop rather than guess
            args.append(float(tokens[index][1]))
            index += 1
        yield command, args

        if command == "M":
            command = "L"
        elif command == "m":
            command = "l"


# ------------------------------------------------------------- flattening


def _cubic_segments(p0: Point, p1: Point, p2: Point, p3: Point, tolerance: float) -> int:
    """How many straight segments approximate this cubic within ``tolerance``.

    The error of an ``n``-segment uniform approximation is bounded by
    ``(3/4) * d / n²`` where ``d`` is the larger of the two second differences
    of the control polygon, so ``n = sqrt(3d / 4t)``.
    """
    d = max(
        math.hypot(p0[0] - 2 * p1[0] + p2[0], p0[1] - 2 * p1[1] + p2[1]),
        math.hypot(p1[0] - 2 * p2[0] + p3[0], p1[1] - 2 * p2[1] + p3[1]),
    )
    if d <= 0 or tolerance <= 0:
        return 1
    return max(1, min(MAX_SEGMENTS, math.ceil(math.sqrt(3.0 * d / (4.0 * tolerance)))))


def _flatten_cubic(
    p0: Point, p1: Point, p2: Point, p3: Point, tolerance: float
) -> List[Point]:
    """Points *after* ``p0`` along the curve."""
    steps = _cubic_segments(p0, p1, p2, p3, tolerance)
    points: List[Point] = []
    for step in range(1, steps + 1):
        t = step / steps
        u = 1.0 - t
        a, b, c, d = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
        points.append(
            (
                a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
                a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1],
            )
        )
    return points


def _quadratic_to_cubic(p0: Point, p1: Point, p2: Point) -> Tuple[Point, Point]:
    """The two cubic controls equivalent to one quadratic control point."""
    return (
        (p0[0] + 2.0 / 3.0 * (p1[0] - p0[0]), p0[1] + 2.0 / 3.0 * (p1[1] - p0[1])),
        (p2[0] + 2.0 / 3.0 * (p1[0] - p2[0]), p2[1] + 2.0 / 3.0 * (p1[1] - p2[1])),
    )


def _flatten_arc(
    start: Point,
    rx: float,
    ry: float,
    rotation: float,
    large_arc: bool,
    sweep: bool,
    end: Point,
    tolerance: float,
) -> List[Point]:
    """Endpoint-parameterised elliptical arc, sampled. Points after ``start``.

    Follows the SVG implementation notes: out-of-range radii are corrected
    rather than rejected, and a zero radius degenerates to a straight line —
    both of which real exports contain.
    """
    if start == end:
        return []
    rx, ry = abs(rx), abs(ry)
    if rx == 0 or ry == 0:
        return [end]

    phi = math.radians(rotation)
    cos_phi, sin_phi = math.cos(phi), math.sin(phi)
    dx2 = (start[0] - end[0]) / 2.0
    dy2 = (start[1] - end[1]) / 2.0
    x1 = cos_phi * dx2 + sin_phi * dy2
    y1 = -sin_phi * dx2 + cos_phi * dy2

    # Scale the radii up when they are too small to join the two endpoints.
    lam = (x1 * x1) / (rx * rx) + (y1 * y1) / (ry * ry)
    if lam > 1:
        scale = math.sqrt(lam)
        rx, ry = rx * scale, ry * scale

    numerator = rx * rx * ry * ry - rx * rx * y1 * y1 - ry * ry * x1 * x1
    denominator = rx * rx * y1 * y1 + ry * ry * x1 * x1
    factor = math.sqrt(max(0.0, numerator / denominator)) if denominator else 0.0
    if large_arc == sweep:
        factor = -factor
    cx1 = factor * rx * y1 / ry
    cy1 = -factor * ry * x1 / rx
    cx = cos_phi * cx1 - sin_phi * cy1 + (start[0] + end[0]) / 2.0
    cy = sin_phi * cx1 + cos_phi * cy1 + (start[1] + end[1]) / 2.0

    def angle(ux: float, uy: float, vx: float, vy: float) -> float:
        dot = ux * vx + uy * vy
        norm = math.hypot(ux, uy) * math.hypot(vx, vy)
        if norm == 0:
            return 0.0
        value = math.acos(max(-1.0, min(1.0, dot / norm)))
        return -value if ux * vy - uy * vx < 0 else value

    theta = angle(1.0, 0.0, (x1 - cx1) / rx, (y1 - cy1) / ry)
    delta = angle(
        (x1 - cx1) / rx, (y1 - cy1) / ry, (-x1 - cx1) / rx, (-y1 - cy1) / ry
    )
    if not sweep and delta > 0:
        delta -= 2 * math.pi
    elif sweep and delta < 0:
        delta += 2 * math.pi

    # Sagitta of a chord subtending Δ on radius r is r(1 - cos(Δ/2)) ≈ rΔ²/8.
    radius = max(rx, ry)
    step = 2.0 * math.sqrt(2.0 * tolerance / radius) if tolerance > 0 else math.pi / 8
    steps = max(2, min(MAX_SEGMENTS, math.ceil(abs(delta) / max(step, 1e-6))))

    points: List[Point] = []
    for index in range(1, steps + 1):
        angle_at = theta + delta * index / steps
        px = rx * math.cos(angle_at)
        py = ry * math.sin(angle_at)
        points.append(
            (cos_phi * px - sin_phi * py + cx, sin_phi * px + cos_phi * py + cy)
        )
    points[-1] = end  # land exactly, whatever the trigonometry rounded to
    return points


def flatten_path(d: str, tolerance: float = DEFAULT_TOLERANCE_MM) -> List[Contour]:
    """Turn a ``d`` attribute into straight-line contours.

    Curves are subdivided until they sit within ``tolerance`` of the true
    curve, in the same units the path is written in.
    """
    contours: List[Contour] = []
    points: List[Point] = []
    current: Point = (0.0, 0.0)
    start: Point = (0.0, 0.0)
    previous_control: Optional[Point] = None
    previous_command = ""

    def flush(closed: bool) -> None:
        if len(points) >= 2:
            contours.append(Contour(points=list(points), closed=closed))
        points.clear()

    for command, args in iter_commands(d):
        upper = command.upper()
        relative = command.islower()

        def absolute(index: int) -> Point:
            x, y = args[index], args[index + 1]
            return (current[0] + x, current[1] + y) if relative else (x, y)

        if upper == "M":
            flush(False)
            current = absolute(0)
            start = current
            points.append(current)
        elif upper == "Z":
            if points:
                flush(True)
            current = start
            points.append(current)
        elif upper == "L":
            current = absolute(0)
            points.append(current)
        elif upper == "H":
            current = (current[0] + args[0] if relative else args[0], current[1])
            points.append(current)
        elif upper == "V":
            current = (current[0], current[1] + args[0] if relative else args[0])
            points.append(current)
        elif upper in ("C", "S", "Q", "T", "A"):
            if not points:
                points.append(current)
            if upper == "C":
                c1, c2, end = absolute(0), absolute(2), absolute(4)
            elif upper == "S":
                reflected = (
                    (2 * current[0] - previous_control[0], 2 * current[1] - previous_control[1])
                    if previous_control is not None and previous_command in ("C", "S")
                    else current
                )
                c1, c2, end = reflected, absolute(0), absolute(2)
            elif upper in ("Q", "T"):
                if upper == "Q":
                    control, end = absolute(0), absolute(2)
                else:
                    control = (
                        (2 * current[0] - previous_control[0], 2 * current[1] - previous_control[1])
                        if previous_control is not None and previous_command in ("Q", "T")
                        else current
                    )
                    end = absolute(0)
                c1, c2 = _quadratic_to_cubic(current, control, end)
                previous_control = control
            else:  # A
                end = (
                    (current[0] + args[5], current[1] + args[6]) if relative
                    else (args[5], args[6])
                )
                points.extend(
                    _flatten_arc(
                        current, args[0], args[1], args[2],
                        bool(args[3]), bool(args[4]), end, tolerance,
                    )
                )
                current = end
                previous_control = None
                previous_command = upper
                continue

            points.extend(_flatten_cubic(current, c1, c2, end, tolerance))
            previous_control = c2 if upper in ("C", "S") else previous_control
            current = end

        if upper not in ("C", "S", "Q", "T"):
            previous_control = None
        previous_command = upper

    flush(False)
    return contours


def _numbers(value: Optional[str]) -> List[float]:
    if not value:
        return []
    return [float(match.group()) for match in _NUMBER_RE.finditer(value)]


def _length(node: Node, name: str, default: float = 0.0) -> float:
    numbers = _numbers(node.element.get(name))
    return numbers[0] if numbers else default


def _ellipse_contour(cx: float, cy: float, rx: float, ry: float, tolerance: float) -> Contour:
    radius = max(rx, ry)
    step = 2.0 * math.sqrt(2.0 * tolerance / radius) if tolerance > 0 and radius > 0 else 0.1
    steps = max(8, min(MAX_SEGMENTS, math.ceil(2 * math.pi / max(step, 1e-6))))
    steps += -steps % 4  # a multiple of 4 lands a point on each extreme exactly
    points = [
        (cx + rx * math.cos(2 * math.pi * i / steps), cy + ry * math.sin(2 * math.pi * i / steps))
        for i in range(steps)
    ]
    return Contour(points=points, closed=True)


def node_contours(node: Node, tolerance: float = DEFAULT_TOLERANCE_MM) -> List[Contour]:
    """Flatten one element into contours, in the element's own coordinates.

    Returns an empty list for anything whose outline we cannot know without a
    font or a resolved reference — ``<text>``, ``<image>``, ``<use>``. Callers
    report those as unmeasured rather than treating them as empty.
    """
    tag = node.tag
    if tag == "path":
        return flatten_path(node.element.get("d") or "", tolerance)
    if tag == "rect":
        x, y = _length(node, "x"), _length(node, "y")
        width, height = _length(node, "width"), _length(node, "height")
        if width <= 0 or height <= 0:
            return []
        rx = _numbers(node.element.get("rx"))
        ry = _numbers(node.element.get("ry"))
        radius_x = rx[0] if rx else (ry[0] if ry else 0.0)
        radius_y = ry[0] if ry else radius_x
        radius_x = min(radius_x, width / 2)
        radius_y = min(radius_y, height / 2)
        if radius_x <= 0 or radius_y <= 0:
            points = [(x, y), (x + width, y), (x + width, y + height), (x, y + height)]
            return [Contour(points=points, closed=True)]
        # Rounded corners: four quarter-ellipse arcs joined by the straight runs.
        d = (
            f"M{x + radius_x} {y} H{x + width - radius_x} "
            f"A{radius_x} {radius_y} 0 0 1 {x + width} {y + radius_y} "
            f"V{y + height - radius_y} "
            f"A{radius_x} {radius_y} 0 0 1 {x + width - radius_x} {y + height} "
            f"H{x + radius_x} "
            f"A{radius_x} {radius_y} 0 0 1 {x} {y + height - radius_y} "
            f"V{y + radius_y} "
            f"A{radius_x} {radius_y} 0 0 1 {x + radius_x} {y} Z"
        )
        return flatten_path(d, tolerance)
    if tag == "circle":
        radius = _length(node, "r")
        if radius <= 0:
            return []
        return [_ellipse_contour(_length(node, "cx"), _length(node, "cy"), radius, radius, tolerance)]
    if tag == "ellipse":
        rx, ry = _length(node, "rx"), _length(node, "ry")
        if rx <= 0 or ry <= 0:
            return []
        return [_ellipse_contour(_length(node, "cx"), _length(node, "cy"), rx, ry, tolerance)]
    if tag == "line":
        return [
            Contour(
                points=[
                    (_length(node, "x1"), _length(node, "y1")),
                    (_length(node, "x2"), _length(node, "y2")),
                ],
                closed=False,
            )
        ]
    if tag in ("polyline", "polygon"):
        numbers = _numbers(node.element.get("points"))
        points = list(zip(numbers[0::2], numbers[1::2]))
        if len(points) < 2:
            return []
        return [Contour(points=points, closed=tag == "polygon")]
    return []


def transform_contours(contours: Sequence[Contour], matrix: Sequence[float], scale: float = 1.0) -> List[Contour]:
    """Map contours through a matrix, then multiply by a uniform ``scale``.

    ``scale`` is how the caller gets from root user units to millimetres.
    """
    result: List[Contour] = []
    for contour in contours:
        moved: List[Point] = []
        for point in contour.points:
            x, y = apply_matrix(matrix, point)
            moved.append((x * scale, y * scale))
        result.append(Contour(points=moved, closed=contour.closed))
    return result


def node_contours_mm(
    node: Node, unit_scale: float, tolerance_mm: float = DEFAULT_TOLERANCE_MM
) -> List[Contour]:
    """Flatten an element into millimetre coordinates.

    Curves are flattened in the element's own units at the tolerance that
    *becomes* ``tolerance_mm`` once the transforms are applied, so a shape
    inside ``transform="scale(100)"`` is not approximated a hundred times more
    coarsely than one that isn't.
    """
    factor = unit_scale * node.transform_scale
    local_tolerance = tolerance_mm / factor if factor > 0 else tolerance_mm
    contours = node_contours(node, local_tolerance)
    return transform_contours(contours, node.matrix, unit_scale)


# ----------------------------------------------------------- path output


def _format(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"


def contours_to_path_data(contours: Iterable[Contour]) -> str:
    """Write contours back out as a ``d`` attribute, every subpath closed."""
    parts: List[str] = []
    for contour in contours:
        points = contour.points
        if len(points) < 2:
            continue
        if points[0] == points[-1]:
            points = points[:-1]  # "Z" says it; repeating the point is noise
        if len(points) < 3:
            continue
        parts.append("M " + " L ".join(f"{_format(x)} {_format(y)}" for x, y in points) + " Z")
    return " ".join(parts)


# -------------------------------------------------------------- backends


class Backend:
    """One implementation of the polygon operations.

    Deliberately narrow: build a polygon from contours, grow or shrink it,
    measure it, and read it back as contours. Everything the rules and fixers
    need is expressed in those four.
    """

    name = ""

    def polygon(self, contours: Sequence[Contour]):  # pragma: no cover - abstract
        raise NotImplementedError

    def stroke(self, contours: Sequence[Contour], width: float, cap: str, join: str):  # pragma: no cover
        raise NotImplementedError

    def grow(self, shape, distance: float):  # pragma: no cover - abstract
        raise NotImplementedError

    def area(self, shape) -> float:  # pragma: no cover - abstract
        raise NotImplementedError

    def is_empty(self, shape) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    def union(self, first, second):  # pragma: no cover - abstract
        raise NotImplementedError

    def intersect(self, first, second):  # pragma: no cover - abstract
        raise NotImplementedError

    def contours(self, shape) -> List[Contour]:  # pragma: no cover - abstract
        raise NotImplementedError


#: SVG's linecap/linejoin names, in the words the backend uses.
_CAP_STYLES = {"butt": "flat", "round": "round", "square": "square"}
_JOIN_STYLES = {"miter": "mitre", "miter-clip": "mitre", "round": "round", "bevel": "bevel"}


class ShapelyBackend(Backend):
    """GEOS via shapely.

    Chosen over pyclipper because the work here is not only offsetting: the
    thinness test needs areas and validity repair as well, and shapely has all
    three in one library with floating-point coordinates. pyclipper offsets
    integers, which would mean picking a scale factor and living with it.
    """

    name = "shapely"

    def __init__(self) -> None:
        import shapely  # imported lazily: optional dependency

        self._shapely = shapely

    # -- helpers ------------------------------------------------------
    def _valid(self, geometry):
        """Repair self-intersections; return only the polygonal parts."""
        from shapely.geometry import GeometryCollection, MultiPolygon, Polygon

        if not geometry.is_valid:
            geometry = self._shapely.make_valid(geometry)
        if isinstance(geometry, (Polygon, MultiPolygon)):
            return geometry
        if isinstance(geometry, GeometryCollection):
            polygons = [part for part in geometry.geoms if isinstance(part, (Polygon, MultiPolygon))]
            if polygons:
                return self._shapely.union_all(polygons)
        return Polygon()

    # -- the operations -----------------------------------------------
    def polygon(self, contours: Sequence[Contour]):
        """Combine contours with the even-odd rule.

        Folding with symmetric difference is exactly even-odd, which is what
        makes a donut come out as a ring rather than a disc. Artwork drawn for
        the nonzero rule agrees with it in every case that matters here —
        holes are drawn inside their shell, not overlapping it.
        """
        from shapely.geometry import Polygon

        result = None
        for contour in contours:
            points = contour.points
            if len(points) < 3:
                continue
            ring = self._valid(Polygon(points))
            if ring.is_empty:
                continue
            result = ring if result is None else result.symmetric_difference(ring)
        return Polygon() if result is None else self._valid(result)

    def stroke(self, contours: Sequence[Contour], width: float, cap: str, join: str):
        from shapely.geometry import LinearRing, LineString, Polygon

        if width <= 0:
            return Polygon()
        pieces = []
        for contour in contours:
            points = contour.points
            if len(points) < 2:
                continue
            if contour.closed and len(points) >= 3:
                geometry = LinearRing(points)
            else:
                geometry = LineString(points)
            pieces.append(
                geometry.buffer(
                    width / 2.0,
                    quad_segs=QUARTER_CIRCLE_SEGMENTS,
                    cap_style=_CAP_STYLES.get(cap, "flat"),
                    join_style=_JOIN_STYLES.get(join, "mitre"),
                )
            )
        if not pieces:
            return Polygon()
        return self._valid(self._shapely.union_all(pieces))

    def grow(self, shape, distance: float):
        if distance == 0:
            return shape
        return self._valid(
            shape.buffer(distance, quad_segs=QUARTER_CIRCLE_SEGMENTS, join_style="round")
        )

    def area(self, shape) -> float:
        return float(shape.area)

    def is_empty(self, shape) -> bool:
        return bool(shape.is_empty)

    def union(self, first, second):
        return self._valid(first.union(second))

    def intersect(self, first, second):
        return self._valid(first.intersection(second))

    def contours(self, shape) -> List[Contour]:
        from shapely.geometry import MultiPolygon, Polygon
        from shapely.geometry.polygon import orient

        polygons: List = []
        if isinstance(shape, Polygon):
            polygons = [shape] if not shape.is_empty else []
        elif isinstance(shape, MultiPolygon):
            polygons = list(shape.geoms)

        result: List[Contour] = []
        for polygon in polygons:
            # Shell counter-clockwise, holes clockwise: with SVG's default
            # nonzero fill rule that is what makes a hole a hole.
            oriented = orient(polygon, sign=1.0)
            result.append(Contour(points=list(oriented.exterior.coords), closed=True))
            for interior in oriented.interiors:
                result.append(Contour(points=list(interior.coords), closed=True))
        return result


def backends_disabled() -> bool:
    return os.environ.get(DISABLE_ENV_VAR, "").strip() not in ("", "0", "false", "no")


def available_backends() -> List[Backend]:
    """Every geometry backend installed on this machine."""
    if backends_disabled():
        return []
    try:
        return [ShapelyBackend()]
    except ImportError:
        return []


def default_backend() -> Optional[Backend]:
    """The backend to use, or ``None`` when the machine has none."""
    found = available_backends()
    return found[0] if found else None


#: What ``svgemb doctor`` should tell you to install when there is none.
INSTALL_HINT = 'install the geometry extra: pip install "svg-for-embroidery[geometry]"'


# ------------------------------------------------------ the measurements


@dataclass(frozen=True)
class Thinness:
    """How much of a shape is too thin to stitch."""

    #: Area of the shape as drawn, in mm².
    area: float
    #: Area left after removing everything narrower than the limit.
    surviving_area: float
    #: The width that was tested, in mm.
    min_width: float
    #: Area the corners were always going to cost (see :func:`corner_allowance`).
    corner_allowance: float = 0.0

    @property
    def lost_area(self) -> float:
        return max(0.0, self.area - self.surviving_area)

    @property
    def excess_area(self) -> float:
        """Lost area that the corners do *not* explain — the real defect."""
        return max(0.0, self.lost_area - self.corner_allowance)

    @property
    def excess_ratio(self) -> float:
        return self.excess_area / self.area if self.area > 0 else 0.0

    @property
    def vanishes(self) -> bool:
        """True when nothing at all survives — the whole shape is a hairline."""
        return self.surviving_area <= 0.0


#: Most one corner may be allowed, as a multiple of r². Past roughly 123° of
#: turn the formula runs away, and a shape that turns that sharply is a spike —
#: exactly the defect being looked for, so it does not get an allowance.
_MAX_CORNER_ALLOWANCE = 1.0


def corner_allowance(contours: Sequence[Contour], min_width: float) -> float:
    """Area an opening removes at sharp corners even when nothing is thin.

    A disc of radius ``r`` cannot reach the tip of a corner, so opening a
    perfectly stitchable square still rounds all four of them off. The loss at
    a corner that turns by ``θ`` is ``r²(tan(θ/2) − θ/2)``: for a right angle
    that is ``r²(4−π)/4``, and a 5 mm square at a 1.5 mm limit loses 1.9% of
    its area to nothing but its own corners.

    This is real geometry rather than numerical noise, so the rule subtracts it
    instead of hiding it under a threshold. Flattened curves contribute almost
    nothing — the loss falls off as ``θ³`` — so a circle is allowed nothing and
    needs nothing.
    """
    radius = min_width / 2.0
    total = 0.0
    for contour in contours:
        points = contour.points
        if len(points) >= 2 and points[0] == points[-1]:
            points = points[:-1]
        count = len(points)
        if count < 3:
            continue
        for index in range(count):
            before = points[index - 1]
            here = points[index]
            after = points[(index + 1) % count]
            ax, ay = here[0] - before[0], here[1] - before[1]
            bx, by = after[0] - here[0], after[1] - here[1]
            turn = abs(math.atan2(ax * by - ay * bx, ax * bx + ay * by))
            if turn <= 0:
                continue
            half = min(turn / 2.0, math.pi / 2.0 - 1e-9)
            total += radius * radius * min(
                math.tan(half) - half, _MAX_CORNER_ALLOWANCE
            )
    return total


def stroke_outline(
    contours: Sequence[Contour],
    width: float,
    cap: str = "butt",
    join: str = "miter",
    backend: Optional[Backend] = None,
) -> Optional[List[Contour]]:
    """The filled shape a stroke of ``width`` paints along ``contours``.

    ``None`` when no backend is installed.
    """
    backend = backend or default_backend()
    if backend is None:
        return None
    try:
        shape = backend.stroke(contours, width, cap, join)
    except Exception as exc:  # noqa: BLE001 - third-party failure modes vary
        raise GeometryError(f"{backend.name} could not outline the stroke: {exc}") from exc
    if backend.is_empty(shape):
        return []
    return backend.contours(shape)


def trim_thin_detail(
    contours: Sequence[Contour], min_width: float, backend: Optional[Backend] = None
) -> Optional[List[Contour]]:
    """The shape with its thin runs cut away and its corners left alone.

    The opening on its own — shrink by half the width, grow back — finds the
    thin detail perfectly, but it is not a *repair*: it also rounds off every
    corner in the design, including the ones :func:`corner_allowance` just
    finished excusing. The fix would then change far more of the drawing than
    the check ever complained about, and hand back a shape whose every right
    angle is a sixteen-segment arc.

    Growing the eroded core back by *twice* the radius and intersecting with
    the original fixes that. A corner sits ``r√2`` from the core, comfortably
    inside ``2r``, so every boundary that was fine comes back exactly as it was
    drawn — straight lines stay straight lines. The thin runs stay cut, because
    there is no core near them to grow back from.

    What survives is a stub of up to half the minimum width where a thin part
    met a solid one. The check tolerates that for the same reason it tolerates
    corners: it is a fraction of a stitch, not a feature.
    """
    backend = backend or default_backend()
    if backend is None:
        return None
    shape = backend.polygon(contours)
    if backend.is_empty(shape):
        return []
    radius = min_width / 2.0
    core = backend.grow(shape, -radius)
    if backend.is_empty(core):
        return []  # nothing in the shape is thick enough to keep
    result = backend.intersect(shape, backend.grow(core, 2.0 * radius))
    if backend.is_empty(result):
        return []
    return backend.contours(result)


def measure_thinness(
    contours: Sequence[Contour],
    min_width: float,
    backend: Optional[Backend] = None,
    stroke_width: float = 0.0,
    cap: str = "butt",
    join: str = "miter",
) -> Optional[Thinness]:
    """How much of a shape would not survive being stitched at ``min_width``.

    ``stroke_width`` widens the painted region first: a hairline path with a
    fat stroke on it is not a thin feature, and measuring the fill alone would
    say it was.

    The corner allowance is computed alongside, so the caller can tell a shape
    that is genuinely too thin from one that merely has corners.

    Returns ``None`` when there is no backend, or when the shape encloses no
    area at all (an open polyline with no fill, say) — nothing to measure.
    """
    backend = backend or default_backend()
    if backend is None:
        return None
    try:
        shape = backend.polygon(contours)
        if stroke_width > 0:
            shape = backend.union(shape, backend.stroke(contours, stroke_width, cap, join))
        if backend.is_empty(shape):
            return None
        area = backend.area(shape)
        if area <= 0:
            return None
        radius = min_width / 2.0
        survivor = backend.grow(backend.grow(shape, -radius), radius)
        surviving = 0.0 if backend.is_empty(survivor) else backend.area(survivor)
    except Exception as exc:  # noqa: BLE001 - third-party failure modes vary
        raise GeometryError(f"{backend.name} could not measure the shape: {exc}") from exc
    return Thinness(
        area=area,
        surviving_area=surviving,
        min_width=min_width,
        corner_allowance=corner_allowance(contours, min_width),
    )
