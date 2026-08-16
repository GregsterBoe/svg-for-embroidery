"""B0: turning a quantised bitmap into paths — by borrowing a tracer, not writing one.

Writing a tracer is a research project. Three usable ones already exist, so the
roadmap's instruction for this step is *buy, don't build*: wrap each candidate in
one interface, run them over the B1 corpus, and record which one wins and why.
:mod:`svg_embroidery.bench` does the comparing; this module only makes the
candidates comparable.

The interface is deliberately narrow, and shaped by what Phase A already checks
rather than by what the tracers offer:

**One group per colour, biggest first.** ``structure.color_layers`` wants a
top-level group per thread colour, and a stitching order that starts with the
largest area. Mask tracers give that naturally — they trace one colour at a time
— so the wrapper drives them once per palette entry and assembles the document
itself. A colour tracer produces its own SVG and is taken as it comes; that
difference is a finding, not a defect to paper over.

**Millimetres on the outside, pixels on the inside.** The document carries
``width``/``height`` in mm from the profile and a ``viewBox`` in working pixels,
so the result can be handed straight to ``svgemb check`` without a conversion
step in between.

Every backend is optional, and the module follows the same rule as
:mod:`svg_embroidery.geometry` and :mod:`svg_embroidery.raster`: a tracer you do
not have is a measurement you do not get. Nothing here is imported at module
load, so ``import svg_embroidery`` on a bare machine still works.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from .raster import RGB, Quantisation

#: Set this to pretend no tracer is installed, so CI can prove the degraded path
#: works rather than assuming it does.
DISABLE_ENV_VAR = "SVGEMB_NO_TRACER"

#: Point this at a potrace binary that is not on ``PATH``. Termux installs it in
#: the usual place, but an unpacked or self-built copy is common enough to be
#: worth supporting without a wrapper script.
POTRACE_ENV_VAR = "SVGEMB_POTRACE"

#: Speckles smaller than this many pixels are dropped by the mask tracers.
#: potrace's own default, kept so the comparison measures the tracers rather
#: than our tuning of them. B3 is where despeckling gets thought about properly.
TURDSIZE = 2

#: Corner threshold. 1.0 is potrace's default; lower is more corners.
ALPHAMAX = 1.0

#: Canvas used when nobody says otherwise, in mm. Only affects the mm/pixel
#: ratio written into the document, never the geometry.
DEFAULT_CANVAS_MM = 100.0

#: How long one image may take before the wrapper gives up on a subprocess.
TIMEOUT_SECONDS = 60

INSTALL_HINT = (
    "install a tracer: 'pkg install potrace' (Termux) or 'apt install potrace', "
    'or pip install "svg-for-embroidery[trace]" for the pure-Python fallback'
)


class TracerError(Exception):
    """Raised when a tracer is asked for work it cannot do here."""


def _package_version(distribution: str) -> str:
    """An installed distribution's version, or "" if it cannot be asked."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version(distribution)
    except PackageNotFoundError:
        return ""
    except Exception:  # noqa: BLE001 - metadata is not worth failing a run over
        return ""


def tracers_disabled() -> bool:
    return os.environ.get(DISABLE_ENV_VAR, "").strip() not in ("", "0", "false", "no")


# ----------------------------------------------------------------- results


@dataclass(frozen=True)
class Layer:
    """One colour's worth of traced outline."""

    #: Index into the quantisation's palette.
    index: int
    color: RGB
    #: Fraction of the image this colour covers, so the document can be ordered
    #: the way it will be stitched.
    area: float
    #: One or more ``d`` attributes.
    data: List[str]
    #: A transform the tracer's own coordinates need, or "" when the data is
    #: already in working pixels.
    transform: str = ""


@dataclass(frozen=True)
class Trace:
    """What one backend made of one image."""

    svg: str
    backend: str
    layers: int
    paths: int
    nodes: int
    seconds: float
    #: Non-empty when the result is usable but something about it is worth
    #: knowing — a colour tracer's flat output, for instance.
    notes: List[str] = field(default_factory=list)


# ---------------------------------------------------------------- backends


class Backend:
    """One way of getting paths out of a bitmap.

    Narrow on purpose: hand it a quantised image and a canvas size, get an SVG
    document back. Everything a tracer is usually configured with — turd size,
    corner threshold, colour precision — is fixed at the tracer's own default,
    because B0 compares tracers and B3 is where tuning belongs.
    """

    name = ""
    #: "mask" traces one colour at a time; "colour" swallows the whole image.
    kind = ""
    about = ""
    install = ""

    def available(self) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    def version(self) -> str:
        """What is installed, as precisely as it can be asked.

        Recorded alongside every benchmark run: two versions of the same tracer
        do not have to agree on a curve, and a silent upgrade would otherwise
        read as a code regression.
        """
        return ""

    def trace(self, quantisation: Quantisation, canvas_mm: float = DEFAULT_CANVAS_MM) -> Trace:  # pragma: no cover
        raise NotImplementedError


