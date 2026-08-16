"""B3: the preprocessing pipeline — cleaning an image up before it is traced.

The premise of Phase B is that **quality comes from preprocessing, not from the
tracer**, and B0 put a number on it: potrace scores `fit` 0.250 on a clean scan
and vtracer 0.030, not because vtracer traces better but because it smooths the
grain first. This module is the other half of that trade — the smoothing,
without vtracer's habit of deleting thin strokes along with the noise.

Six stages, each usable and testable on its own:

1. :func:`upscale` — give a tiny source enough pixels to trace
2. :func:`flatten_background` — make the paper one exact colour
3. :func:`denoise` — bilateral filter: kill grain, keep edges
4. :func:`normalise_contrast` — only when the image is genuinely flat-toned
5. :func:`quantise_lab` — reduce to the profile's colour budget, in CIE Lab
6. :func:`despeckle` — drop specks too small to be artwork

Stages 1–4 produce a cleaned *image*; 5–6 produce the *labels* a tracer turns
into paths. The split matters, because it is also the split between "measure
this better" and "convert this": B1's metrics on a grainy scan are measuring the
grain, so cleaning is what makes them answer the question they claim to.

**Everything here is pure Python**, the same judgement A1 made about decoding
PNG and A5 made about flattening curves: it is all pixel arithmetic, so a phone
gets the same answers as a desktop and the conversion path does not disappear
behind a compiler.

Three decisions worth keeping, each of which was a wrong answer first:

**The noise estimate must not read artwork as noise.** Sizing the filter from
the *median* difference between neighbouring pixels puts ``hatching`` at 147 —
crosshatch means half of all neighbours straddle a line, so the median lands
mid-edge. At that width the filter blurs across the strokes and turns the
drawing into grey tone: the verdict stayed *hopeless*, but for the wrong reason
and with the artwork destroyed. :func:`noise_sigma` reads a low percentile
instead, which is blind to the edges and sees only the floor, and the result is
capped (:data:`MAX_SIGMA`) so that a filter which *would* blur across a real
edge simply cannot be built.

**Contrast normalisation is off unless it is needed.** Stretching an image that
already spans the full range does nothing useful and clips at both ends, which
manufactures flat pixels: it posterised ``gradient-linear`` from 189 colours to
34 and moved it from *hopeless* to *marginal* — a ramp made to look drawable by
the preprocessing. It now fires only below :data:`LOW_CONTRAST`, which no image
in the corpus trips, and is tested on a purpose-built faded fixture instead.

**Stage 6 despeckles; it does not open.** A morphological opening at the
profile's minimum width is exactly A5's ``geometry.min_feature_size`` fixer,
which that step classified **destructive** and put behind an explicit per-rule
flag. Running it silently here would delete artwork the user never agreed to
lose — measured: it took ``line-art-thin`` from 2.9% ink to 0.1%, which is
vtracer's failure mode reproduced faithfully. Dropping small *connected
regions* removes grain and keeps the stroke, because a hairline that crosses the
whole image is a large region that happens to be thin.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .colors import srgb_to_lab
from .raster import RGB, Quantisation, rgb_pixels
from .visual import Raster

#: Weights for luminance. Noise and contrast are judged on brightness, which is
#: where grain lives and what the eye reads an edge from.
LUMA = (0.2126, 0.7152, 0.0722)

#: Radius of the bilateral kernel, in working pixels. Two is enough to average a
#: dozen samples per pixel, and the cost is quadratic in it.
DENOISE_RADIUS = 2

#: Passes of the filter. One removes most grain; the second cleans up what the
#: first left along edges, where fewer neighbours were available to average.
DENOISE_PASSES = 2

#: Never build a range kernel wider than this. A black-on-white edge is a jump
#: of about 210, so at 40 it keeps a weight of about 1e-6 and survives intact;
#: much above that and the "edge-preserving" filter stops preserving edges.
MAX_SIGMA = 40.0

#: ...and never narrower than this, or the filter does nothing at all on an
#: image whose noise floor rounds to zero.
MIN_SIGMA = 6.0

#: The percentile of neighbour differences taken as the noise floor. Low enough
#: that artwork edges — however many there are — sit above it. See
#: :func:`noise_sigma`.
NOISE_PERCENTILE = 0.25

#: How much wider than the measured floor the range kernel is built. A quarter
#: of neighbouring pairs differ by less than the floor, so the filter has to
#: reach well past it to average the rest of the grain together. Five is the
#: smallest value that cleans the corpus's worst scan — ``scan-noisy`` goes from
#: 0.317 edge density to 0.028 — and larger values buy nothing, so this is the
#: gentlest filter that does the job rather than the strongest one that fits.
NOISE_FACTOR = 5.0

#: Stretch the tones only when the image occupies less than this much of the
#: available range. Above it, normalisation is a no-op that clips.
LOW_CONTRAST = 0.60

#: How close two colours must be to count as the same background, summed over
#: the three channels.
BACKGROUND_TOLERANCE = 36

#: A flood fill that swallows more than this is not finding a background, it is
#: finding the whole picture — so it is discarded.
MAX_BACKGROUND = 0.92

#: Two palette entries closer than this in CIE Lab are the same colour, and are
#: merged rather than kept. A just-noticeable difference is around 1–2 and a
#: thread you could actually buy is much further off than that, so 2.5 only ever
#: catches entries nobody could tell apart. See :func:`quantise_lab`.
MIN_SEPARATION = 2.5


@dataclass(frozen=True)
class Stage:
    """What one stage did, so a conversion can be explained afterwards."""

    name: str
    says: str

    def __str__(self) -> str:
        return f"{self.name}: {self.says}"


@dataclass(frozen=True)
class Recipe:
    """The pipeline's settings, taken from the profile rather than invented.

    The project's ground rule — *the profile is the spec* — applied to pixels:
    the colour budget is the shop's ``color.max_count``, and the speck size
    comes from its ``stroke.min_width`` expressed in working pixels. No second
    source of truth about what this shop can stitch.
    """

    colors: int
    #: Radius of the kernel that asks the profile's minimum-width question, in
    #: working pixels — B1's :func:`~svg_embroidery.bench.scale_for` computes it.
    radius: int = 1
    denoise: bool = True
    background: bool = True
    contrast: bool = True
    #: Pixels below which a connected region is absorbed into its surroundings.
    #: **One means absorb nothing**, and that is the default on purpose — see
    #: :func:`despeckle` for the measurements behind it.
    speck_area: int = 1


@dataclass
class Preprocessed:
    """The cleaned image, the labels to trace, and what happened on the way."""

    #: After stages 1–4: an image, still full colour. What the metrics describe.
    cleaned: Raster
    #: After stages 5–6: the labels a tracer turns into paths.
    quantisation: Quantisation
    stages: List[Stage] = field(default_factory=list)

    def note(self, name: str, says: str) -> None:
        self.stages.append(Stage(name, says))

    def summary(self) -> str:
        return "\n".join(str(stage) for stage in self.stages)


# ------------------------------------------------------------------ helpers


def _luma(color: Sequence[int]) -> float:
    return LUMA[0] * color[0] + LUMA[1] * color[1] + LUMA[2] * color[2]


def _as_raster(pixels: Sequence[RGB], width: int, height: int) -> Raster:
    out = bytearray()
    for red, green, blue in pixels:
        out += bytes((red, green, blue, 255))
    return Raster(width=width, height=height, pixels=bytes(out))


def _percent(value: float) -> str:
    return f"{value * 100:.0f}%"


# ------------------------------------------------------- 1. upscale a tiny source


def upscale(raster: Raster, min_side: int) -> Raster:
    """Repeat pixels until the shorter side reaches ``min_side``.

    Nearest-neighbour on purpose: interpolation would invent colours between the
    ones the artist chose, and every later stage has to reduce colours again. A
    32×32 icon has enough *shape* to trace and not enough *pixels*, and copying
    them changes the second without touching the first.

    This is for the conversion path only. B1 refuses to upsample before
    measuring — the resolution a metric runs at has to be resolution the source
    really has — and this does not change that: it invents no detail, but it
    would let a thinness test claim an answer the source cannot support.
    """
    longest = max(raster.width, raster.height)
    if longest >= min_side:
        return raster
    factor = -(-min_side // max(1, longest))  # ceil
    width, height = raster.width * factor, raster.height * factor
    source = raster.pixels
    out = bytearray(width * height * 4)
    for y in range(height):
        row = (y // factor) * raster.width
        target = y * width * 4
        for x in range(width):
            index = (row + x // factor) * 4
            out[target : target + 4] = source[index : index + 4]
            target += 4
    return Raster(width=width, height=height, pixels=bytes(out))


# --------------------------------------------------- 2. background handling


def flatten_background(
    raster: Raster, tolerance: int = BACKGROUND_TOLERANCE
) -> Tuple[Raster, float]:
    """Find the paper colour from the corners, then make *all* the paper that colour.

    Paper is never one colour: a scanner gives it a gradient and a grain, and
    both survive quantisation as speckle. The corners are the one part of an
    image that is background almost by definition, so a flood fill starts there
    and stops where the colour stops matching.

    **The fill identifies the colour; the snap applies it.** Filling only what
    the corners can reach leaves every enclosed region behind — the paper inside
    a drawn ring is background by any reading, and no path from a corner gets to
    it. On ``scan-clean`` that stranded a quarter of the paper with its grain
    intact, which is enough to keep the image from measuring flat. So the
    connected fill establishes *which* colour the paper is, and then every pixel
    within ``tolerance`` of it is snapped to it wherever it sits.

    Returns the image and the share of it that ended up as background. A fill
    that swallows more than :data:`MAX_BACKGROUND` of the picture has found the
    picture rather than its background, and is discarded rather than applied.
    """
    width, height = raster.width, raster.height
    pixels = rgb_pixels(raster)
    if width < 2 or height < 2:
        return raster, 0.0

    corners = [pixels[0], pixels[width - 1], pixels[(height - 1) * width], pixels[-1]]
    seed = max(set(corners), key=corners.count)

    seen = bytearray(width * height)
    stack = [0, width - 1, (height - 1) * width, width * height - 1]
    filled: List[int] = []
    for start in stack:
        if not seen[start]:
            seen[start] = 1
            filled.append(start)
    while stack:
        index = stack.pop()
        y, x = divmod(index, width)
        for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
            if not (0 <= ny < height and 0 <= nx < width):
                continue
            neighbour = ny * width + nx
            if seen[neighbour]:
                continue
            color = pixels[neighbour]
            if sum(abs(color[c] - seed[c]) for c in range(3)) <= tolerance:
                seen[neighbour] = 1
                stack.append(neighbour)
                filled.append(neighbour)

    if len(filled) / (width * height) > MAX_BACKGROUND:
        return raster, 0.0

    out = list(pixels)
    painted = 0
    for index, color in enumerate(pixels):
        if sum(abs(color[c] - seed[c]) for c in range(3)) <= tolerance:
            out[index] = seed
            painted += 1
    return _as_raster(out, width, height), painted / (width * height)


# ------------------------------------------------------------- 3. denoising


def noise_sigma(raster: Raster) -> float:
    """The grain's floor, in luminance units, measured without reading artwork as grain.

    Every neighbouring pair of pixels differs either because of noise (a little,
    everywhere) or because a line runs between them (a lot, in a few places).
    The obvious estimator is the median difference, and it is wrong for exactly
    the images that need this most: in ``hatching``, half of all neighbours
    straddle a stroke, so the median sits mid-edge and reports 147 on an image
    whose actual noise is zero. Sizing a filter from that blurs the drawing away.

    A low percentile cannot be dragged up that way — however much of the image
    is edges, the quiet pairs are still the quiet ones — so that is what is
    measured. It is a *floor*, not a standard deviation: a quarter of pairs sit
    below it, and :data:`NOISE_FACTOR` is what turns it into a filter width.
    """
    width, height = raster.width, raster.height
    if width < 2:
        return 0.0
    pixels = rgb_pixels(raster)
    lumas = [_luma(color) for color in pixels]
    diffs = []
    for y in range(height):
        row = y * width
        for x in range(width - 1):
            diffs.append(abs(lumas[row + x] - lumas[row + x + 1]))
    if not diffs:
        return 0.0
    diffs.sort()
    return diffs[min(len(diffs) - 1, int(len(diffs) * NOISE_PERCENTILE))]


def denoise(
    raster: Raster,
    sigma: Optional[float] = None,
    radius: int = DENOISE_RADIUS,
    passes: int = DENOISE_PASSES,
) -> Tuple[Raster, float]:
    """Bilateral filter: average neighbours that are near in space *and* colour.

    A plain blur cannot be used here — it removes the grain and the edges
    together, which is the whole problem with converting a scan. Weighting each
    neighbour by how similar it already is means a pixel in the middle of the
    paper averages with the paper around it, while a pixel on the edge of a
    stroke averages only with the rest of that stroke.

    **Applied separably**, rows then columns, which is an approximation — a true
    bilateral does not factor — but a standard and very close one, and it turns
    a 25-tap kernel into two 5-tap ones. Precision here would cost five times
    the runtime to move the third decimal of a metric.

    ``sigma`` defaults to :func:`noise_sigma`, clamped into
    ``[MIN_SIGMA, MAX_SIGMA]``. Returns the image and the sigma used.
    """
    if sigma is None:
        sigma = NOISE_FACTOR * noise_sigma(raster)
    sigma = max(MIN_SIGMA, min(MAX_SIGMA, sigma))

    width, height = raster.width, raster.height
    spatial = [math.exp(-(offset ** 2) / (2 * max(1.0, radius / 1.5) ** 2))
               for offset in range(-radius, radius + 1)]
    # The range term only ever sees an integer difference, so it is a lookup.
    weights = [math.exp(-(delta ** 2) / (2 * sigma ** 2)) for delta in range(256)]

    pixels = [list(color) for color in rgb_pixels(raster)]
    for _ in range(max(1, passes)):
        pixels = _bilateral_pass(pixels, width, height, radius, spatial, weights, along_rows=True)
        pixels = _bilateral_pass(pixels, width, height, radius, spatial, weights, along_rows=False)
    return _as_raster([tuple(color) for color in pixels], width, height), sigma


def _bilateral_pass(pixels, width, height, radius, spatial, weights, along_rows):
    """One direction of the separable filter."""
    lumas = [_luma(color) for color in pixels]
    out = [None] * len(pixels)
    outer, inner = (height, width) if along_rows else (width, height)
    step = 1 if along_rows else width

    for major in range(outer):
        base = major * width if along_rows else major
        for minor in range(inner):
            index = base + minor * step
            here = lumas[index]
            totals = [0.0, 0.0, 0.0]
            total_weight = 0.0
            for offset in range(-radius, radius + 1):
                position = minor + offset
                if position < 0 or position >= inner:
                    continue
                neighbour = base + position * step
                weight = spatial[offset + radius] * weights[int(abs(lumas[neighbour] - here))]
                color = pixels[neighbour]
                totals[0] += color[0] * weight
                totals[1] += color[1] * weight
                totals[2] += color[2] * weight
                total_weight += weight
            out[index] = [
                max(0, min(255, int(value / total_weight + 0.5))) for value in totals
            ]
    return out


# --------------------------------------------------------- 4. contrast


def contrast_span(raster: Raster) -> float:
    """How much of the available brightness range the image actually uses."""
    pixels = rgb_pixels(raster)
    lumas = sorted(_luma(color) for color in pixels)
    if not lumas:
        return 1.0
    low = lumas[int(len(lumas) * 0.02)]
    high = lumas[min(len(lumas) - 1, int(len(lumas) * 0.98))]
    return max(0.0, (high - low) / 255.0)


def normalise_contrast(raster: Raster) -> Tuple[Raster, bool]:
    """Stretch a faded image so its ink is properly dark and its paper properly light.

    **Only when it is needed**, which is the entire lesson of this stage.
    Stretching an image that already spans the range does nothing to the middle
    and clips at both ends, and clipping is how you manufacture flat pixels:
    applied unconditionally it turned ``gradient-linear`` from 189 colours into
    34 and moved a ramp from *hopeless* to *marginal*. Preprocessing that makes
    an unconvertible image look convertible is worse than none.

    Returns the image and whether anything was done to it.
    """
    if contrast_span(raster) >= LOW_CONTRAST:
        return raster, False

    pixels = rgb_pixels(raster)
    lumas = sorted(_luma(color) for color in pixels)
    low = lumas[int(len(lumas) * 0.02)]
    high = lumas[min(len(lumas) - 1, int(len(lumas) * 0.98))]
    if high - low < 1:
        return raster, False

    scale = 255.0 / (high - low)
    out = []
    for color in pixels:
        here = _luma(color)
        wanted = (here - low) * scale
        # Scale the colour towards or away from black by the ratio the luminance
        # moved, so hue survives the stretch instead of drifting per channel.
        ratio = wanted / here if here > 1 else 0.0
        out.append(
            tuple(max(0, min(255, int(channel * ratio + 0.5))) for channel in color)
        )
    return _as_raster(out, raster.width, raster.height), True


# ------------------------------------------------- 5. quantisation in Lab


def quantise_lab(raster: Raster, k: int) -> Quantisation:
    """Reduce to at most ``k`` colours, deciding distance in CIE Lab.

    The RGB quantiser in :mod:`svg_embroidery.raster` is the *measuring* one and
    stays where it is: it has to run with nothing installed, and B1's baseline
    is taken with it. This is the conversion one, and it merges the colours a
    person would merge — RGB distance calls ``#00ff00`` and ``#00cc00`` far
    apart and ``#000080`` and ``#008000`` close, which is backwards.

    Otherwise the same shape as its RGB sibling, for the same reasons: split the
    box holding the most error rather than the widest one, keep a colour that
    dominates its cluster verbatim rather than averaging it into a shade no
    thread matches, and refine with Lloyd afterwards because median cut leaves
    two rare colours sharing an entry whenever one common colour owns half the
    image.
    """
    if k < 1:
        raise ValueError("k must be at least 1")
    pixels = rgb_pixels(raster)
    histogram: Dict[RGB, int] = {}
    for color in pixels:
        histogram[color] = histogram.get(color, 0) + 1

    lab: Dict[RGB, Tuple[float, float, float]] = {
        color: srgb_to_lab("#{:02x}{:02x}{:02x}".format(*color)) for color in histogram
    }

    def mean(box):
        weight = sum(count for _, count in box)
        return [
            sum(lab[color][axis] * count for color, count in box) / weight for axis in range(3)
        ]

    def spread(box):
        weight = sum(count for _, count in box)
        if weight <= 1:
            return 0.0
        centre = mean(box)
        return sum(
            count * sum((lab[color][axis] - centre[axis]) ** 2 for axis in range(3))
            for color, count in box
        )

    def split(box):
        if len(box) < 2:
            return None
        spans = [
            max(lab[color][axis] for color, _ in box) - min(lab[color][axis] for color, _ in box)
            for axis in range(3)
        ]
        axis = spans.index(max(spans))
        box.sort(key=lambda item: lab[item[0]][axis])
        half = sum(count for _, count in box) / 2
        running, cut = 0, 0
        for position, (_, count) in enumerate(box):
            running += count
            if running >= half:
                cut = position + 1
                break
        cut = min(max(cut, 1), len(box) - 1)
        return box[:cut], box[cut:]

    def representative(box):
        weight = sum(count for _, count in box)
        color, count = max(box, key=lambda item: item[1])
        if count >= weight * 0.5:
            return color
        centre = mean(box)
        return min(
            box,
            key=lambda item: sum((lab[item[0]][axis] - centre[axis]) ** 2 for axis in range(3)),
        )[0]

    if len(histogram) <= k:
        palette = sorted(histogram)
    else:
        boxes = [list(histogram.items())]
        while len(boxes) < k:
            candidates = [box for box in boxes if len(box) > 1]
            if not candidates:
                break
            target = max(candidates, key=spread)
            parts = split(target)
            if parts is None:
                break
            boxes.remove(target)
            boxes.extend(parts)
        palette = [representative(box) for box in boxes]

        for _ in range(3):
            clusters: List[List[Tuple[RGB, int]]] = [[] for _ in palette]
            for color, count in histogram.items():
                clusters[_nearest_lab(lab, palette, color)].append((color, count))
            moved = [
                representative(cluster) if cluster else palette[index]
                for index, cluster in enumerate(clusters)
            ]
            if moved == palette:
                break
            palette = moved

    # Outside the branch on purpose: an image that arrives *already* holding two
    # indistinguishable colours has them merged too. Being inside the budget is
    # not a reason to ship a palette with a duplicate in it.
    palette = _merge_indistinguishable(lab, list(palette), histogram)

    lookup: Dict[RGB, int] = {}
    indices = []
    for color in pixels:
        index = lookup.get(color)
        if index is None:
            index = _nearest_lab(lab, palette, color)
            lookup[color] = index
        indices.append(index)

    return Quantisation(
        palette=list(palette), indices=indices, width=raster.width, height=raster.height
    )


def _merge_indistinguishable(lab, palette: List[RGB], histogram: Dict[RGB, int]) -> List[RGB]:
    """Drop palette entries a person could not tell from another one.

    Asking for ``k`` colours does not mean using ``k`` colours. A scan's paper
    is one colour to the eye, and median cut splits it in two anyway when it has
    a spare entry and nowhere better to put it — ``scan-clean`` came out with
    ``#faf7f0`` and ``#f9f6ef``, **0.3 apart in Lab**, which is not two colours.

    The cost is not cosmetic. Two entries that alternate across the same flat
    region put a boundary between them on every other pixel, so the image
    acquires a field of one-pixel slivers: 2% of it measured as *too fine to
    stitch*, an entirely manufactured defect. Merging them is also what a shop
    wants on its own terms — a palette entry is a thread change.
    """
    weight = {color: 0 for color in palette}
    for color, count in histogram.items():
        weight[palette[_nearest_lab(lab, palette, color)]] += count

    merged = True
    while merged and len(palette) > 1:
        merged = False
        for i in range(len(palette)):
            for j in range(i + 1, len(palette)):
                left, right = palette[i], palette[j]
                distance = math.sqrt(
                    sum((lab[left][axis] - lab[right][axis]) ** 2 for axis in range(3))
                )
                if distance >= MIN_SEPARATION:
                    continue
                # The more-used colour survives: it is the one the design is
                # actually painted in, and the other is its rounding error.
                loser = right if weight[left] >= weight[right] else left
                keeper = left if loser is right else right
                weight[keeper] += weight[loser]
                palette = [color for color in palette if color != loser]
                merged = True
                break
            if merged:
                break
    return palette


def _nearest_lab(lab, palette: Sequence[RGB], color: RGB) -> int:
    if color not in lab:
        lab[color] = srgb_to_lab("#{:02x}{:02x}{:02x}".format(*color))
    target = lab[color]
    best, best_distance = 0, None
    for index, entry in enumerate(palette):
        if entry not in lab:
            lab[entry] = srgb_to_lab("#{:02x}{:02x}{:02x}".format(*entry))
        here = lab[entry]
        distance = sum((here[axis] - target[axis]) ** 2 for axis in range(3))
        if best_distance is None or distance < best_distance:
            best, best_distance = index, distance
    return best


# ---------------------------------------------------------- 6. despeckling


def despeckle(quantisation: Quantisation, min_area: int) -> Tuple[Quantisation, float]:
    """Absorb connected regions smaller than ``min_area`` into what surrounds them.

    **Not an opening.** Eroding and re-dilating every colour is A5's
    ``geometry.min_feature_size`` repair, which that step measured, classified
    *destructive*, and put behind a flag naming the rule — because it deletes
    any shape narrower than the needle, and a designer's hairline is a shape.
    Measured here it took ``line-art-thin`` from 2.9% ink to 0.1%: it does not
    clean the image, it erases the drawing.

    What is safe to remove without asking is a region too small to be *anything*
    — a speck of grain that survived the filter, or a couple of pixels the
    quantiser assigned to the wrong entry. A hairline that crosses the whole
    image is thin, but it is not small, so it stays.

    Returns the labels and the share of the image reassigned.
    """
    width, height = quantisation.width, quantisation.height
    indices = list(quantisation.indices)
    if min_area <= 1 or not indices:
        return quantisation, 0.0

    seen = bytearray(width * height)
    moved = 0
    for start in range(width * height):
        if seen[start]:
            continue
        value = indices[start]
        stack = [start]
        seen[start] = 1
        region = []
        neighbours: Dict[int, int] = {}
        while stack:
            index = stack.pop()
            region.append(index)
            y, x = divmod(index, width)
            for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if not (0 <= ny < height and 0 <= nx < width):
                    continue
                neighbour = ny * width + nx
                if indices[neighbour] == value:
                    if not seen[neighbour]:
                        seen[neighbour] = 1
                        stack.append(neighbour)
                else:
                    neighbours[indices[neighbour]] = neighbours.get(indices[neighbour], 0) + 1
        if len(region) < min_area and neighbours:
            # Whichever colour already surrounds it most: the speck disappears
            # into its surroundings rather than into an arbitrary entry.
            winner = max(sorted(neighbours), key=lambda entry: neighbours[entry])
            for index in region:
                indices[index] = winner
            moved += len(region)

    return (
        Quantisation(
            palette=quantisation.palette, indices=indices, width=width, height=height
        ),
        moved / (width * height),
    )


# ------------------------------------------------------------- the pipeline


def clean(raster: Raster, recipe: Recipe) -> Tuple[Raster, List[Stage]]:
    """Stages 2–4: everything that produces an image rather than labels.

    **The grain is measured before anything is done to it.** Flattening the
    background first sets half the pixels to exactly one value, and a noise
    floor read afterwards is then measuring that flat region rather than the
    image: ``scan-noisy`` estimated 37 as it arrived and 0 once its paper had
    been snapped, so the filter sized itself for an image with no grain and left
    the grain alone. Order matters here, and only here.
    """
    stages: List[Stage] = []
    sigma = NOISE_FACTOR * noise_sigma(raster) if recipe.denoise else None

    if recipe.background:
        raster, share = flatten_background(raster)
        stages.append(
            Stage(
                "background",
                f"flattened {_percent(share)} of the image to one colour"
                if share
                else "no background found from the corners, left alone",
            )
        )

    if recipe.denoise:
        raster, used = denoise(raster, sigma=sigma)
        stages.append(Stage("denoise", f"bilateral filter at sigma {used:.0f}"))

    if recipe.contrast:
        span = contrast_span(raster)
        raster, stretched = normalise_contrast(raster)
        stages.append(
            Stage(
                "contrast",
                "stretched the tones out" if stretched
                else f"already spans {_percent(span)} of the range, left alone",
            )
        )

    return raster, stages


def run(raster: Raster, recipe: Recipe) -> Preprocessed:
    """The whole pipeline: an image in, labels ready to trace out."""
    cleaned, stages = clean(raster, recipe)

    quantisation = quantise_lab(cleaned, recipe.colors)
    stages.append(
        Stage("quantise", f"reduced to {quantisation.colors} colour(s) in Lab space")
    )

    if recipe.speck_area > 1:
        quantisation, share = despeckle(quantisation, recipe.speck_area)
        stages.append(
            Stage(
                "despeckle",
                f"absorbed {_percent(share)} of the image — regions under "
                f"{recipe.speck_area} px, too small to be artwork",
            )
        )
    else:
        stages.append(
            Stage("despeckle", "off: the filter left no specks to remove")
        )

    return Preprocessed(cleaned=cleaned, quantisation=quantisation, stages=stages)
