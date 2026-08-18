"""A1: measuring whether a change altered the image.

Every fixer needs one question answered — *did this change how the design
looks?* — and Phase B needs the same machinery to score traced output against
its source. So it lives here, once.

Rendering is delegated to whatever is installed (``rsvg-convert``, ``resvg``,
``cairosvg``, Inkscape). Decoding and comparison are pure Python: PNG is zlib
plus five filter types, which is little enough code that the harness stays free
of compiled dependencies and keeps working on a phone.

Nothing here is required. With no renderer installed, :func:`visual_difference`
returns ``None`` and callers skip the comparison rather than failing — a
missing renderer is a missing measurement, not a broken build.
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

#: Rendered width used for comparisons. Big enough to show a thin stroke
#: changing, small enough that a pure-Python decode stays quick.
DEFAULT_WIDTH = 256

#: Per-channel difference (0-255) below which two pixels count as equal.
DEFAULT_TOLERANCE = 2

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: Bytes per pixel for the PNG colour types renderers actually emit.
_CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}


class RenderError(Exception):
    """Raised when a renderer is present but could not draw the file."""


class PngError(Exception):
    """Raised for PNG data we cannot decode."""


# ---------------------------------------------------------------- rasters


@dataclass(frozen=True)
class Raster:
    """A decoded image: ``width * height`` pixels, 4 bytes each (RGBA)."""

    width: int
    height: int
    pixels: bytes

    @property
    def size(self) -> Tuple[int, int]:
        return (self.width, self.height)

    def composite_over(self, background: Tuple[int, int, int] = (255, 255, 255)) -> bytes:
        """Flatten onto a solid background, returning RGB bytes.

        This is not cosmetic. Renderers leave arbitrary colour values in fully
        transparent pixels, so comparing raw RGBA reports differences in parts
        of the image nobody can see. Compositing first compares what is
        actually visible.
        """
        out = bytearray(self.width * self.height * 3)
        pixels = self.pixels
        br, bg, bb = background
        for index in range(0, len(pixels), 4):
            alpha = pixels[index + 3]
            target = (index // 4) * 3
            if alpha == 255:
                out[target : target + 3] = pixels[index : index + 3]
            elif alpha == 0:
                out[target] = br
                out[target + 1] = bg
                out[target + 2] = bb
            else:
                inverse = 255 - alpha
                out[target] = (pixels[index] * alpha + br * inverse) // 255
                out[target + 1] = (pixels[index + 1] * alpha + bg * inverse) // 255
                out[target + 2] = (pixels[index + 2] * alpha + bb * inverse) // 255
        return bytes(out)


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _unfilter(raw: bytes, width: int, height: int, channels: int) -> bytearray:
    """Undo PNG's per-scanline filters."""
    stride = width * channels
    out = bytearray(stride * height)
    previous = bytearray(stride)
    position = 0
    for row in range(height):
        filter_type = raw[position]
        position += 1
        line = bytearray(raw[position : position + stride])
        position += stride

        if filter_type == 1:  # Sub
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif filter_type == 2:  # Up
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 0xFF
        elif filter_type == 3:  # Average
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif filter_type == 4:  # Paeth
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                upper_left = previous[i - channels] if i >= channels else 0
                line[i] = (line[i] + _paeth(left, previous[i], upper_left)) & 0xFF
        elif filter_type != 0:
            raise PngError(f"unknown PNG filter type {filter_type}")

        out[row * stride : (row + 1) * stride] = line
        previous = line
    return out


