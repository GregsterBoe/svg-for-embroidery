#!/usr/bin/env python3
"""Build the B1 benchmark corpus.

Twenty-one images spanning the range the roadmap names — flat logos, line art,
scanned drawings, photos with backgrounds, transparent PNGs, gradients and
low-resolution junk — generated rather than collected, for three reasons:

* **Reproducible.** A fixed LCG and an unfiltered PNG writer mean every machine
  produces byte-identical files, so a metric that moves is the code moving and
  not the corpus.
* **Reviewable.** What makes ``line-art-thin`` thin is a number in this file,
  not a property of a JPEG somebody found. When a metric disagrees with
  intuition, the fixture can be read.
* **Licence-free.** No question about whose photograph is in the repository.

The cost is honest and worth stating: generated images are cleaner than real
ones. This corpus proves the metrics *discriminate*; it does not prove they
match human judgement on real scans. Adding real images later is additive —
drop them in the directory and list them in the manifest.

Usage::

    python bench/make_corpus.py            # write any missing images
    python bench/make_corpus.py --force    # rewrite them all

Only the JPEG fixture needs Pillow; every other image is written by the
dependency-free encoder in ``svg_embroidery.raster``.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Callable, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from svg_embroidery.raster import encode_png  # noqa: E402
from svg_embroidery.visual import Raster  # noqa: E402

HERE = Path(__file__).resolve().parent
CORPUS = HERE / "corpus"
SIZE = 256

RGBA = Tuple[int, int, int, int]

WHITE: RGBA = (255, 255, 255, 255)
BLACK: RGBA = (26, 26, 26, 255)
RED: RGBA = (200, 16, 46, 255)
BLUE: RGBA = (32, 74, 160, 255)
GOLD: RGBA = (232, 178, 40, 255)
GREEN: RGBA = (30, 132, 73, 255)
CLEAR: RGBA = (0, 0, 0, 0)


class Rand:
    """A tiny LCG, so the corpus does not depend on Python's RNG staying put."""

    def __init__(self, seed: int) -> None:
        self.state = seed & 0xFFFFFFFF

    def next(self) -> int:
        self.state = (1664525 * self.state + 1013904223) & 0xFFFFFFFF
        return self.state

    def below(self, limit: int) -> int:
        return self.next() % limit

    def spread(self, amount: int) -> int:
        """A signed jitter in ``[-amount, amount]``."""
        return self.below(2 * amount + 1) - amount