class MaskBackend(Backend):
    """A tracer that takes one black-and-white mask at a time.

    This is the shape our problem already has — a quantised image *is* a stack
    of masks — so these backends get the layered document for free, and the
    wrapper decides stitching order rather than the tracer.
    """

    kind = "mask"

    def _trace_mask(
        self, mask: Sequence[bool], width: int, height: int
    ) -> Tuple[List[str], str]:  # pragma: no cover - abstract
        """Path data for the True pixels, and any transform it needs."""
        raise NotImplementedError

    def trace(self, quantisation: Quantisation, canvas_mm: float = DEFAULT_CANVAS_MM) -> Trace:
        if not self.available():
            raise TracerError(f"{self.name} is not available here — {self.install}")
        started = time.monotonic()
        layers: List[Layer] = []
        order = sorted(
            range(quantisation.colors), key=quantisation.area, reverse=True
        )
        for index in order:
            area = quantisation.area(index)
            if area <= 0:
                continue
            mask = [value == index for value in quantisation.indices]
            data, transform = self._trace_mask(
                mask, quantisation.width, quantisation.height
            )
            data = [d for d in data if d.strip()]
            if not data:
                continue
            layers.append(
                Layer(
                    index=index,
                    color=quantisation.palette[index],
                    area=area,
                    data=data,
                    transform=transform,
                )
            )
        svg = layered_svg(layers, quantisation.width, quantisation.height, canvas_mm)
        paths, nodes = measure_svg(svg)
        return Trace(
            svg=svg,
            backend=self.name,
            layers=len(layers),
            paths=paths,
            nodes=nodes,
            seconds=time.monotonic() - started,
        )