def decode_png(data: bytes) -> Raster:
    """Decode an 8-bit, non-interlaced PNG into RGBA.

    That covers what every SVG renderer emits. Anything else raises, rather
    than returning an image that quietly means something different.
    """
    if not data.startswith(PNG_SIGNATURE):
        raise PngError("not a PNG file")

    position = len(PNG_SIGNATURE)
    header: Optional[Tuple[int, int, int, int, int]] = None
    idat = bytearray()
    palette = b""
    transparency = b""

    while position + 8 <= len(data):
        (length,) = struct.unpack(">I", data[position : position + 4])
        kind = data[position + 4 : position + 8]
        body = data[position + 8 : position + 8 + length]
        position += 12 + length  # length + type + body + CRC

        if kind == b"IHDR":
            width, height, depth, color_type, _, _, interlace = struct.unpack(
                ">IIBBBBB", body
            )
            header = (width, height, depth, color_type, interlace)
        elif kind == b"IDAT":
            idat += body
        elif kind == b"PLTE":
            palette = body
        elif kind == b"tRNS":
            transparency = body
        elif kind == b"IEND":
            break

    if header is None:
        raise PngError("PNG has no header chunk")
    width, height, depth, color_type, interlace = header
    if depth != 8:
        raise PngError(f"unsupported PNG bit depth {depth}")
    if interlace:
        raise PngError("interlaced PNGs are not supported")
    if color_type == 3 and not palette:
        raise PngError("paletted PNG without a palette")
    if color_type not in _CHANNELS and color_type != 3:
        raise PngError(f"unsupported PNG colour type {color_type}")

    channels = 1 if color_type == 3 else _CHANNELS[color_type]
    raw = _unfilter(zlib.decompress(bytes(idat)), width, height, channels)

    rgba = bytearray(width * height * 4)
    for index in range(width * height):
        source = index * channels
        target = index * 4
        if color_type == 0:  # greyscale
            value = raw[source]
            rgba[target : target + 4] = bytes((value, value, value, 255))
        elif color_type == 2:  # RGB
            rgba[target : target + 3] = raw[source : source + 3]
            rgba[target + 3] = 255
        elif color_type == 3:  # palette
            entry = raw[source] * 3
            rgba[target : target + 3] = palette[entry : entry + 3]
            alpha_index = raw[source]
            rgba[target + 3] = (
                transparency[alpha_index] if alpha_index < len(transparency) else 255
            )
        elif color_type == 4:  # greyscale + alpha
            value = raw[source]
            rgba[target : target + 4] = bytes((value, value, value, raw[source + 1]))
        else:  # RGBA
            rgba[target : target + 4] = raw[source : source + 4]
    return Raster(width=width, height=height, pixels=bytes(rgba))


# ------------------------------------------------------------ comparison


@dataclass(frozen=True)
class Difference:
    """How far apart two renderings are."""

    changed_pixels: int
    total_pixels: int
    max_delta: int
    mean_delta: float
    note: str = ""

    @property
    def ratio(self) -> float:
        return self.changed_pixels / self.total_pixels if self.total_pixels else 0.0

    @property
    def identical(self) -> bool:
        return self.changed_pixels == 0

    def within(self, budget: float) -> bool:
        """True when at most ``budget`` (0-1) of the image changed."""
        return self.ratio <= budget

    def to_dict(self) -> Dict[str, Any]:
        return {
            "changed_pixels": self.changed_pixels,
            "total_pixels": self.total_pixels,
            "ratio": self.ratio,
            "max_delta": self.max_delta,
            "mean_delta": self.mean_delta,
            "identical": self.identical,
            "note": self.note,
            "text": str(self),
        }

    def __str__(self) -> str:
        if self.identical:
            return "images identical"
        return (
            f"{self.changed_pixels}/{self.total_pixels} pixels differ "
            f"({self.ratio:.2%}), max channel delta {self.max_delta}"
        )


def compare_rasters(
    left: Raster, right: Raster, tolerance: int = DEFAULT_TOLERANCE
) -> Difference:
    """Compare two rasters after flattening them onto white."""
    if left.size != right.size:
        return Difference(
            changed_pixels=max(left.width * left.height, right.width * right.height),
            total_pixels=max(left.width * left.height, right.width * right.height),
            max_delta=255,
            mean_delta=255.0,
            note=f"different sizes: {left.size} vs {right.size}",
        )

    a = left.composite_over()
    b = right.composite_over()
    total = left.width * left.height
    changed = 0
    max_delta = 0
    sum_delta = 0

    for index in range(0, len(a), 3):
        delta = max(
            abs(a[index] - b[index]),
            abs(a[index + 1] - b[index + 1]),
            abs(a[index + 2] - b[index + 2]),
        )
        if delta:
            sum_delta += delta
            if delta > max_delta:
                max_delta = delta
            if delta > tolerance:
                changed += 1

    return Difference(
        changed_pixels=changed,
        total_pixels=total,
        max_delta=max_delta,
        mean_delta=sum_delta / total if total else 0.0,
    )


def composite_behind(
    raster: Raster, ground: Raster, where: Sequence[bool]
) -> Raster:
    """Put ``ground`` behind ``raster``, but only in the pixels ``where`` marks.

    B8's other half. A converted document has a hole where its source had
    paper, and :func:`compare_rasters` flattens both sides onto white — so an
    off-white scan reports 88% of itself as wrong and a dark photograph reports
    all of it, about a conversion that is very nearly perfect. The design is
    going to be sewn onto fabric, and the best stand-in for that fabric is the
    ground the source actually had, pixel for pixel.

    **Only inside the mask**, which is the whole care in this function. Filling
    every transparent pixel from the reference would paint a seam that opened
    over *artwork* in exactly the colour the artwork should have been, and
    score it correct — hiding the one failure `gaps` exists to find. So a hole
    is excused where it was asked for and nowhere else.
    """
    if raster.size != ground.size:
        return raster
    out = bytearray(raster.pixels)
    behind = ground.pixels
    for pixel in range(raster.width * raster.height):
        index = pixel * 4
        if not where[pixel]:
            continue
        alpha = out[index + 3]
        if alpha == 255:
            continue  # something painted here after all, and it has to answer for it
        inverse = 255 - alpha
        for channel in range(3):
            out[index + channel] = (
                out[index + channel] * alpha + behind[index + channel] * inverse
            ) // 255
        out[index + 3] = 255
    return Raster(width=raster.width, height=raster.height, pixels=bytes(out))


