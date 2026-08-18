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

#: How much of the paper the fill found has to land in a *single* palette entry
#: before that entry is accepted as the background (B8). Below it the paper was
#: split across entries, and dropping one would leave the rest stitched.
BACKGROUND_COHERENCE = 0.80

#: ...and how much of that entry has to be paper. Below it the entry is shared
#: with artwork, and dropping it would delete drawing rather than fabric.
BACKGROUND_PURITY = 0.60

#: Two palette entries closer than this in CIE Lab are the same colour, and are
#: merged rather than kept. A just-noticeable difference is around 1–2 and a
#: thread you could actually buy is much further off than that, so 2.5 only ever
#: catches entries nobody could tell apart. See :func:`quantise_lab`.
MIN_SEPARATION = 2.5

#: How far a colour somebody picked may sit from the palette entry it names, in
#: CIE Lab (C1). A colour tapped in the layer panel *is* an entry of the palette
#: it was read off, so it matches at zero — this number is for the other case,
#: where a pick made against one attempt is replayed against the next one and
#: the quantiser has moved its entries in between. Ten is far enough to survive
#: that drift (a just-noticeable difference is 1–2) and near enough that a pick
#: cannot land on a different colour of the artwork, which is the only way this
#: mechanism could delete something nobody chose. See :func:`entry_for_color`.
REMOVAL_TOLERANCE = 10.0


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

    #: The colour budget for the **artwork**. When :attr:`drop_background` is
    #: set the quantiser is asked for one more than this and the extra entry is
    #: the paper, so the shop's threads all go to the drawing — see :func:`run`.
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
    #: B8: identify the paper and hand its palette entry back so the tracer can
    #: leave it unstitched. Off here and on in the conversion path, because the
    #: measuring path must keep grading the image it was handed.
    drop_background: bool = False
    #: B9, experimental: paper the artwork has closed around is design rather
    #: than ground, so give it its own entry and sew it instead of dropping it
    #: with the rest. Off by default — it costs a thread, and B8's behaviour is
    #: the one every measurement in the project was taken against.
    sew_background_holes: bool = False


@dataclass
class Preprocessed:
    """The cleaned image, the labels to trace, and what happened on the way."""

    #: After stages 1–4: an image, still full colour. What the metrics describe.
    cleaned: Raster
    #: After stages 5–6: the labels a tracer turns into paths.
    quantisation: Quantisation
    stages: List[Stage] = field(default_factory=list)
    #: B8: the palette entry that is the paper, or ``None`` when the pipeline was
    #: not asked to find one or could not identify one it was willing to drop.
    #: The pixels keep their label — the tracer is told to leave the layer out,
    #: which is not the same as relabelling them and is why this is an index
    #: rather than a change to :attr:`quantisation`.
    background: Optional[int] = None
    #: B9: the entry holding paper the artwork encloses, split off :attr:`background`
    #: and painted in the same colour, or ``None`` when the pipeline was not asked
    #: to look or the border could reach every paper pixel. It is an ordinary
    #: stitched layer — the field exists so the panel can say *why* a colour it is
    #: about to list is the same one it is leaving to the fabric.
    enclosed: Optional[int] = None

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


