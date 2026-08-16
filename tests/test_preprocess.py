"""B3: the preprocessing pipeline — one before/after test per stage, then the gates."""

import pytest

from svg_embroidery.bench import load_corpus, run
from svg_embroidery.colors import color_distance
from svg_embroidery.preprocess import (
    LOW_CONTRAST,
    MIN_SEPARATION,
    Recipe,
    clean,
    contrast_span,
    denoise,
    despeckle,
    flatten_background,
    noise_sigma,
    normalise_contrast,
    quantise_lab,
    upscale,
)
from svg_embroidery.preprocess import run as preprocess
from svg_embroidery.raster import edge_density, flat_ratio, quantise, rgb_pixels, thin_ratio
from svg_embroidery.visual import Raster, compare_rasters

from test_bench import BLACK, RED, WHITE, image

PAPER = (250, 247, 240, 255)
INK = (40, 38, 44, 255)


def grain(seed=7, amount=3):
    """A deterministic ±amount jitter, the way the corpus makes a scan."""
    state = {"value": seed}

    def jitter():
        state["value"] = (1103515245 * state["value"] + 12345) % (1 << 31)
        return state["value"] % (2 * amount + 1) - amount

    return jitter


def scan(width=64, height=64, amount=3, paint=None):
    """Paper with a shape on it, and grain over the whole thing."""
    jitter = grain(amount=amount)

    def pixel(x, y):
        base = (paint or (lambda a, b: None))(x, y) or PAPER
        shift = jitter()
        return tuple(
            [min(255, max(0, channel + shift)) for channel in base[:3]] + [255]
        )

    return image(width, height, pixel)


def hexed(color):
    return "#{:02x}{:02x}{:02x}".format(*color)


# -- stage 1: upscale --------------------------------------------------------

def test_upscale_adds_pixels_without_adding_colours():
    """Interpolation would invent shades between the ones the artist chose."""
    icon = image(16, 16, lambda x, y: RED if x < 8 else WHITE)
    bigger = upscale(icon, 64)

    assert (bigger.width, bigger.height) == (64, 64)
    assert set(rgb_pixels(bigger)) == set(rgb_pixels(icon))
    # ...and the shape is where it was: still half red, split down the middle.
    pixels = rgb_pixels(bigger)
    assert pixels[0] == RED[:3] and pixels[63] == WHITE[:3]
    assert sum(1 for p in pixels if p == RED[:3]) == 64 * 32


def test_upscale_leaves_an_image_that_is_already_big_enough_alone():
    already = image(64, 64, lambda x, y: RED)
    assert upscale(already, 64) is already


# -- stage 2: background -----------------------------------------------------

def test_the_background_becomes_one_exact_colour():
    before = scan(paint=lambda x, y: INK if x < 20 else None)
    after, share = flatten_background(before)

    assert share > 0.5
    assert flat_ratio(after) > flat_ratio(before) + 0.5
    # The corners are what identified it, so they are certainly part of it.
    pixels = rgb_pixels(after)
    assert pixels[63] == pixels[-1]


def test_background_enclosed_by_artwork_is_flattened_too():
    """The paper inside a drawn ring is background, and no corner reaches it.

    Filling only what is connected to a corner stranded a quarter of
    ``scan-clean``'s paper with its grain intact — enough to stop the image
    measuring flat at all. The fill finds the colour; the snap applies it.
    """
    def ring(x, y):
        distance = ((x - 32) ** 2 + (y - 32) ** 2) ** 0.5
        return INK if 18 < distance < 24 else None

    after, share = flatten_background(scan(paint=ring))

    pixels = rgb_pixels(after)
    outside = pixels[0]
    inside = pixels[32 * 64 + 32]  # dead centre, walled in by the ring
    assert inside == outside, "the paper inside the ring is still paper"
    assert share > 0.8


def test_a_fill_that_would_swallow_the_picture_is_refused():
    """A flood that reaches everything has found the picture, not its background."""
    nearly_uniform = image(64, 64, lambda x, y: WHITE)
    after, share = flatten_background(nearly_uniform)
    assert share == 0.0
    assert after is nearly_uniform


# -- stage 3: denoise --------------------------------------------------------

def test_denoising_removes_grain_and_keeps_the_edge():
    """Both halves at once — a blur would manage only the first."""
    before = scan(paint=lambda x, y: INK if x < 32 else None)
    after, sigma = denoise(before)

    assert flat_ratio(after) > flat_ratio(before) + 0.5, "the grain is gone"

    pixels = rgb_pixels(after)
    row = 32 * 64
    step = abs(pixels[row + 31][0] - pixels[row + 32][0])
    assert step > 150, "the edge is still an edge, not a ramp"