class Canvas:
    """A mutable RGBA image with just enough drawing to build fixtures."""

    def __init__(self, width: int = SIZE, height: int = SIZE, fill: RGBA = WHITE) -> None:
        self.width = width
        self.height = height
        self.pixels = bytearray(bytes(fill) * (width * height))

    def set(self, x: int, y: int, color: RGBA) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            index = (y * self.width + x) * 4
            self.pixels[index : index + 4] = bytes(color)

    def get(self, x: int, y: int) -> RGBA:
        index = (y * self.width + x) * 4
        return tuple(self.pixels[index : index + 4])  # type: ignore[return-value]

    def each(self, paint: Callable[[int, int], Optional[RGBA]]) -> "Canvas":
        for y in range(self.height):
            for x in range(self.width):
                color = paint(x, y)
                if color is not None:
                    self.set(x, y, color)
        return self

    def rect(self, x0: int, y0: int, x1: int, y1: int, color: RGBA) -> "Canvas":
        for y in range(max(0, y0), min(self.height, y1)):
            for x in range(max(0, x0), min(self.width, x1)):
                self.set(x, y, color)
        return self

    def disc(self, cx: float, cy: float, radius: float, color: RGBA) -> "Canvas":
        for y in range(max(0, int(cy - radius)), min(self.height, int(cy + radius) + 1)):
            for x in range(max(0, int(cx - radius)), min(self.width, int(cx + radius) + 1)):
                if (x - cx) ** 2 + (y - cy) ** 2 <= radius * radius:
                    self.set(x, y, color)
        return self

    def ring(self, cx: float, cy: float, radius: float, width: float, color: RGBA) -> "Canvas":
        inner, outer = radius - width / 2, radius + width / 2
        for y in range(max(0, int(cy - outer)), min(self.height, int(cy + outer) + 1)):
            for x in range(max(0, int(cx - outer)), min(self.width, int(cx + outer) + 1)):
                distance = math.hypot(x - cx, y - cy)
                if inner <= distance <= outer:
                    self.set(x, y, color)
        return self

    def line(self, x0: float, y0: float, x1: float, y1: float, width: float, color: RGBA) -> "Canvas":
        steps = int(max(abs(x1 - x0), abs(y1 - y0)) * 2) + 1
        half = width / 2
        for step in range(steps + 1):
            t = step / steps
            cx, cy = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
            if width <= 1:
                self.set(int(round(cx)), int(round(cy)), color)
            else:
                self.disc(cx, cy, half, color)
        return self

    def polygon(self, points: List[Tuple[float, float]], color: RGBA) -> "Canvas":
        ys = [point[1] for point in points]
        for y in range(max(0, int(min(ys))), min(self.height, int(max(ys)) + 1)):
            crossings = []
            for index in range(len(points)):
                (x0, y0), (x1, y1) = points[index], points[(index + 1) % len(points)]
                if (y0 <= y < y1) or (y1 <= y < y0):
                    crossings.append(x0 + (y - y0) * (x1 - x0) / (y1 - y0))
            crossings.sort()
            for pair in range(0, len(crossings) - 1, 2):
                for x in range(int(crossings[pair]), int(crossings[pair + 1]) + 1):
                    self.set(x, y, color)
        return self

    def noise(self, rand: Rand, amount: int) -> "Canvas":
        for y in range(self.height):
            for x in range(self.width):
                red, green, blue, alpha = self.get(x, y)
                jitter = rand.spread(amount)
                self.set(
                    x, y,
                    (
                        min(255, max(0, red + jitter)),
                        min(255, max(0, green + jitter)),
                        min(255, max(0, blue + jitter)),
                        alpha,
                    ),
                )
        return self

    def rotated(self, degrees: float, fill: RGBA = WHITE) -> "Canvas":
        """Nearest-neighbour rotation about the centre — a crooked scan."""
        out = Canvas(self.width, self.height, fill)
        angle = math.radians(degrees)
        cos, sin = math.cos(angle), math.sin(angle)
        cx, cy = self.width / 2, self.height / 2
        for y in range(self.height):
            for x in range(self.width):
                dx, dy = x - cx, y - cy
                sx = int(cx + dx * cos + dy * sin)
                sy = int(cy - dx * sin + dy * cos)
                if 0 <= sx < self.width and 0 <= sy < self.height:
                    out.set(x, y, self.get(sx, sy))
        return out

    def raster(self) -> Raster:
        return Raster(width=self.width, height=self.height, pixels=bytes(self.pixels))


# ------------------------------------------------------------------ fixtures


def logo_two_colour() -> Canvas:
    canvas = Canvas()
    canvas.disc(128, 128, 92, BLACK)
    canvas.polygon([(128, 44), (210, 190), (46, 190)], RED)
    return canvas


def logo_three_colour() -> Canvas:
    # Blue to the edge on purpose: a white margin would make this a *four*
    # colour image aimed at a three-colour profile, which is what
    # logo-five-colour is for. The fixture has to be what its name claims.
    canvas = Canvas(fill=BLUE)
    canvas.disc(128, 118, 70, GOLD)
    canvas.polygon([(128, 66), (180, 180), (76, 180)], BLACK)
    return canvas


def logo_five_colour() -> Canvas:
    canvas = Canvas()
    canvas.rect(20, 20, 236, 236, BLUE)
    canvas.rect(48, 48, 208, 140, GOLD)
    canvas.disc(128, 150, 58, RED)
    canvas.ring(128, 150, 40, 12, GREEN)
    canvas.polygon([(128, 36), (150, 92), (106, 92)], BLACK)
    return canvas


