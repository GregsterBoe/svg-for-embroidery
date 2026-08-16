"""B1: the raster layer — loading images and measuring what is in them.

Phase B turns pictures into embroidery-ready SVG, and the roadmap's judgement is
that *quality comes from preprocessing, not from the tracer*. That only pays off
if the preprocessing can be measured, so measurement is built first and the
tracer is fitted to it — not the other way round.

Everything here answers questions about a bitmap that predict how it will
embroider:

* how many colours are in it, and how much is lost by reducing them to the
  profile's limit
* how much of it is flat, and how much is shading a needle cannot follow
* how much boundary a tracer would have to chase — which is path count
* how much of the artwork is finer than the thread can render

The same split as :mod:`svg_embroidery.visual` and :mod:`svg_embroidery.geometry`:

**Measuring is pure Python and always available for PNG.** The decoder in
:mod:`svg_embroidery.visual` already exists for the visual harness, so a PNG can
be measured on a phone with nothing installed.

**Other formats need Pillow.** JPEG decoding is not something to hand-roll.
Without it, a JPEG is a measurement you don't get: :func:`load_image` raises a
message naming the fix, and the bench harness records the row as unmeasured
rather than failing the run.
"""

from __future__ import annotations

import os
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .visual import Raster, decode_png

RGB = Tuple[int, int, int]

#: Longest side the metrics run at. Every measurement below is O(pixels) in pure
#: Python, and the properties they describe — flatness, edge density, colour
#: spread — survive a box filter. 128 keeps a 20-image sweep in a few seconds.
WORK_SIDE = 128

#: Formats the built-in decoder handles with no dependencies at all.
NATIVE_SUFFIXES = (".png",)

#: Formats worth trying through Pillow when it is installed.
PILLOW_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff")

#: Set this to pretend Pillow is not installed, so CI can prove the degraded
#: path works rather than assuming it does.
DISABLE_ENV_VAR = "SVGEMB_NO_RASTER"


class RasterError(Exception):
    """Raised when an image cannot be read on this machine."""


def pillow_disabled() -> bool:
    return os.environ.get(DISABLE_ENV_VAR, "").strip() not in ("", "0", "false", "no")


def pillow_available() -> bool:
    """True when Pillow can be used for the formats the core cannot decode."""
    if pillow_disabled():
        return False
    try:
        import PIL.Image  # noqa: F401
    except ImportError:
        return False
    return True


def available_readers() -> List[str]:
    """Which image readers this machine has, best first.

    Named the way :func:`svg_embroidery.visual.available_renderers` is, so
    ``svgemb doctor`` can ask the code that does the work instead of guessing.
    """
    readers = []
    if pillow_available():
        readers.append("Pillow")
    readers.append("built-in PNG")  # always there; visual.py needs it anyway
    return readers


def readable_suffixes() -> Tuple[str, ...]:
    """Every extension that can actually be loaded right now."""
    if pillow_available():
        return tuple(sorted(set(NATIVE_SUFFIXES) | set(PILLOW_SUFFIXES)))
    return NATIVE_SUFFIXES


# ------------------------------------------------------------------ loading


def load_image(path: Path) -> Raster:
    """Read an image file into RGBA, or raise :class:`RasterError`.

    PNG goes through the built-in decoder even when Pillow is present, so the
    numbers a phone reports are the numbers a desktop reports.
    """
    path = Path(path)
    if not path.is_file():
        raise RasterError(f"no such image: {path}")
    suffix = path.suffix.lower()

    if suffix in NATIVE_SUFFIXES:
        try:
            return decode_png(path.read_bytes())
        except Exception as exc:  # noqa: BLE001 - report the file, not the traceback
            if not pillow_available():
                raise RasterError(f"{path.name}: {exc}") from exc
            return _load_with_pillow(path)  # odd PNG (16-bit, interlaced): let Pillow try

    if not pillow_available():
        raise RasterError(
            f"{path.name}: reading '{suffix}' needs Pillow "
            f'(pip install "svg-for-embroidery[convert]"); '
            "PNG works with no dependencies at all"
        )
    return _load_with_pillow(path)


def _load_with_pillow(path: Path) -> Raster:
    from PIL import Image  # imported lazily: optional dependency

    try:
        with Image.open(path) as image:
            rgba = image.convert("RGBA")
            return Raster(width=rgba.width, height=rgba.height, pixels=rgba.tobytes())
    except Exception as exc:  # noqa: BLE001 - Pillow's failure modes vary by format
        raise RasterError(f"{path.name}: {exc}") from exc