def test_the_noise_estimate_is_not_fooled_by_dense_artwork():
    """The bug that blurred ``hatching`` away.

    Half of all neighbouring pairs in a crosshatch straddle a stroke, so the
    *median* difference lands mid-edge and reports noise where there is none.
    Sized from that, the filter destroys the drawing.
    """
    hatch = image(64, 64, lambda x, y: BLACK if x % 4 == 0 else WHITE)
    assert noise_sigma(hatch) < 1.0, "there is no grain in this at all"

    speckled = scan(amount=20)
    assert noise_sigma(speckled) > 2.0, "and plenty in this"


def test_a_filter_wide_enough_to_cross_a_real_edge_is_never_built():
    hatch = image(64, 64, lambda x, y: BLACK if x % 4 == 0 else WHITE)
    before = edge_density(quantise(hatch, 3))
    after, sigma = denoise(hatch)
    assert edge_density(quantise(after, 3)) > before * 0.9, "the strokes survived"


# -- stage 4: contrast -------------------------------------------------------

def test_a_faded_scan_is_stretched_back_out():
    faded = image(64, 64, lambda x, y: (150, 150, 150, 255) if x < 32 else (200, 200, 200, 255))
    assert contrast_span(faded) < LOW_CONTRAST

    after, stretched = normalise_contrast(faded)
    assert stretched
    assert contrast_span(after) > contrast_span(faded) + 0.4


def test_an_image_that_already_spans_the_range_is_left_alone():
    """Stretching it clips, and clipping manufactures flat pixels.

    Applied unconditionally this turned ``gradient-linear`` from 189 colours
    into 34 and moved a ramp from *hopeless* to *marginal* — preprocessing that
    makes an unconvertible image look convertible.
    """
    ramp = image(64, 64, lambda x, y: (x * 4, x * 4, x * 4, 255))
    assert contrast_span(ramp) >= LOW_CONTRAST

    after, stretched = normalise_contrast(ramp)
    assert not stretched
    assert after is ramp
    assert len(set(rgb_pixels(after))) == len(set(rgb_pixels(ramp)))


# -- stage 5: quantisation in Lab -------------------------------------------

def test_quantising_reduces_to_the_budget_and_keeps_flat_colours_exact():
    logo = image(64, 64, lambda x, y: RED if x < 21 else (BLACK if x < 42 else WHITE))
    reduced = quantise_lab(logo, 3)

    assert reduced.colors == 3
    assert set(reduced.palette) == {RED[:3], BLACK[:3], WHITE[:3]}
    assert compare_rasters(logo, reduced.raster()).ratio == 0.0


def test_two_entries_nobody_could_tell_apart_are_merged():
    """``scan-clean`` came back with #faf7f0 and #f9f6ef — 0.3 apart in Lab.

    That is not two colours, and the cost is not cosmetic: two entries
    alternating across one flat region put a boundary on every other pixel, and
    2% of the image measured as too fine to stitch. Entirely manufactured.
    """
    paper, twin, ink = (250, 247, 240), (249, 246, 239), (40, 38, 44)
    assert color_distance(hexed(paper), hexed(twin)) < MIN_SEPARATION

    def pixel(x, y):
        if y < 8:
            return tuple(list(ink) + [255])
        return tuple(list(paper if x % 2 else twin) + [255])

    reduced = quantise_lab(image(64, 64, pixel), 3)
    assert reduced.colors == 2, "the paper is one colour, so it gets one entry"

    # ...and with it goes the field of one-pixel slivers between the two.
    assert thin_ratio(reduced, 1) < 0.02


def test_lab_merges_the_colours_a_person_would():
    """The reason this quantiser exists rather than reusing the RGB one.

    RGB distance calls two dark blues far apart and a blue and a green close.
    Reduced to two colours, the pair the eye groups must end up together.
    """
    navy, midnight, grass = (0, 0, 128), (0, 0, 160), (0, 128, 0)

    def pixel(x, y):
        source = navy if x < 21 else (midnight if x < 42 else grass)
        return tuple(list(source) + [255])

    reduced = quantise_lab(image(63, 63, pixel), 2)
    assert reduced.colors == 2

    # Whichever two survive, the two blues must have landed in one entry.
    at = lambda x: reduced.palette[reduced.indices[31 * 63 + x]]  # noqa: E731
    assert at(10) == at(31), "the blues belong together"
    assert at(10) != at(52), "and the green does not belong with them"