def monogram() -> Canvas:
    canvas = Canvas()
    canvas.rect(70, 56, 96, 200, BLACK)          # left stem
    canvas.rect(160, 56, 186, 200, BLACK)        # right stem
    canvas.polygon([(96, 56), (160, 168), (160, 200), (96, 88)], BLACK)
    return canvas


def line_art(width: float, name_seed: int) -> Canvas:
    canvas = Canvas()
    canvas.ring(128, 128, 88, width, BLACK)
    canvas.line(40, 128, 216, 128, width, BLACK)
    canvas.line(128, 40, 128, 216, width, BLACK)
    canvas.line(66, 66, 190, 190, width, BLACK)
    canvas.line(190, 66, 66, 190, width, BLACK)
    canvas.ring(128, 128, 44, width, BLACK)
    return canvas


def hatching() -> Canvas:
    canvas = Canvas()
    for offset in range(-SIZE, SIZE * 2, 4):
        canvas.line(offset, 0, offset + SIZE, SIZE, 1, BLACK)
    canvas.disc(128, 128, 60, WHITE)
    for offset in range(-SIZE, SIZE * 2, 6):
        canvas.line(offset + SIZE, 0, offset, SIZE, 1, RED)
    return canvas


def scan(noise_amount: int, skew: float = 0.0) -> Canvas:
    canvas = Canvas(fill=(250, 247, 240, 255))
    canvas.ring(120, 120, 74, 6, (40, 38, 44, 255))
    canvas.line(70, 170, 170, 70, 5, (40, 38, 44, 255))
    canvas.polygon([(120, 62), (168, 150), (72, 150)], (40, 38, 44, 255))
    if skew:
        canvas = canvas.rotated(skew, fill=(250, 247, 240, 255))
    canvas.noise(Rand(7 + noise_amount), noise_amount)
    return canvas


def photo_portrait() -> Canvas:
    rand = Rand(1234)
    canvas = Canvas()

    def paint(x: int, y: int) -> RGBA:
        distance = math.hypot(x - 118, y - 100) / 150
        shade = max(0.0, 1.0 - distance * distance)
        base = (
            int(60 + 150 * shade),
            int(48 + 120 * shade),
            int(44 + 100 * shade),
        )
        grain = rand.spread(6)
        return (
            min(255, max(0, base[0] + grain)),
            min(255, max(0, base[1] + grain)),
            min(255, max(0, base[2] + grain)),
            255,
        )

    return canvas.each(paint)


def photo_landscape() -> Canvas:
    rand = Rand(99)
    canvas = Canvas()

    def paint(x: int, y: int) -> RGBA:
        if y < 150:
            t = y / 150
            base = (int(120 + 100 * t), int(160 + 70 * t), int(220 - 20 * t))
            grain = rand.spread(3)
        else:
            t = (y - 150) / 106
            base = (int(90 - 30 * t), int(110 - 40 * t), int(60 - 20 * t))
            grain = rand.spread(14)
        return (
            min(255, max(0, base[0] + grain)),
            min(255, max(0, base[1] + grain)),
            min(255, max(0, base[2] + grain)),
            255,
        )

    return canvas.each(paint)


def photo_busy() -> Canvas:
    rand = Rand(4242)
    canvas = Canvas()

    def paint(x: int, y: int) -> RGBA:
        swirl = math.sin(x / 9.0) * math.cos(y / 7.0)
        base = int(128 + 90 * swirl)
        return (
            min(255, max(0, base + rand.spread(30))),
            min(255, max(0, 255 - base + rand.spread(30))),
            min(255, max(0, (base * 2) % 255 + rand.spread(30))),
            255,
        )

    return canvas.each(paint)


def alpha_logo() -> Canvas:
    canvas = Canvas(fill=CLEAR)
    canvas.disc(128, 128, 90, RED)
    canvas.polygon([(128, 60), (188, 180), (68, 180)], BLACK)
    return canvas