def encode_png(raster: Raster) -> bytes:
    """Write RGBA back out as a PNG.

    The counterpart to the decoder in :mod:`svg_embroidery.visual`, needed to
    build corpus fixtures reproducibly and to dump a mask when a stage of the
    pipeline misbehaves. Deliberately unfiltered: byte-identical everywhere,
    which matters more here than file size.
    """
    stride = raster.width * 4
    raw = bytearray()
    for row in range(raster.height):
        raw.append(0)  # filter type 0 (None)
        raw += raster.pixels[row * stride : (row + 1) * stride]

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body))
            + kind
            + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", raster.width, raster.height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


# --------------------------------------------------------------- reshaping


def rgb_pixels(raster: Raster, background: RGB = (255, 255, 255)) -> List[RGB]:
    """Flatten onto a background and return one ``(r, g, b)`` per pixel."""
    flat = raster.composite_over(background)
    return [
        (flat[index], flat[index + 1], flat[index + 2]) for index in range(0, len(flat), 3)
    ]


def has_alpha(raster: Raster) -> bool:
    """True when any pixel is not fully opaque."""
    return any(raster.pixels[index] != 255 for index in range(3, len(raster.pixels), 4))


def downsample(raster: Raster, max_side: int = WORK_SIDE) -> Raster:
    """Box-filter down so the longest side is at most ``max_side``.

    Averaging rather than nearest-neighbour: dropping pixels would erase the
    thin details these metrics exist to notice, and report a cleaner image than
    the one that was handed in.
    """
    longest = max(raster.width, raster.height)
    if longest <= max_side:
        return raster

    scale = longest / max_side
    width = max(1, int(raster.width / scale))
    height = max(1, int(raster.height / scale))
    out = bytearray(width * height * 4)
    source = raster.pixels

    for y in range(height):
        y0 = (y * raster.height) // height
        y1 = max(y0 + 1, ((y + 1) * raster.height) // height)
        for x in range(width):
            x0 = (x * raster.width) // width
            x1 = max(x0 + 1, ((x + 1) * raster.width) // width)
            totals = [0, 0, 0, 0]
            count = 0
            for sy in range(y0, y1):
                row = sy * raster.width
                for sx in range(x0, x1):
                    index = (row + sx) * 4
                    totals[0] += source[index]
                    totals[1] += source[index + 1]
                    totals[2] += source[index + 2]
                    totals[3] += source[index + 3]
                    count += 1
            target = (y * width + x) * 4
            for channel in range(4):
                out[target + channel] = totals[channel] // count
    return Raster(width=width, height=height, pixels=bytes(out))


# ------------------------------------------------------------ quantisation


@dataclass(frozen=True)
class Quantisation:
    """An image reduced to ``k`` colours."""

    palette: List[RGB]
    #: One palette index per pixel, row-major.
    indices: List[int]
    width: int
    height: int

    @property
    def colors(self) -> int:
        return len(self.palette)

    def raster(self) -> Raster:
        """The quantised image, so it can be compared against the source."""
        out = bytearray(self.width * self.height * 4)
        for position, index in enumerate(self.indices):
            red, green, blue = self.palette[index]
            target = position * 4
            out[target] = red
            out[target + 1] = green
            out[target + 2] = blue
            out[target + 3] = 255
        return Raster(width=self.width, height=self.height, pixels=bytes(out))

    def area(self, index: int) -> float:
        """Fraction of the image painted in one palette entry."""
        total = len(self.indices)
        return self.indices.count(index) / total if total else 0.0


#: A cluster whose single most common colour holds at least this much of its
#: weight is represented by that colour exactly, rather than by its mean. See
#: :func:`_representative` for why.
SNAP_SHARE = 0.5

#: Lloyd refinement passes after the median cut. Three is enough to fix the
#: usual median-cut failure — a dominant colour split down the middle while two
#: rarer ones share an entry — and cheap, because it runs over the histogram
#: rather than over pixels.
LLOYD_PASSES = 3


def _widest_channel(box: Sequence[Tuple[RGB, int]]) -> int:
    spans = []
    for channel in range(3):
        values = [color[channel] for color, _ in box]
        spans.append(max(values) - min(values))
    return spans.index(max(spans))


def _average(box: Sequence[Tuple[RGB, int]]) -> RGB:
    weight = sum(count for _, count in box)
    if not weight:
        return (0, 0, 0)
    return tuple(  # type: ignore[return-value]
        sum(color[channel] * count for color, count in box) // weight for channel in range(3)
    )


def _representative(box: Sequence[Tuple[RGB, int]]) -> RGB:
    """The colour that stands for a cluster.

    The mean is the textbook answer and it is wrong for the artwork this
    project exists to handle. A flat logo downsamples to one dominant red plus
    a rim of antialiased pixels between it and the background; averaging those
    together moves the entry a few units off the brand colour, and *every*
    pixel of the logo then counts as changed. Embroidery wants the colour the
    designer chose, not a colour near it.

    So: when one colour owns most of the cluster, keep it verbatim. Otherwise —
    a photograph, where no single value dominates — the mean is right after all.
    """
    weight = sum(count for _, count in box)
    if not weight:
        return (0, 0, 0)
    color, count = max(box, key=lambda item: item[1])
    if count >= weight * SNAP_SHARE:
        return color
    return _average(box)


def _spread(box: Sequence[Tuple[RGB, int]]) -> float:
    """Weighted squared deviation inside a box — how much error it still holds.

    Splitting the box with the widest *span* chases outliers: one stray pixel
    stretches a box without there being much error in it. Splitting the box
    with the most error puts the next palette entry where it pays.
    """
    weight = sum(count for _, count in box)
    if weight <= 1:
        return 0.0
    mean = _average(box)
    return sum(
        count * sum((color[channel] - mean[channel]) ** 2 for channel in range(3))
        for color, count in box
    )


def _nearest(palette: Sequence[RGB], color: RGB) -> int:
    return min(
        range(len(palette)),
        key=lambda i: (
            (palette[i][0] - color[0]) ** 2
            + (palette[i][1] - color[1]) ** 2
            + (palette[i][2] - color[2]) ** 2
        ),
    )


def _split(box: List[Tuple[RGB, int]]) -> Optional[Tuple[List, List]]:
    """Cut a box at the weighted median of its widest channel."""
    if len(box) < 2:
        return None
    channel = _widest_channel(box)
    box.sort(key=lambda item: item[0][channel])
    half = sum(count for _, count in box) / 2
    running = 0
    cut = 0
    for position, (_, count) in enumerate(box):
        running += count
        if running >= half:
            cut = position + 1
            break
    cut = min(max(cut, 1), len(box) - 1)
    return box[:cut], box[cut:]


def quantise(raster: Raster, k: int) -> Quantisation:
    """Reduce to at most ``k`` colours: median cut, then Lloyd refinement.

    Median cut in RGB, not Lab: this is the *measuring* stack, and it has to run
    with nothing installed. B3 replaces the quantiser used by the conversion
    pipeline with a Lab one, and the bench baseline is what will show whether
    that was worth it.

    An image with no more than ``k`` colours already comes back untouched, and
    flat artwork with a few antialiased edges comes back with its own colours
    unmoved — see :func:`_representative` — so a logo scores a loss of zero
    rather than a rounding error that would swamp every later comparison.
    """
    if k < 1:
        raise ValueError("k must be at least 1")
    pixels = rgb_pixels(raster)
    histogram: Dict[RGB, int] = {}
    for color in pixels:
        histogram[color] = histogram.get(color, 0) + 1

    if len(histogram) <= k:
        palette = sorted(histogram)
    else:
        boxes: List[List[Tuple[RGB, int]]] = [list(histogram.items())]
        while len(boxes) < k:
            candidates = [box for box in boxes if len(box) > 1]
            if not candidates:
                break
            target = max(candidates, key=_spread)
            parts = _split(target)
            if parts is None:
                break
            boxes.remove(target)
            boxes.extend(parts)
        palette = [_representative(box) for box in boxes]

        # Median cut cuts at a weighted median, which lands *inside* a dominant
        # colour whenever one holds more than half the image — leaving two
        # rarer colours to share an entry. Lloyd fixes that: reassign, recentre,
        # repeat. Over the histogram, so the cost is in unique colours.
        for _ in range(LLOYD_PASSES):
            clusters: List[List[Tuple[RGB, int]]] = [[] for _ in palette]
            for color, count in histogram.items():
                clusters[_nearest(palette, color)].append((color, count))
            moved = [
                _representative(cluster) if cluster else palette[index]
                for index, cluster in enumerate(clusters)
            ]
            if moved == palette:
                break
            palette = moved

    lookup: Dict[RGB, int] = {}
    indices = []
    for color in pixels:
        index = lookup.get(color)
        if index is None:
            index = _nearest(palette, color)
            lookup[color] = index
        indices.append(index)

    return Quantisation(
        palette=list(palette), indices=indices, width=raster.width, height=raster.height
    )


# ----------------------------------------------------------------- metrics


def unique_colors(raster: Raster) -> int:
    """Distinct RGB values, once transparency is flattened onto white."""
    return len(set(rgb_pixels(raster)))


def flat_ratio(raster: Raster) -> float:
    """Fraction of pixels identical to all four of their neighbours.

    The cleanest separator in the whole set. Flat vector-like artwork scores
    near 1 — every pixel matches its neighbours except along the few edges.
    A photograph scores near 0, because shading means no two adjacent pixels
    are ever quite the same. It says *is this drawable at all* before any
    judgement about how many colours to keep.
    """
    width, height = raster.width, raster.height
    if width < 3 or height < 3:
        return 1.0
    pixels = rgb_pixels(raster)
    flat = 0
    for y in range(1, height - 1):
        row = y * width
        for x in range(1, width - 1):
            here = pixels[row + x]
            if (
                pixels[row + x - 1] == here
                and pixels[row + x + 1] == here
                and pixels[row - width + x] == here
                and pixels[row + width + x] == here
            ):
                flat += 1
    return flat / ((width - 2) * (height - 2))


def edge_density(quantisation: Quantisation) -> float:
    """Fraction of pixels sitting on a colour boundary.

    A stand-in for path count long before there is a tracer: every boundary
    pixel is contour the tracer has to follow. When B4 lands, the two columns
    sit side by side in the bench table and the correlation is checkable rather
    than assumed.
    """
    width, height = quantisation.width, quantisation.height
    if width < 2 or height < 2:
        return 0.0
    indices = quantisation.indices
    edges = 0
    for y in range(height - 1):
        row = y * width
        for x in range(width - 1):
            here = indices[row + x]
            if here != indices[row + x + 1] or here != indices[row + width + x]:
                edges += 1
    return edges / ((width - 1) * (height - 1))


def _erode(mask: List[bool], width: int, height: int, radius: int) -> List[bool]:
    """Square-kernel erosion, done separably: rows first, then columns."""
    horizontal = [False] * (width * height)
    for y in range(height):
        row = y * width
        for x in range(width):
            lo, hi = max(0, x - radius), min(width - 1, x + radius)
            if x - radius < 0 or x + radius > width - 1:
                continue  # the border cannot be interior to the kernel
            horizontal[row + x] = all(mask[row + i] for i in range(lo, hi + 1))

    out = [False] * (width * height)
    for y in range(height):
        if y - radius < 0 or y + radius > height - 1:
            continue
        row = y * width
        for x in range(width):
            out[row + x] = all(
                horizontal[(y + offset) * width + x] for offset in range(-radius, radius + 1)
            )
    return out


def _dilate(mask: List[bool], width: int, height: int, radius: int) -> List[bool]:
    """Square-kernel dilation, the inverse pass of :func:`_erode`."""
    horizontal = [False] * (width * height)
    for y in range(height):
        row = y * width
        for x in range(width):
            lo, hi = max(0, x - radius), min(width - 1, x + radius)
            horizontal[row + x] = any(mask[row + i] for i in range(lo, hi + 1))

    out = [False] * (width * height)
    for y in range(height):
        row = y * width
        lo, hi = max(0, y - radius), min(height - 1, y + radius)
        for x in range(width):
            out[row + x] = any(horizontal[i * width + x] for i in range(lo, hi + 1))
    return out


def thin_ratio(quantisation: Quantisation, radius: int) -> float:
    """Fraction of the image in features narrower than ``2 * radius + 1`` px.

    A morphological opening per colour: erode, then dilate. Anything that does
    not survive was thinner than the kernel, which is exactly the detail a
    needle cannot render — the same test :mod:`svg_embroidery.geometry` applies
    to vector shapes in A5, done on pixels because there are no shapes yet.

    ``radius`` comes from the target profile, not from the image: it is the
    profile's minimum stitchable width converted into working pixels, so the
    answer means "too fine *for this shop*".
    """
    if radius < 1:
        return 0.0
    width, height, indices = quantisation.width, quantisation.height, quantisation.indices
    if width <= 2 * radius or height <= 2 * radius:
        return 1.0  # the whole image is finer than the kernel

    survived = [False] * len(indices)
    for index in range(len(quantisation.palette)):
        mask = [value == index for value in indices]
        if not any(mask):
            continue
        opened = _dilate(_erode(mask, width, height, radius), width, height, radius)
        for position, keep in enumerate(opened):
            if keep and mask[position]:
                survived[position] = True
    return 1.0 - (sum(survived) / len(indices))