class PotraceBinary(MaskBackend):
    """potrace(1), driven over a pipe.

    The reference implementation, and the reason the roadmap names it as the one
    to beat. It reads PBM on stdin and writes SVG on stdout, so no file ever
    touches disk and no Python binding has to be built — which matters on a
    phone, where compiling a C extension is the step that fails.
    """

    name = "potrace"
    about = "potrace(1) binary, one mask per colour"
    install = "pkg install potrace (Termux) or apt install potrace"

    def binary(self) -> Optional[str]:
        override = os.environ.get(POTRACE_ENV_VAR, "").strip()
        if override:
            return override if os.path.isfile(override) else None
        return shutil.which("potrace")

    def available(self) -> bool:
        return not tracers_disabled() and self.binary() is not None

    def version(self) -> str:
        binary = self.binary()
        if binary is None:
            return ""
        try:
            done = subprocess.run(
                [binary, "--version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):  # pragma: no cover - hostile PATH
            return ""
        # "potrace 1.16. Copyright (C) 2001-2019 Peter Selinger." — the number is
        # the only part of that worth keeping.
        found = re.search(r"\d+(?:\.\d+)+", done.stdout.decode("utf-8", "replace"))
        return found.group(0) if found else ""

    def _trace_mask(
        self, mask: Sequence[bool], width: int, height: int
    ) -> Tuple[List[str], str]:
        binary = self.binary()
        if binary is None:  # pragma: no cover - guarded by available()
            raise TracerError(f"potrace disappeared from PATH — {self.install}")
        try:
            done = subprocess.run(
                [binary, "--svg", "--turdsize", str(TURDSIZE),
                 "--alphamax", str(ALPHAMAX), "--output", "-", "-"],
                input=pbm_bytes(mask, width, height),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise TracerError(f"potrace gave up after {TIMEOUT_SECONDS}s") from exc
        except OSError as exc:
            raise TracerError(f"cannot run potrace: {exc}") from exc
        if done.returncode != 0:
            detail = done.stderr.decode("utf-8", "replace").strip() or "no message"
            raise TracerError(f"potrace failed: {detail}")

        out = done.stdout.decode("utf-8", "replace")
        transform = ""
        found = re.search(r"<g[^>]*\stransform=\"([^\"]*)\"", out)
        if found:
            transform = found.group(1)
        return re.findall(r"<path[^>]*\sd=\"([^\"]*)\"", out), transform


class PotracerLibrary(MaskBackend):
    """potracer, the pure-Python port of the same algorithm.

    Same curves, no binary to install — bought with numpy as a dependency and a
    large constant factor, both of which the spike measures rather than guesses
    at. Its value is as a fallback where a binary cannot be installed at all.
    """

    name = "potracer"
    about = "pure-Python potrace port, one mask per colour"
    install = 'pip install "svg-for-embroidery[trace]"'

    def available(self) -> bool:
        if tracers_disabled():
            return False
        try:
            import potrace  # noqa: F401
        except ImportError:
            return False
        return True

    def version(self) -> str:
        return _package_version("potracer")

    def _trace_mask(
        self, mask: Sequence[bool], width: int, height: int
    ) -> Tuple[List[str], str]:
        import numpy
        import potrace

        # potracer inverts whatever it is handed, on the assumption that it came
        # from an image where white is the background. Ours is already a mask of
        # the ink, so it goes in inverted and comes back out the right way up.
        grid = numpy.array(mask, dtype=bool).reshape((height, width))
        path = potrace.Bitmap(numpy.logical_not(grid)).trace(
            turdsize=TURDSIZE, alphamax=ALPHAMAX
        )
        # No transform: unlike the binary's SVG output, which carries potrace's
        # bottom-left origin and a x10 scale, the library hands back points in
        # image coordinates already.
        return [d for d in (_curve_data(curve) for curve in path) if d], ""


class VtracerLibrary(Backend):
    """VTracer, which traces colour directly.

    The odd one out: it quantises and traces in one step, so it is handed the
    reduced image as PNG and returns a finished document. Fast and good at
    photographs — but it decides its own colours and emits a flat list of
    paths, so what comes back needs restructuring before Phase A will accept it.
    """

    name = "vtracer"
    kind = "colour"
    about = "VTracer, whole image at once"
    install = 'pip install "svg-for-embroidery[trace]"'

    def available(self) -> bool:
        if tracers_disabled():
            return False
        try:
            import vtracer  # noqa: F401
        except ImportError:
            return False
        return True

    def version(self) -> str:
        return _package_version("vtracer")

    def trace(self, quantisation: Quantisation, canvas_mm: float = DEFAULT_CANVAS_MM) -> Trace:
        if not self.available():
            raise TracerError(f"{self.name} is not available here — {self.install}")
        import vtracer

        from .raster import encode_png

        started = time.monotonic()
        try:
            svg = vtracer.convert_raw_image_to_svg(
                encode_png(quantisation.raster()), img_format="png"
            )
        except Exception as exc:  # noqa: BLE001 - a Rust panic is not an ImportError
            raise TracerError(f"vtracer failed: {exc}") from exc

        fills = set(re.findall(r"fill=\"(#[0-9a-fA-F]{6})\"", svg))
        notes = []
        if "<g" not in svg:
            notes.append(
                "flat output: one <path> per shape with no colour groups, so it "
                "fails structure.color_layers until it is restructured"
            )
        if len(fills) > quantisation.colors:
            notes.append(
                f"chose its own {len(fills)} colours, ignoring the profile's "
                f"{quantisation.colors}"
            )
        paths, nodes = measure_svg(svg)
        return Trace(
            svg=svg,
            backend=self.name,
            layers=len(fills),
            paths=paths,
            nodes=nodes,
            seconds=time.monotonic() - started,
            notes=notes,
        )


#: Every candidate B0 considered, best first. The order is the spike's
#: conclusion, not a guess: see docs/spikes/B0-tracers.md.
ALL_BACKENDS: Tuple[Backend, ...] = (
    PotraceBinary(),
    PotracerLibrary(),
    VtracerLibrary(),
)


def available_backends() -> List[Backend]:
    """Every tracer installed on this machine, best first."""
    return [backend for backend in ALL_BACKENDS if backend.available()]


def default_backend() -> Optional[Backend]:
    """The tracer to use, or ``None`` when the machine has none."""
    found = available_backends()
    return found[0] if found else None


def backend_named(name: str) -> Backend:
    """Look one up by name, whether or not it is installed."""
    for backend in ALL_BACKENDS:
        if backend.name == name:
            return backend
    known = ", ".join(backend.name for backend in ALL_BACKENDS)
    raise TracerError(f"no such tracer: {name} — known tracers are {known}")


# ------------------------------------------------------------------ pieces


def pbm_bytes(mask: Sequence[bool], width: int, height: int) -> bytes:
    """A mask as binary PBM, which is what every mask tracer eats.

    P4 rather than P1 because a 256px image is 8KB instead of 65KB, and this
    goes down a pipe once per colour per image.
    """
    stride = (width + 7) // 8
    body = bytearray(stride * height)
    for y in range(height):
        row, base = y * stride, y * width
        for x in range(width):
            if mask[base + x]:
                body[row + (x >> 3)] |= 0x80 >> (x & 7)
    return f"P4\n{width} {height}\n".encode("ascii") + bytes(body)


def _point(value) -> Tuple[float, float]:
    """potracer hands back points as objects or as pairs, depending on version."""
    if hasattr(value, "x"):
        return float(value.x), float(value.y)
    return float(value[0]), float(value[1])


#: Decimal places kept on a coordinate. At the resolutions the bench works at, a
#: working pixel is around half a millimetre, so two decimals is ~5 microns —
#: three orders of magnitude below anything a needle can express. It is written
#: down because the alternative distorts the comparison: potrace's own writer
#: quantises to 0.1px, and letting potracer emit full float precision would have
#: made it look twice as bulky for a difference nothing can see.
COORDINATE_DECIMALS = 2


def _number(value: float) -> str:
    return f"{value:.{COORDINATE_DECIMALS}f}".rstrip("0").rstrip(".")


def _pair(point: Tuple[float, float]) -> str:
    return f"{_number(point[0])},{_number(point[1])}"


def _curve_data(curve) -> str:
    """One potracer curve as an SVG ``d`` attribute."""
    if curve.start_point is None:  # pragma: no cover - empty curve
        return ""
    parts = [f"M{_pair(_point(curve.start_point))}"]
    for segment in curve:
        end = _point(segment.end_point)
        if segment.is_corner:
            parts.append(f"L{_pair(_point(segment.c))}L{_pair(end)}")
        else:
            parts.append(
                f"C{_pair(_point(segment.c1))} {_pair(_point(segment.c2))} {_pair(end)}"
            )
    parts.append("Z")
    return "".join(parts)


def hex_color(color: RGB) -> str:
    return "#{:02x}{:02x}{:02x}".format(*color)


def layered_svg(
    layers: Sequence[Layer], width: int, height: int, canvas_mm: float = DEFAULT_CANVAS_MM
) -> str:
    """Assemble traced layers into the document Phase A wants to see.

    One group per colour, largest first, ``fill`` on the group rather than on
    each path — which is both smaller and what ``color.max_count`` counts.

    All of a layer's outlines go into a **single** ``<path>``, which is not
    cosmetic: a hole is a subpath wound against its container, and the moment it
    becomes its own element there is nothing left for it to cut a hole in, so it
    fills in solid. potrace's own SVG writer does the same thing for the same
    reason; potracer hands back loose curves and would otherwise lose every
    counter in the artwork.
    """
    scale = canvas_mm / max(width, height, 1)
    body = []
    for rank, layer in enumerate(layers, start=1):
        transform = f' transform="{layer.transform}"' if layer.transform else ""
        body.append(
            f'  <g id="colour-{rank}" fill="{hex_color(layer.color)}"{transform}>'
        )
        body.append(f'    <path d="{" ".join(layer.data)}"/>')
        body.append("  </g>")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{_number(width * scale)}mm" height="{_number(height * scale)}mm" '
        f'viewBox="0 0 {width} {height}">\n' + "\n".join(body) + "\n</svg>\n"
    )


def count_nodes(data: str) -> int:
    """Drawing commands in one ``d`` attribute.

    Path count alone rewards a tracer that draws one enormous ragged outline, so
    the spike counts nodes as well — that is what actually costs stitches.
    """
    from .geometry import iter_commands

    try:
        return sum(1 for command, _ in iter_commands(data) if command.upper() != "Z")
    except Exception:  # noqa: BLE001 - a tracer's output is not ours to trust
        return len(re.findall(r"[A-Za-z]", data))


def measure_svg(svg: str) -> Tuple[int, int]:
    """Subpaths and nodes in a finished document, whoever wrote it.

    Counted from the output rather than from each backend's internals, because
    the same drawing can be one ``<path>`` of forty subpaths or forty ``<path>``
    elements depending on the tracer's house style — and comparing those two
    numbers to each other says nothing about either tracer. Subpaths are the
    unit that survives the difference, and the unit an embroidery machine sees
    as a shape to fill.
    """
    from .document import iter_subpaths

    paths = nodes = 0
    for data in re.findall(r"<path[^>]*\sd=\"([^\"]*)\"", svg):
        paths += len(list(iter_subpaths(data)))
        nodes += count_nodes(data)
    return paths, nodes