def alpha_soft() -> Canvas:
    canvas = Canvas(fill=CLEAR)

    def paint(x: int, y: int) -> Optional[RGBA]:
        distance = math.hypot(x - 128, y - 128)
        if distance > 100:
            return None
        alpha = 255 if distance < 60 else int(255 * (100 - distance) / 40)
        return (BLUE[0], BLUE[1], BLUE[2], alpha)

    return canvas.each(paint)


def paper_minority() -> Canvas:
    """B8's awkward case: the ground is neither the largest area nor the bottom layer.

    Every other fixture has its paper as the biggest thing in the picture, so
    the background sorts to the bottom of the stitching order and dropping it is
    free — nothing was ever grown underneath it. Here two bands cross and reach
    all four edges, leaving the paper as four corner squares: **blue 45%, paper
    30%, red 25%**, so the paper is stitched second and the red beneath it is
    spread under it by B4's trap.

    That is the one arrangement where dropping a layer can show: the pixels the
    red was grown into are about to become fabric, and a red hairline round the
    hole is what a naive drop leaves behind. The proportions are the fixture —
    keep the paper off both ends of the area order or the image stops testing
    anything.
    """
    canvas = Canvas()
    canvas.rect(0, 70, SIZE, 186, RED)      # a band to both side edges
    canvas.rect(70, 0, 186, SIZE, BLUE)     # and one to the top and bottom
    return canvas


def gradient_linear() -> Canvas:
    def paint(x: int, y: int) -> RGBA:
        t = x / (SIZE - 1)
        return (int(220 * (1 - t) + 20 * t), int(30 * (1 - t) + 90 * t), int(40 * (1 - t) + 190 * t), 255)

    return Canvas().each(paint)


def gradient_radial() -> Canvas:
    def paint(x: int, y: int) -> RGBA:
        t = min(1.0, math.hypot(x - 128, y - 128) / 150)
        return (int(250 - 200 * t), int(220 - 190 * t), int(80 + 100 * t), 255)

    return Canvas().each(paint)


def lowres_icon() -> Canvas:
    canvas = Canvas(32, 32, WHITE)
    canvas.disc(16, 16, 12, BLUE)
    canvas.rect(14, 6, 18, 20, WHITE)
    canvas.rect(14, 23, 18, 27, WHITE)
    return canvas


def antialiased_edges() -> Canvas:
    """A flat logo drawn with heavy antialiasing: few *intended* colours, many real ones."""
    canvas = Canvas()
    samples = 4

    def paint(x: int, y: int) -> RGBA:
        hits = 0
        for sy in range(samples):
            for sx in range(samples):
                px = x + (sx + 0.5) / samples
                py = y + (sy + 0.5) / samples
                if math.hypot(px - 128, py - 128) <= 88:
                    hits += 1
        coverage = hits / (samples * samples)
        return (
            int(255 * (1 - coverage) + RED[0] * coverage),
            int(255 * (1 - coverage) + RED[1] * coverage),
            int(255 * (1 - coverage) + RED[2] * coverage),
            255,
        )

    return canvas.each(paint)