def _hex(color: Sequence[int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*color)


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


@dataclass(frozen=True)
class Background:
    """The paper: which colour it is, and exactly which pixels are it.

    The mask is the part B8 needs. Matching a *colour* through the rest of the
    pipeline does not work — denoising and contrast both move it, so the seed
    read here and the palette entry found after quantisation are not the same
    number. Which pixels were paper does not change, so that is what is carried.
    """

    color: RGB
    #: One flag per pixel, row-major, True where this pixel is paper.
    mask: List[bool]
    #: Share of the image the mask covers.
    share: float


def find_background(
    raster: Raster, tolerance: int = BACKGROUND_TOLERANCE
) -> Optional[Background]:
    """Which pixels are the paper, found from the corners.

    Paper is never one colour: a scanner gives it a gradient and a grain, and
    both survive quantisation as speckle. The corners are the one part of an
    image that is background almost by definition, so a flood fill starts there
    and stops where the colour stops matching.

    **The fill identifies the colour; the mask says where it is.** Filling only
    what the corners can reach leaves every enclosed region behind — the paper
    inside a drawn ring is background by any reading, and no path from a corner
    gets to it. On ``scan-clean`` that stranded a quarter of the paper with its
    grain intact, which is enough to keep the image from measuring flat. So the
    connected fill establishes *which* colour the paper is, and then every pixel
    within ``tolerance`` of it counts as paper wherever it sits.

    ``None`` when there is no background to find: a fill that swallows more than
    :data:`MAX_BACKGROUND` of the picture has found the picture rather than its
    background, and an image too small to have corners has neither.
    """
    width, height = raster.width, raster.height
    if width < 2 or height < 2:
        return None
    pixels = rgb_pixels(raster)

    corners = [pixels[0], pixels[width - 1], pixels[(height - 1) * width], pixels[-1]]
    seed = max(set(corners), key=corners.count)

    seen = bytearray(width * height)
    stack = [0, width - 1, (height - 1) * width, width * height - 1]
    filled = 0
    for start in stack:
        if not seen[start]:
            seen[start] = 1
            filled += 1
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
                filled += 1

    if filled / (width * height) > MAX_BACKGROUND:
        return None

    mask = [
        sum(abs(color[c] - seed[c]) for c in range(3)) <= tolerance for color in pixels
    ]
    return Background(color=seed, mask=mask, share=sum(mask) / (width * height))


def flatten_background(
    raster: Raster, tolerance: int = BACKGROUND_TOLERANCE
) -> Tuple[Raster, float]:
    """Make all the paper exactly one colour, so its grain stops being artwork.

    :func:`find_background` decides what the paper is; this paints it. Returns
    the image and the share of it that ended up as background — zero, and the
    image untouched, when there was no background to find.
    """
    found = find_background(raster, tolerance)
    if found is None:
        return raster, 0.0
    pixels = rgb_pixels(raster)
    out = [
        found.color if is_paper else color
        for color, is_paper in zip(pixels, found.mask)
    ]
    return _as_raster(out, raster.width, raster.height), found.share


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


# --------------------------------- 7. paper the artwork encloses (B9)


def enclosed_background(quantisation: Quantisation, background: int) -> List[bool]:
    """Which pixels of the paper entry the image border cannot reach.

    :func:`find_background` establishes the paper's *colour* with a fill from the
    corners and then marks every pixel within tolerance of it **wherever it
    sits** — deliberately, because the paper inside a drawn ring has the same
    grain as the paper outside it and flattening only the reachable half left a
    quarter of ``scan-clean`` speckled. That is the right answer for stage 2,
    where the question is what to smooth.

    It is the wrong answer for B8, where the question is what to *leave to the
    fabric*: a white shirt drawn on white paper is paper by colour and artwork by
    intent, and dropping the whole palette entry cuts a hole through the design.
    The distinction the colour cannot make, connectivity can — **background is
    what the outside of the picture can reach.** So this floods the paper label
    inward from the border and returns what is left over: paper the artwork has
    closed around.

    Four-connected, which is the conservative direction here: a line of artwork
    running diagonally seals the region behind it, so a pocket is called enclosed
    where an eight-connected flood would leak past the corner and call it fabric.
    Sewing something that could have been left bare is a thread; leaving a hole
    where the design had ink is a hole.
    """
    width, height = quantisation.width, quantisation.height
    indices = quantisation.indices
    if width < 1 or height < 1 or not indices:
        return [False] * len(indices)

    reached = bytearray(len(indices))
    stack: List[int] = []
    border = [y * width for y in range(height)]
    border += [y * width + width - 1 for y in range(height)]
    border += list(range(width)) + list(range((height - 1) * width, height * width))
    for position in border:
        if indices[position] == background and not reached[position]:
            reached[position] = 1
            stack.append(position)

    while stack:
        position = stack.pop()
        y, x = divmod(position, width)
        for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
            if not (0 <= ny < height and 0 <= nx < width):
                continue
            neighbour = ny * width + nx
            if reached[neighbour] or indices[neighbour] != background:
                continue
            reached[neighbour] = 1
            stack.append(neighbour)

    return [
        value == background and not reached[position]
        for position, value in enumerate(indices)
    ]


def _regions(mask: Sequence[bool], width: int, height: int) -> List[List[int]]:
    """The mask's separate regions, four-connected, as lists of pixel positions."""
    seen = bytearray(len(mask))
    out: List[List[int]] = []
    for start in range(len(mask)):
        if seen[start] or not mask[start]:
            continue
        stack = [start]
        seen[start] = 1
        region = []
        while stack:
            position = stack.pop()
            region.append(position)
            y, x = divmod(position, width)
            for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if not (0 <= ny < height and 0 <= nx < width):
                    continue
                neighbour = ny * width + nx
                if mask[neighbour] and not seen[neighbour]:
                    seen[neighbour] = 1
                    stack.append(neighbour)
        out.append(region)
    return out


@dataclass(frozen=True)
class Enclosed:
    """What B9 found, and what it did about it."""

    #: The new palette entry, or ``None`` when nothing was left to sew.
    index: Optional[int]
    #: Share of the image that entry covers.
    share: float
    #: Regions the needle can sew, so they became the entry.
    sewn: int
    #: Regions too fine to sew, left to the fabric with the rest of the ground.
    too_fine: int


def sew_enclosed_background(
    quantisation: Quantisation, background: int, radius: int = 1
) -> Tuple[Quantisation, Enclosed]:
    """Give the paper the artwork encloses its own palette entry, so it is sewn.

    Returns the labels and what happened — an :class:`Enclosed` with ``index``
    ``None`` when the border can reach every paper pixel, which is the ordinary
    case and leaves the conversion exactly as B8 made it.

    **Only regions the machine can sew, which is the guard the corpus asked
    for.** Enclosure is a fact about connectivity and it does not know the
    difference between a shape and a gap: measured over the corpus, ``hatching``
    reports **4715 enclosed regions covering 55% of the image**, because on
    crosshatch the paper between every pair of strokes is walled in. Sewing that
    is not sewing the artwork, it is sewing the spaces in it — thousands of
    slivers, a thread spent, and the loop then trying to make them stitchable.
    ``photo-landscape`` says the same thing more quietly at 662.

    So each region is put to the test the shop already applies to everything
    else — *does a disc the width of a stitch fit inside it* — with the
    profile's own ``stroke.min_width`` as the kernel, the same number
    :func:`_extra_entry_earned_its_thread` uses for the same reason. A sliver
    between two strokes vanishes under the erosion and stays fabric, exactly as
    it was before the flag; a white shirt has an inside. No new threshold, and
    nothing for anybody to tune: the ruleset already says how fine is too fine.

    **A new entry in the same colour, rather than a relabelling into whatever
    surrounds it.** Merging the region into its neighbour is the cheaper repair
    and it is a different repair: it repaints the pixels in a colour they never
    had, so a white highlight inside a black outline comes back black. Keeping
    the colour keeps the picture, and the price is stated rather than hidden —
    the paper is a thread now, so the document carries one more colour than B8's
    budget assumed and ``color.max_count`` counts it. The retry loop answers that
    complaint the way it answers every other one, by lowering the ink budget.

    Two entries then hold the same RGB, which is not a problem for anything
    downstream — the tracer works in indices, ``layer_order`` in areas, and the
    checker counts *distinct* colours, so the document is k inks plus paper
    rather than k inks plus paper twice. The one place it is a question is C1,
    where a person names a colour rather than an index: see
    :func:`entry_for_color`, which breaks the resulting tie towards the entry
    that is actually being sewn.

    No *area* threshold on top of that, deliberately. Width is the question a
    needle asks, and B5's cleanup already drops finished subpaths under the
    profile's minimum area — so both halves of "too small to sew" have an answer
    in the ruleset, and neither needs a second one invented here.
    """
    width, height = quantisation.width, quantisation.height
    enclosed = enclosed_background(quantisation, background)
    if not any(enclosed):
        return quantisation, Enclosed(None, 0.0, 0, 0)

    # One erosion over the whole mask rather than one per region: a region is
    # sewable where it still holds a pixel the disc fitted around.
    interior = _eroded(enclosed, width, height, radius)
    sewn: List[int] = []
    too_fine = 0
    for region in _regions(enclosed, width, height):
        if any(interior[position] for position in region):
            sewn += region
        else:
            too_fine += 1

    if not sewn:
        return quantisation, Enclosed(None, 0.0, 0, too_fine)

    index = quantisation.colors
    indices = list(quantisation.indices)
    for position in sewn:
        indices[position] = index
    return (
        Quantisation(
            palette=list(quantisation.palette) + [quantisation.palette[background]],
            indices=indices,
            width=width,
            height=height,
        ),
        Enclosed(
            index=index,
            share=len(sewn) / len(indices),
            sewn=len(_regions(
                [value == index for value in indices], width, height
            )),
            too_fine=too_fine,
        ),
    )


# ------------------------------------------------------------- the pipeline


def clean(
    raster: Raster, recipe: Recipe
) -> Tuple[Raster, List[Stage], Optional[Background]]:
    """Stages 2–4: everything that produces an image rather than labels.

    **The grain is measured before anything is done to it.** Flattening the
    background first sets half the pixels to exactly one value, and a noise
    floor read afterwards is then measuring that flat region rather than the
    image: ``scan-noisy`` estimated 37 as it arrived and 0 once its paper had
    been snapped, so the filter sized itself for an image with no grain and left
    the grain alone. Order matters here, and only here.

    Returns the paper it found alongside the image, because B8 needs to know
    *where* it was and stages 3–4 move the colour it was.
    """
    stages: List[Stage] = []
    sigma = NOISE_FACTOR * noise_sigma(raster) if recipe.denoise else None
    found: Optional[Background] = None

    if recipe.background:
        found = find_background(raster)
        share = found.share if found is not None else 0.0
        if found is not None:
            pixels = rgb_pixels(raster)
            raster = _as_raster(
                [
                    found.color if is_paper else color
                    for color, is_paper in zip(pixels, found.mask)
                ],
                raster.width,
                raster.height,
            )
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

    return raster, stages, found


def background_entry(
    quantisation: Quantisation, found: Background
) -> Optional[int]:
    """Which palette entry is the paper, or ``None`` when none of them safely is.

    Decided on **pixels rather than colour**: the entry holding most of the mask
    that :func:`find_background` marked. Matching the seed colour would be
    matching a number that denoising and contrast have both had a go at since.

    Two guards, and they are the step rather than a detail — this is the one
    place where dropping the wrong entry deletes somebody's drawing:

    - **the paper has to be in one entry.** Below
      :data:`BACKGROUND_COHERENCE` of the mask in a single entry, the quantiser
      split the paper across several and dropping one would leave the rest
      stitched, in a colour chosen to blend with fabric that is no longer there.
    - **that entry has to be mostly paper.** Below :data:`BACKGROUND_PURITY`,
      the entry is shared with artwork of a similar colour, and dropping it
      would take the artwork with it.

    Either way the answer is ``None``, which the caller reads as *convert this
    the way it was converted before B8*. A background that cannot be identified
    safely is not a failure, it is a conversion that keeps its background — the
    project's own rule that a measurement you can't take costs you the
    measurement and never the run.
    """
    counts: Dict[int, int] = {}
    for position, is_paper in enumerate(found.mask):
        if is_paper:
            index = quantisation.indices[position]
            counts[index] = counts.get(index, 0) + 1
    if not counts:
        return None

    index, hits = max(counts.items(), key=lambda item: item[1])
    if hits < sum(counts.values()) * BACKGROUND_COHERENCE:
        return None
    if hits < quantisation.indices.count(index) * BACKGROUND_PURITY:
        return None
    return index


def entry_for_color(
    quantisation: Quantisation,
    color: RGB,
    tolerance: float = REMOVAL_TOLERANCE,
    avoid: Sequence[int] = (),
) -> Optional[int]:
    """Which palette entry a person meant when they picked this colour (C1).

    The nearest one in CIE Lab, which is the quantiser's own answer: every pixel
    of the image was assigned to an entry by exactly this question, so a colour
    picked out of the source resolves to the layer that colour was traced into.
    Nearest in *Lab* rather than in RGB for the reason A4 wrote down — RGB
    distance does not match what a person sees, and this is a person pointing.

    ``None`` when the nearest entry is further off than ``tolerance``, which is
    the guard that makes the mechanism safe to replay: a removal named against
    one attempt is re-resolved against the next, and a retry that re-quantised
    the image may not have that colour any more. Removing *something* because it
    was the closest thing left would delete a colour nobody picked, so nothing is
    removed and the caller says so — the project's own rule that a question you
    can't answer costs the answer and never the run.

    :func:`background_entry` asks a related question and answers it a different
    way on purpose: it matches on **pixels**, because the paper's colour has been
    through denoising and contrast since it was seed-matched, while a picked
    colour is read off the palette (or the image) that this attempt produced.

    ``avoid`` breaks a **tie** towards an entry that is not in it, and does no
    more than that: it cannot make a further colour win, so it can never delete
    something nobody picked, which is what the tolerance above is for. It exists
    for B9, which is the only way two entries can hold the same colour — the
    paper, and the part of the paper the artwork encloses. Those two are exactly
    as close to any pick, and the one a person can mean is the one being sewn:
    naming a colour that is already fabric is a request with nothing behind it,
    and the caller has a sentence ready for it if that is genuinely all there is.
    """
    if not quantisation.palette:
        return None
    target = srgb_to_lab("#{:02x}{:02x}{:02x}".format(*color))
    avoided = set(avoid)
    best, best_key = 0, None
    for index, entry in enumerate(quantisation.palette):
        here = srgb_to_lab("#{:02x}{:02x}{:02x}".format(*entry))
        distance = math.sqrt(
            sum((here[axis] - target[axis]) ** 2 for axis in range(3))
        )
        key = (distance, index in avoided)
        if best_key is None or key < best_key:
            best, best_key = index, key
    if best_key is None or best_key[0] > tolerance:
        return None
    return best


def _eroded(mask: Sequence[bool], width: int, height: int, radius: int) -> List[bool]:
    """What is left of a mask once a disc of ``radius`` is walked around inside it.

    A5's question — *does a disc the width of a stitch fit in here* — answered on
    the pixel grid, so it needs no geometry backend and runs on a phone. Separable:
    a square erosion is a horizontal one then a vertical one.
    """
    out = list(mask)
    if radius < 1:
        return out
    for along_rows in (True, False):
        outer, inner = (height, width) if along_rows else (width, height)
        step = 1 if along_rows else width
        into = [False] * len(out)
        for major in range(outer):
            base = major * width if along_rows else major
            for minor in range(inner):
                if minor < radius or minor >= inner - radius:
                    continue  # the frame cannot be an interior
                into[base + minor * step] = all(
                    out[base + (minor + offset) * step]
                    for offset in range(-radius, radius + 1)
                )
        out = into
    return out


def _survives_the_needle(
    quantisation: Quantisation, index: int, radius: int
) -> bool:
    """Is any part of this palette entry as wide as the profile's thinnest stitch?

    :func:`_eroded` asked of a colour rather than of a contour.
    """
    return any(
        _eroded(
            [value == index for value in quantisation.indices],
            quantisation.width,
            quantisation.height,
            radius,
        )
    )


def _extra_entry_earned_its_thread(
    quantisation: Quantisation, background: int, radius: int
) -> bool:
    """Did asking for one more colour buy artwork, or the edge between two?

    **The finding that put this here, measured on the corpus.** Handing the
    quantiser an extra entry does not hand it to the drawing. On **11 of the 18
    images where a background is isolated at all** it goes to the antialiasing
    ramp between two real colours — ``#8c8c8c`` at 0.25% of ``logo-two-colour``,
    ``#c5c5c5`` at 0.02% of ``monogram`` — because a filtered hard edge really
    is a distinct colour, just not one anybody chose. On the other seven it goes
    where it was meant to: ``logo-five-colour`` gets ``#c8102e`` at 11.7%, a
    fourth colour of the logo that the old budget could not afford.

    Note which way the split falls. The images that *earn* it are the ones with
    real colour in them — the photographs, the gradients, ``logo-five-colour``.
    The ones that do not are flat artwork, where the profile's budget was
    already enough and the only thing left to spend a thread on is an edge.

    So the extra thread is granted on the same test the shop already applies to
    everything else: **can the machine sew it.** A ramp is one pixel wide by
    construction and vanishes under an erosion at the profile's own minimum
    feature; a colour of the artwork has an inside. No new number — ``radius``
    is the profile's ``stroke.min_width`` in this attempt's pixels, the same
    kernel B1 sizes the working resolution around.

    Rejecting it is not the same as keeping the background. The caller
    quantises again at the plain budget and still leaves the paper unstitched,
    so a two-ink drawing at a three-thread budget comes back as two inks and
    bare fabric — which is what it is, rather than two inks, bare fabric and a
    grey hairline tracing every edge in the design.
    """
    return all(
        _survives_the_needle(quantisation, index, radius)
        for index in range(quantisation.colors)
        if index != background and quantisation.area(index) > 0
    )


def plan_palette(
    cleaned: Raster, recipe: Recipe, found: Optional[Background]
) -> Tuple[Quantisation, Optional[int], Optional[Stage]]:
    """Quantise, and decide whether one of the entries is paper to leave out.

    B8's whole decision in one place, because it is three decisions that have to
    agree: the budget counts threads, the paper is extra, and the extra entry is
    only kept if it was earned. Four ways out, and the last three all end at the
    same document the image got before B8 — with or without its paper:

    - **the paper isolated and the extra thread spent on the drawing.** The
      ``wanted + 1`` palette is kept and one entry is named as the background.
    - **the extra thread was not earned** (:func:`_extra_entry_earned_its_thread`)
      — it bought the antialiasing ramp between two real colours rather than a
      colour of the artwork. It is handed back and the image quantised at the
      plain budget, with the paper still left unstitched.
    - **...and the plain budget then does not isolate the paper.** Rare, and it
      has to be said rather than assumed: a different budget is a different
      clustering, so the guards in :func:`background_entry` get a second and
      independent chance to refuse.
    - **the paper was never isolated at all**, or there is none to find.

    Only the first costs a second quantisation, and only on the path that was
    going to spend one anyway. The stage note is the outcome in words; ``None``
    means the pipeline was not asked to look, so there is nothing to report.
    """
    wanted = recipe.colors
    if not recipe.drop_background:
        return quantise_lab(cleaned, wanted), None, None

    if found is None:
        return (
            quantise_lab(cleaned, wanted),
            None,
            Stage(
                "background",
                "no paper to leave unstitched, so the whole colour budget is "
                "spent the way it always was",
            ),
        )

    quantisation = quantise_lab(cleaned, wanted + 1)
    background = background_entry(quantisation, found)

    if background is None:
        return (
            quantise_lab(cleaned, wanted),
            None,
            Stage(
                "background",
                f"asked for {wanted + 1} colours so the paper could be left "
                "unstitched, but no single entry is cleanly the paper — "
                f"quantised at {wanted} and the background is stitched",
            ),
        )

    if not _extra_entry_earned_its_thread(quantisation, background, recipe.radius):
        quantisation = quantise_lab(cleaned, wanted)
        background = background_entry(quantisation, found)
        gave_back = (
            f"asked for {wanted + 1} colours, but the extra one bought the edge "
            "between two others rather than a colour of the artwork, so it was "
            "given back"
        )
        if background is None:
            return (
                quantisation,
                None,
                Stage(
                    "background",
                    f"{gave_back} — quantised at {wanted}, where no single entry "
                    "is cleanly the paper, so the background is stitched",
                ),
            )
        return (
            quantisation,
            background,
            Stage(
                "background",
                f"{gave_back} — quantised at {wanted} and left "
                + _hex(quantisation.palette[background])
                + " unstitched",
            ),
        )

    return (
        quantisation,
        background,
        Stage(
            "background",
            f"asked for {wanted + 1} colours and left "
            + _hex(quantisation.palette[background])
            + f" unstitched, so all {wanted} thread(s) go to the artwork",
        ),
    )


def run(raster: Raster, recipe: Recipe) -> Preprocessed:
    """The whole pipeline: an image in, labels ready to trace out.

    **B8: the colour budget is the artwork's, and the paper is extra.** A shop's
    ``color.max_count`` is a count of threads, and nobody loads the garment onto
    the machine — so when the paper is going to be left unstitched the quantiser
    is asked for ``colors + 1`` and the extra entry is the one that goes. The
    alternative reading spends a thread on fabric, and does it unevenly: the
    same artwork exported with an alpha channel would get three inks where the
    scan of it got two, which is the tool grading the file format rather than
    the design.

    The ``+1`` is **conditional on actually dropping something**, so it is asked
    for, checked, and given back when either check fails: an image whose paper
    the quantiser will not isolate cleanly, or one where the extra entry buys an
    edge rather than a colour, is quantised again at the plain budget. That costs
    a second quantisation on the path that was going to be disappointing anyway,
    and it is the only way to avoid shipping a document with one colour more than
    the profile allows. :func:`plan_palette` holds that decision.
    """
    cleaned, stages, found = clean(raster, recipe)

    quantisation, background, said = plan_palette(cleaned, recipe, found)
    if said is not None:
        stages.append(said)

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

    # B9, and last on purpose: despeckling relabels pixels, so a pocket of paper
    # small enough to be absorbed is absorbed *before* anything asks whether the
    # artwork encloses it. Asked the other way round, this would sew islands the
    # next stage was about to delete.
    enclosed: Optional[int] = None
    if recipe.sew_background_holes and background is not None:
        quantisation, found_inside = sew_enclosed_background(
            quantisation, background, recipe.radius
        )
        enclosed = found_inside.index
        skipped = (
            f", and {found_inside.too_fine} too fine for the needle to sew, left "
            "to the fabric"
            if found_inside.too_fine
            else ""
        )
        if enclosed is not None:
            says = (
                f"{found_inside.sewn} region(s) of "
                + _hex(quantisation.palette[background])
                + " closed in by the artwork are sewn instead"
                f" ({_percent(found_inside.share)} of the image){skipped} — the "
                "paper is a thread now, which the colour budget will see"
            )
        elif found_inside.too_fine:
            says = (
                f"{found_inside.too_fine} region(s) of paper are closed in by the "
                "artwork, and every one is finer than this ruleset can sew, so the "
                "background is left whole"
            )
        else:
            says = (
                "the border reaches all the paper, so none of it is inside the "
                "artwork and the background is left whole"
            )
        stages.append(Stage("enclosed", says))

    return Preprocessed(
        cleaned=cleaned,
        quantisation=quantisation,
        stages=stages,
        background=background,
        enclosed=enclosed,
    )