# ---------------------------------------------------------- show-through


@dataclass(frozen=True)
class ShowThrough:
    """How much of a rendered document has no paint on it at all."""

    #: Pixels the artwork does not fully cover.
    pixels: int
    total_pixels: int
    #: Those pixels added up by how much each is missing, in whole pixels. A
    #: pixel at three-quarters coverage contributes 0.25.
    missing: float
    #: The least-covered pixel, 0-255. 255 means nothing showed through at all,
    #: and anything near 0 is an outright hole rather than a soft edge.
    worst: int

    @property
    def ratio(self) -> float:
        """Share of the image touched by a gap, however faintly."""
        return self.pixels / self.total_pixels if self.total_pixels else 0.0

    @property
    def area(self) -> float:
        """Share of the image genuinely unpainted — the headline number.

        The one to grade a seam on: :attr:`ratio` counts a pixel that is 99%
        covered the same as a hole, which flatters nothing but exaggerates
        everything.
        """
        return self.missing / self.total_pixels if self.total_pixels else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pixels": self.pixels,
            "total_pixels": self.total_pixels,
            "ratio": self.ratio,
            "area": self.area,
            "worst": self.worst,
            "text": str(self),
        }

    def __str__(self) -> str:
        if not self.pixels:
            return "nothing shows through"
        return (
            f"{self.pixels}/{self.total_pixels} pixels not fully covered "
            f"({self.area:.2%} of the image missing, worst alpha {self.worst})"
        )


def show_through(
    raster: Raster,
    margin: int = 1,
    ignore: Optional[Sequence[bool]] = None,
) -> ShowThrough:
    """Measure where a rendering has no paint on it — B4's seam instrument.

    Two shapes drawn edge to edge do not join. Even when their outlines agree to
    the last decimal, each covers half of the boundary pixel and the two halves
    composite to three-quarters, so a hairline of page shows between them; where
    the outlines *disagree*, which is what separately-traced colour masks always
    do, the hairline is a real gap. Both land in the same place: the renderer
    writes an alpha below 255, and that is the whole measurement. No second
    render against a contrasting background, no threshold, no guessing which
    colour a pixel should have been.

    It is only a seam measurement for a document that is *supposed* to cover its
    canvas, which a trace of a quantised image is by construction — every pixel
    of the source carries a label, so every pixel of the output belongs to some
    layer. Point it at ordinary artwork and it will faithfully report the empty
    space around the design.

    ``margin`` drops a frame that many pixels wide, because the outermost row of
    the canvas is half-covered by the edge of the artwork in every correctly
    drawn document. That is the edge of the page, not a seam, and counting it
    would put a floor under the metric that no fix could ever reach.

    ``ignore`` drops pixels that are bare **on purpose** — B8's dropped
    background, one flag per pixel of ``raster`` in row-major order. They leave
    the denominator as well as the count, because a document that is one third
    deliberate fabric has not got a third less seam to show: the question this
    answers is *did the layers that are there join up*, and it can only be asked
    about the part of the canvas somebody meant to stitch. Without it, dropping
    a background reads as the largest gap the instrument has ever seen and the
    column stops meaning anything.
    """
    width, height = raster.width, raster.height
    inner_width = width - 2 * margin
    inner_height = height - 2 * margin
    if inner_width < 1 or inner_height < 1:  # too small to have an inside
        return ShowThrough(pixels=0, total_pixels=0, missing=0.0, worst=255)
    if ignore is not None and len(ignore) != width * height:
        raise ValueError(
            f"ignore covers {len(ignore)} pixels, but the image has {width * height}"
        )

    pixels = raster.pixels
    count = 0
    missing = 0
    worst = 255
    total = 0
    for y in range(margin, height - margin):
        row = y * width
        for position in range(row + margin, row + width - margin):
            if ignore is not None and ignore[position]:
                continue
            total += 1
            alpha = pixels[position * 4 + 3]
            if alpha < 255:
                count += 1
                missing += 255 - alpha
                if alpha < worst:
                    worst = alpha
    return ShowThrough(
        pixels=count,
        total_pixels=total,
        missing=missing / 255.0,
        worst=worst,
    )