def test_an_image_inside_its_budget_comes_back_untouched():
    logo = image(64, 64, lambda x, y: RED if x < 32 else WHITE)
    assert compare_rasters(logo, quantise_lab(logo, 5).raster()).ratio == 0.0


# -- stage 6: despeckle ------------------------------------------------------

def test_a_speck_is_absorbed_by_what_surrounds_it():
    def pixel(x, y):
        return BLACK if (x, y) in ((10, 10), (40, 41)) else WHITE

    reduced = quantise_lab(image(64, 64, pixel), 2)
    despeckled, share = despeckle(reduced, 3)

    assert 0 < share < 0.01
    assert len(set(despeckled.indices)) == 1, "nothing but background is left"


def test_a_hairline_is_not_a_speck_however_thin_it_is():
    """The line that separates despeckling from A5's destructive repair.

    An opening at the profile's minimum width deletes any shape narrower than
    the needle — measured, it took ``line-art-thin`` from 2.9% ink to 0.1%,
    which is vtracer's failure mode. A stroke crossing the whole image is thin,
    but it is not *small*, so dropping small regions leaves it alone.
    """
    line = image(64, 64, lambda x, y: BLACK if y == 32 else WHITE)
    reduced = quantise_lab(line, 2)
    before = sum(1 for index in reduced.indices if reduced.palette[index] == BLACK[:3])

    despeckled, share = despeckle(reduced, 3)
    after = sum(1 for index in despeckled.indices if despeckled.palette[index] == BLACK[:3])

    assert share == 0.0
    assert after == before == 64


def test_despeckling_is_off_by_default():
    """Because on this corpus every setting that removed anything removed artwork.

    The bilateral filter has already taken the grain out by the time stage 6
    runs, so the only regions small enough to absorb were the fragments of thin
    line art: at 3 px it ate 42% of ``hatching``. It stays available and stays
    off.
    """
    assert Recipe(colors=3).speck_area == 1
    line = image(64, 64, lambda x, y: BLACK if y == 32 else WHITE)
    result = preprocess(line, Recipe(colors=2))
    assert any("off" in stage.says for stage in result.stages if stage.name == "despeckle")


# -- the pipeline ------------------------------------------------------------

def test_the_pipeline_reports_what_each_stage_did():
    result = preprocess(scan(paint=lambda x, y: INK if x < 20 else None), Recipe(colors=3))
    named = {stage.name for stage in result.stages}
    assert named == {"background", "denoise", "contrast", "quantise", "despeckle"}
    assert all(stage.says for stage in result.stages)
    assert "background" in result.summary()


def test_the_grain_is_measured_before_the_background_is_flattened():
    """Order matters here, and only here.

    Snapping the paper to one value first sets half the pixels equal, so a noise
    floor read afterwards measures that flat region instead of the image:
    ``scan-noisy`` estimated 37 as it arrived and 0 once flattened, and the
    filter then sized itself for an image with no grain.
    """
    noisy = scan(amount=20, paint=lambda x, y: INK if x < 20 else None)
    flattened, _ = flatten_background(noisy)
    assert noise_sigma(noisy) > noise_sigma(flattened)

    # clean() takes its measurement first, so the filter is sized for the grain.
    cleaned, stages = clean(noisy, Recipe(colors=3))
    sigma = next(stage for stage in stages if stage.name == "denoise").says
    assert sigma != "bilateral filter at sigma 6", "sigma 6 is the floor: nothing was seen"


def test_a_recipe_can_switch_any_stage_off():
    before = scan()
    quiet = Recipe(colors=3, denoise=False, background=False, contrast=False)
    result = preprocess(before, quiet)
    assert compare_rasters(before, result.cleaned).ratio == 0.0


def test_the_colour_budget_comes_from_the_recipe_not_from_the_image():
    busy = image(64, 64, lambda x, y: (x * 4 % 256, y * 4 % 256, 128, 255))
    assert preprocess(busy, Recipe(colors=2)).quantisation.colors <= 2


# -- the gates ---------------------------------------------------------------

@pytest.fixture(scope="module")
def prepared():
    """The whole corpus through the pipeline — the slowest thing in the suite."""
    return {row.name: row for row in run(load_corpus(), preprocess=True).rows}


@pytest.fixture(scope="module")
def raw():
    return {row.name: row for row in run(load_corpus()).rows}