#: name -> (builder, profile, category, expectation, note)
FIXTURES = [
    ("logo-two-colour", logo_two_colour, "embroidery-basic", "logo", "good",
     "two flat colours, crisp edges — the easy case"),
    ("logo-three-colour", logo_three_colour, "embroidery-basic", "logo", "good",
     "exactly the profile's colour budget"),
    ("logo-five-colour", logo_five_colour, "embroidery-basic", "logo", "marginal",
     "five flat colours into a three-colour profile"),
    ("monogram", monogram, "embroidery-basic", "logo", "good",
     "one colour, thick strokes"),
    ("line-art-thick", lambda: line_art(5, 1), "embroidery-basic", "line-art", "good",
     "5px strokes — comfortably stitchable"),
    ("line-art-thin", lambda: line_art(1, 2), "embroidery-basic", "line-art", "marginal",
     "1px strokes — below the needle's minimum"),
    ("hatching", hatching, "embroidery-basic", "line-art", "hopeless",
     "dense crosshatch: almost all of it is too fine"),
    ("scan-clean", lambda: scan(3), "embroidery-basic", "scan", "good",
     "off-white paper, faint grain"),
    ("scan-noisy", lambda: scan(26), "embroidery-basic", "scan", "marginal",
     "heavy speckle over flat artwork"),
    ("scan-skewed", lambda: scan(10, skew=7.0), "embroidery-basic", "scan", "marginal",
     "crooked on the glass, with grain"),
    ("photo-portrait", photo_portrait, "embroidery-basic", "photo", "hopeless",
     "smooth shading, no flat regions at all"),
    ("photo-landscape", photo_landscape, "embroidery-basic", "photo", "hopeless",
     "sky gradient over textured ground"),
    ("photo-busy", photo_busy, "embroidery-basic", "photo", "hopeless",
     "high-frequency colour everywhere"),
    ("paper-minority", paper_minority, "embroidery-basic", "logo", "good",
     "two bands to the edges: the ground is only the four corners"),
    ("alpha-logo", alpha_logo, "embroidery-basic", "alpha", "good",
     "flat colours on a transparent background"),
    ("alpha-soft", alpha_soft, "embroidery-basic", "alpha", "marginal",
     "soft alpha falloff — the background decision matters"),
    ("gradient-linear", gradient_linear, "embroidery-basic", "gradient", "hopeless",
     "a two-colour ramp with nothing flat in it"),
    ("gradient-radial", gradient_radial, "embroidery-basic", "gradient", "hopeless",
     "radial ramp, same problem from the centre out"),
    ("lowres-icon", lowres_icon, "embroidery-basic", "junk", "marginal",
     "32x32: flat, but there is very little of it"),
    ("antialiased-edges", antialiased_edges, "embroidery-basic", "junk", "good",
     "two intended colours, hundreds of real ones along the edge"),
    ("jpeg-blocky", logo_three_colour, "embroidery-basic", "junk", "marginal",
     "a flat logo through heavy JPEG compression"),
]


def write_manifest() -> None:
    lines = [
        "# The B1 benchmark corpus.",
        "#",
        "# Generated by bench/make_corpus.py — edit that, not this. Each entry names",
        "# the profile the image is aimed at, so a metric like 'too thin to stitch'",
        "# means too thin *for that shop* rather than in the abstract.",
        "#",
        "# expect: what a human would say before measuring anything. B2's triage is",
        "# graded against this column; B1 only records it.",
        "",
        "images:",
    ]
    for name, _, profile, category, expect, note in FIXTURES:
        suffix = ".jpg" if name == "jpeg-blocky" else ".png"
        quoted = '"' + note.replace("\\", "\\\\").replace('"', '\\"') + '"'
        lines.extend([
            f"  - name: {name}",
            f"    file: {name}{suffix}",
            f"    profile: {profile}",
            f"    category: {category}",
            f"    expect: {expect}",
            f"    note: {quoted}",
        ])
    (CORPUS / "manifest.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="rewrite images that already exist")
    args = parser.parse_args(argv)

    CORPUS.mkdir(parents=True, exist_ok=True)
    written, skipped = 0, 0
    for name, builder, _, _, _, _ in FIXTURES:
        is_jpeg = name == "jpeg-blocky"
        path = CORPUS / f"{name}{'.jpg' if is_jpeg else '.png'}"
        if path.exists() and not args.force:
            skipped += 1
            continue
        canvas = builder()
        if is_jpeg:
            try:
                from PIL import Image
            except ImportError:
                print(f"skip {path.name}: needs Pillow to encode JPEG", file=sys.stderr)
                continue
            image = Image.frombytes("RGBA", (canvas.width, canvas.height), bytes(canvas.pixels))
            image.convert("RGB").save(path, "JPEG", quality=18)
        else:
            path.write_bytes(encode_png(canvas.raster()))
        written += 1
        print(f"wrote {path.name}")

    write_manifest()
    print(f"{written} written, {skipped} already present; manifest updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