# ------------------------------------------------------------- renderers


@dataclass(frozen=True)
class Renderer:
    """One way of turning SVG text into PNG bytes."""

    name: str
    #: Given (input path, output path, width) produce a command line.
    command: Optional[Callable[[Path, Path, int], Sequence[str]]] = None
    #: Alternative for in-process renderers.
    call: Optional[Callable[[str, int], bytes]] = None

    def render(self, source: str, width: int = DEFAULT_WIDTH) -> bytes:
        if self.call is not None:
            try:
                return self.call(source, width)
            except Exception as exc:  # noqa: BLE001 - third-party failure modes vary
                raise RenderError(f"{self.name} failed: {exc}") from exc

        assert self.command is not None
        with tempfile.TemporaryDirectory() as tmp:
            svg_path = Path(tmp) / "in.svg"
            png_path = Path(tmp) / "out.png"
            svg_path.write_text(source, encoding="utf-8")
            try:
                subprocess.run(
                    self.command(svg_path, png_path, width),
                    check=True,
                    capture_output=True,
                    timeout=60,
                )
            except subprocess.CalledProcessError as exc:
                detail = exc.stderr.decode("utf-8", "replace").strip()[:200]
                raise RenderError(f"{self.name} failed: {detail}") from exc
            except (OSError, subprocess.SubprocessError) as exc:
                raise RenderError(f"{self.name} failed: {exc}") from exc
            if not png_path.exists():
                raise RenderError(f"{self.name} produced no output")
            return png_path.read_bytes()


def _cairosvg_render(source: str, width: int) -> bytes:
    import cairosvg  # imported lazily: optional dependency

    return cairosvg.svg2png(bytestring=source.encode("utf-8"), output_width=width)


#: Candidates in preference order. Fast and faithful first.
_CANDIDATES: Tuple[Tuple[str, Renderer], ...] = (
    (
        "rsvg-convert",
        Renderer(
            name="rsvg-convert",
            command=lambda src, out, w: ["rsvg-convert", "-w", str(w), "-o", str(out), str(src)],
        ),
    ),
    (
        "resvg",
        Renderer(
            name="resvg",
            command=lambda src, out, w: ["resvg", "--width", str(w), str(src), str(out)],
        ),
    ),
    (
        "inkscape",
        Renderer(
            name="inkscape",
            command=lambda src, out, w: [
                "inkscape",
                str(src),
                "--export-type=png",
                f"--export-width={w}",
                f"--export-filename={out}",
            ],
        ),
    ),
)


#: Set this to pretend nothing is installed — useful in CI to prove that the
#: degraded path really works, rather than assuming it does.
DISABLE_ENV_VAR = "SVGEMB_NO_RENDERER"


def renderers_disabled() -> bool:
    return os.environ.get(DISABLE_ENV_VAR, "").strip() not in ("", "0", "false", "no")


def available_renderers() -> List[Renderer]:
    """Every renderer installed on this machine, best first."""
    if renderers_disabled():
        return []
    found = [renderer for executable, renderer in _CANDIDATES if shutil.which(executable)]
    try:
        import cairosvg  # noqa: F401
    except ImportError:
        pass
    else:
        found.append(Renderer(name="cairosvg", call=_cairosvg_render))
    return found


def default_renderer() -> Optional[Renderer]:
    """The renderer to use, or ``None`` when the machine has none."""
    renderers = available_renderers()
    return renderers[0] if renderers else None


def render(source: str, width: int = DEFAULT_WIDTH, renderer: Optional[Renderer] = None) -> Optional[Raster]:
    """Render SVG text, or ``None`` when nothing is installed to do it."""
    renderer = renderer or default_renderer()
    if renderer is None:
        return None
    return decode_png(renderer.render(source, width))


def visual_difference(
    before: str,
    after: str,
    width: int = DEFAULT_WIDTH,
    renderer: Optional[Renderer] = None,
    tolerance: int = DEFAULT_TOLERANCE,
) -> Optional[Difference]:
    """How much two SVG documents differ visually.

    Returns ``None`` when no renderer is installed. Both sides always go
    through the *same* renderer — mixing two would compare their antialiasing,
    not the documents.
    """
    renderer = renderer or default_renderer()
    if renderer is None:
        return None
    left = decode_png(renderer.render(before, width))
    right = decode_png(renderer.render(after, width))
    return compare_rasters(left, right, tolerance=tolerance)