@pytest.mark.slow
def test_a_scanned_line_drawing_now_measures_like_the_flat_artwork_it_is(raw, prepared):
    """B3's headline gate, and the assertion B1 wrote down expecting it to fail.

    ``scan-clean`` is a line drawing on paper. Grain meant no pixel equalled its
    neighbour, so it measured 0.001 flat with 84% of it "too fine to stitch" —
    an accurate measurement of the grain and a useless one of the drawing.
    """
    before, after = raw["scan-clean"], prepared["scan-clean"]
    assert before.flat < 0.10 and before.thin > 0.5, "the gap B1 recorded"

    assert after.flat > 0.85, "it measures flat now"
    assert after.thin < 0.05, "and stitchable"
    assert after.quant < 0.05
    assert after.verdict == "good", "which is what a human said all along"


@pytest.mark.slow
def test_preprocessing_does_not_flatter_an_image_that_cannot_be_stitched(prepared):
    """The warning B2 left for this step, checked rather than trusted.

    Denoising run to convergence pulls ``hatching`` *past* ``scan-clean`` by
    smoothing the artwork away — it makes the hopeless image look better than
    the good one. If this step moves the crosshatch out of *hopeless*, it is
    deleting artwork rather than denoising.
    """
    assert prepared["hatching"].verdict == "hopeless"
    assert prepared["hatching"].thin > 0.9, "the strokes are still there to be too thin"

    for name in ("photo-portrait", "photo-landscape", "photo-busy",
                 "gradient-linear", "gradient-radial"):
        assert prepared[name].verdict == "hopeless", name


@pytest.mark.slow
def test_thin_line_art_survives_the_cleaning(raw, prepared):
    """vtracer's failure mode, which this step exists to avoid reproducing."""
    before, after = raw["line-art-thin"], prepared["line-art-thin"]
    assert after.thin == pytest.approx(before.thin, abs=0.01), "the strokes are as thin as ever"
    assert after.verdict == "marginal", "and still below the needle, as they were"


@pytest.mark.slow
def test_triage_still_separates_the_corpus_after_preprocessing(prepared):
    """19/20, and the miss moves from a corrupted metric to an unmeasured property.

    ``scan-skewed`` is crooked on the glass, which is the whole reason a human
    calls it marginal — and nothing here measures rotation. Before B3 it landed
    on *marginal* for its grain, which was the right answer for the wrong
    reason; with the grain gone there is no signal left, so it now reads *good*.
    """
    graded = {name: row for name, row in prepared.items() if row.expect and row.verdict}
    missed = {
        name: (row.expect, row.verdict)
        for name, row in graded.items()
        if row.expect != row.verdict
    }
    assert missed == {"scan-skewed": ("marginal", "good")}


@pytest.mark.slow
def test_the_metrics_improve_where_the_roadmap_said_they_would(raw, prepared):
    """Scans get better; the images that were already clean do not get worse."""
    for name in ("scan-clean", "scan-noisy", "scan-skewed"):
        assert prepared[name].edges < raw[name].edges / 2, name
        assert prepared[name].thin < raw[name].thin / 2, name

    for name in ("logo-two-colour", "monogram", "line-art-thick", "antialiased-edges"):
        assert prepared[name].quant <= raw[name].quant + 0.01, name
        assert prepared[name].thin <= raw[name].thin + 0.01, name


# -- how the benchmark carries it -------------------------------------------

def test_a_preprocessed_run_is_never_diffed_against_a_raw_baseline():
    """Same rule as the resolution and the tracer: different question, no diff."""
    from svg_embroidery.bench import incomparable

    entries = load_corpus()[:1]
    prepared_run = run(entries, preprocess=True)
    raw_run = run(entries)

    reasons = incomparable(raw_run.to_dict(), prepared_run)
    assert reasons and "preprocessing" in reasons[0]
    assert incomparable(prepared_run.to_dict(), prepared_run) == []


def test_the_run_says_whether_it_preprocessed(capsys):
    from svg_embroidery.bench import render_table

    entries = load_corpus()[:1]
    assert "as handed in" in render_table(run(entries))
    assert "preprocessed (B3)" in render_table(run(entries, preprocess=True))


def test_the_stages_are_reported_against_the_row():
    entries = [entry for entry in load_corpus() if entry.name == "scan-clean"]
    row = run(entries, preprocess=True).rows[0]
    assert any(note.startswith("preprocess/denoise") for note in row.notes)
