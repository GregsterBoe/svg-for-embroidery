"""B6: the conversion loop — the knobs, the order they are tried, and the gate.

The gate for this step is *most of the images a human called convertible come
out passing their profile, unattended*. The corpus answers that in one command
(``svgemb bench -p embroidery-strict --convert``); what is tested here is the
machinery underneath it, plus the same gate in miniature on a purpose-built
image — one whose detail is too fine at the smallest size the profile allows
and stitchable at a size it also allows.
"""

from dataclasses import replace

import pytest

from svg_embroidery.checker import Checker
from svg_embroidery.document import parse_svg
from svg_embroidery.cli import main
from svg_embroidery.convert import (
    CANVAS_STEP,
    MIN_COLORS,
    ConvertError,
    Settings,
    adjust,
    canvas_limits,
    clamp,
    convert,
    initial_settings,
    kernel_radius,
    limits_for,
    min_area_mm2,
    plan_next,
    prepare,
    render_conversion,
    working_side,
)
from svg_embroidery.profiles import load_profile
from svg_embroidery.raster import encode_png
from svg_embroidery.tracer import DISABLE_ENV_VAR, available_backends
from svg_embroidery.visual import Raster, available_renderers as renderers, render

BASIC = load_profile("embroidery-basic")
STRICT = load_profile("embroidery-strict")

WHITE = (255, 255, 255, 255)
BLACK = (26, 26, 26, 255)


def image(width, height, paint):
    pixels = bytearray()
    for y in range(height):
        for x in range(width):
            pixels += bytes(paint(x, y))
    return Raster(width=width, height=height, pixels=bytes(pixels))


def block(x, y):
    """A solid square, so an element cannot pass by having nothing left in it."""
    return 20 <= x < 90 and 20 <= y < 90


def lattice(x, y):
    """A grid of 5 px bars: 1.5 mm at 8 cm on the nose, and clearly finer where
    they cross. The repair cannot rescue it — B5's opening leaves a stub where a
    thin part meets a solid one — so the profile is failed outright and only a
    change of settings answers it."""
    return 110 <= x < 250 and 20 <= y < 250 and ((x - 110) % 40 < 5 or (y - 20) % 40 < 5)


def bars(x, y):
    """Three 4 px bars. Straight and separate, so the destructive repair *can*
    satisfy the profile — by cutting all three off. This is the drawing that
    passes at 8 cm with 42% of its ink deleted."""
    return 120 <= x < 124 or 160 <= x < 164 or 200 <= x < 204


#: Too fine to stitch at the smallest size the profile allows, comfortable half
#: again as large. The same artwork, failing or passing on nothing but its size.
LATTICE = image(256, 256, lambda x, y: BLACK if block(x, y) or lattice(x, y) else WHITE)

#: Passes at the smallest size, but only because the bars are cut away.
FINE_LINES = image(256, 256, lambda x, y: BLACK if block(x, y) or bars(x, y) else WHITE)

needs_tracer = pytest.mark.skipif(
    not available_backends(), reason="no tracer installed here"
)


@pytest.fixture(scope="module")
def lattice_run():
    """One conversion of :data:`LATTICE`, shared: tracing is the slow part."""
    return convert(LATTICE, STRICT, name="lattice")


@pytest.fixture(scope="module")
def bars_run():
    return convert(FINE_LINES, STRICT, name="fine-bars")


# -- reading the profile -----------------------------------------------------

def test_a_conversion_starts_at_the_smallest_size_the_profile_allows():
    """The pessimistic end, and for the same reason B1 measures there."""
    settings, note = initial_settings(STRICT, source_side=256)
    assert settings.canvas_mm == 80.0
    assert settings.colors == 2
    assert not note  # nothing to enlarge


def test_the_canvas_ceiling_is_the_tightest_bound_the_profile_sets():
    smallest, largest = canvas_limits(STRICT)
    assert (smallest, largest) == (80.0, 300.0)
    assert canvas_limits(BASIC) == (100.0, 380.0)


def test_a_profile_with_no_canvas_rule_has_no_ceiling_of_ours():
    """No opinion in the profile means the try budget is the only limit."""
    silent = load_profile("embroidery-basic")
    silent.rules = [spec for spec in silent.rules if spec.id != "geometry.canvas_size"]
    _, largest = canvas_limits(silent)
    assert largest == float("inf")


def test_the_smallest_stitchable_patch_comes_from_the_profile():
    assert min_area_mm2(STRICT) == 2.25
    stripped = load_profile("embroidery-strict")
    stripped.rules = [spec for spec in stripped.rules if spec.id != "geometry.min_area"]
    # Falls back to the square of the minimum feature width: the smallest patch
    # a stitch that wide can fill in both directions.
    assert min_area_mm2(stripped) == pytest.approx(1.5 * 1.5)


def test_a_source_with_too_few_pixels_is_enlarged_and_says_so():
    settings, note = initial_settings(STRICT, source_side=32)
    assert settings.work_side == 160
    assert "32px" in note and "160px" in note
    assert prepare(image(32, 32, lambda x, y: BLACK), settings).width == 160


def test_a_source_with_plenty_of_pixels_is_reduced_without_comment():
    side, note = working_side(STRICT, source_side=1024)
    assert side == 160 and note == ""
    assert prepare(FINE_LINES, Settings(80.0, 2, side)).width == 160


def test_the_kernel_follows_the_canvas_rather_than_being_carried_over():
    """A design sewn larger has fewer pixels per millimetre, so a smaller kernel."""
    tiny = kernel_radius(STRICT, Settings(canvas_mm=40.0, colors=2, work_side=160))
    start = kernel_radius(STRICT, Settings(canvas_mm=80.0, colors=2, work_side=160))
    grown = kernel_radius(STRICT, Settings(canvas_mm=300.0, colors=2, work_side=160))
    # One pixel is the floor — below it there is no kernel — so the movement to
    # watch is upward as the design gets smaller.
    assert tiny > start >= grown >= 1


# -- which knob, and in what order -------------------------------------------

def test_detail_too_fine_asks_for_a_bigger_canvas_first():
    settings = Settings(canvas_mm=80.0, colors=2, work_side=160)
    proposed, why = adjust(settings, ["geometry.min_feature_size"], STRICT)
    assert why.knob == "canvas" and why.turned
    assert proposed.canvas_mm == pytest.approx(80.0 * CANVAS_STEP)
    # Nothing else moved: the artwork is not touched, only its size.
    assert (proposed.colors, proposed.speck_area) == (2, 1)


def test_the_cheapest_knob_wins_when_two_complaints_are_open():
    """Order is a claim about what a conversion may cost the artwork."""
    settings = Settings(canvas_mm=80.0, colors=3, work_side=160)
    _, why = adjust(
        settings, ["color.max_count", "geometry.min_feature_size"], STRICT
    )
    assert why.knob == "canvas"


def test_too_many_shapes_absorbs_specks_the_profile_already_rejects():
    settings = Settings(canvas_mm=80.0, colors=2, work_side=160)
    proposed, why = adjust(settings, ["path.max_count"], STRICT)
    assert why.knob == "speckle"
    # 2.25 mm² at 0.5 mm per pixel is nine pixels, and not one more: past the
    # profile's own number, despeckling stops removing specks.
    assert proposed.speck_area == 9
    assert adjust(proposed, ["path.max_count"], STRICT)[0] is None


def test_dropping_a_colour_is_last_and_never_reaches_one():
    settings = Settings(canvas_mm=80.0, colors=3, work_side=160)
    proposed, why = adjust(settings, ["color.max_count"], STRICT)
    assert why.knob == "colours" and proposed.colors == 2
    exhausted, why = adjust(proposed, ["color.max_count"], STRICT)
    assert exhausted is None and not why.turned
    assert str(MIN_COLORS) in why.says


def test_a_knob_with_no_room_left_says_which_limit_it_hit():
    settings = Settings(canvas_mm=300.0, colors=2, work_side=160)
    proposed, why = adjust(settings, ["geometry.min_feature_size"], STRICT)
    assert proposed is None
    assert not why.turned
    assert "30.0 cm" in why.says and "largest" in why.says


def test_a_complaint_no_knob_answers_stops_the_loop():
    """Better a run that stops than one that turns something at random."""
    settings = Settings(canvas_mm=80.0, colors=2, work_side=160)
    assert adjust(settings, ["color.no_gradients"], STRICT) is None


# -- turning the knobs by hand (B7) ------------------------------------------

def test_a_slider_reaches_exactly_as_far_as_the_loop_does():
    """The range is read off the profile, not invented for the page."""
    settings = Settings(canvas_mm=80.0, colors=2, work_side=160)
    limits = limits_for(STRICT, settings)
    assert (limits.canvas_min_mm, limits.canvas_max_mm) == canvas_limits(STRICT)
    assert not limits.canvas_open_ended
    # The speck cap is the number _more_despeckling stops at, for the same
    # reason: past the profile's own minimum area it removes artwork.
    assert limits.speck_max == adjust(settings, ["path.max_count"], STRICT)[0].speck_area


def test_a_colour_slider_never_offers_more_than_the_profile_allows():
    """A budget over the limit only buys a document that fails its own check."""
    assert limits_for(STRICT, Settings(80.0, 2, 160)).colors_max == 2
    assert limits_for(BASIC, Settings(100.0, 3, 200)).colors_max == 3
    # A shop that stocks one thread means one, so the floor gives way, not the
    # ceiling.
    vinyl = load_profile("plotter-vinyl")
    limits = limits_for(vinyl, initial_settings(vinyl, 256)[0])
    assert (limits.colors_min, limits.colors_max) == (1, 1)


def test_a_profile_with_no_maximum_size_says_the_end_is_the_pages_own(tmp_path):
    path = tmp_path / "open.yaml"
    path.write_text("name: open\nrules:\n  - id: color.max_count\n    max: 3\n")
    profile = load_profile(str(path))
    limits = limits_for(profile, initial_settings(profile, 256)[0])
    assert limits.canvas_open_ended
    assert limits.canvas_max_mm > limits.canvas_min_mm


def test_settings_dragged_past_the_profile_are_pulled_back_and_reported():
    wanted = Settings(canvas_mm=5000.0, colors=99, work_side=160, speck_area=400)
    settings, notes = clamp(wanted, STRICT)
    assert settings.canvas_mm == 300.0 and settings.colors == 2
    assert len(notes) == 3
    assert "30.0 cm" in notes[0] and "2" in notes[1] and "mm²" in notes[2]
    # Inside the limits, nothing is changed and nothing is said.
    assert clamp(settings, STRICT) == (settings, [])


@needs_tracer
def test_the_loop_can_be_run_one_attempt_at_a_time(lattice_run):
    """B7 drives the loop from a browser, and must make the same decisions.

    ``plan_next`` is the judgement itself, lifted out of the ``for``: asked
    about an attempt this run already made, it proposes exactly what the run
    went on to do. A page that re-implemented this would be a second copy of
    B6 with no test suite behind it.
    """
    first = lattice_run.attempts[0]
    proposed, adjustment = plan_next(first, STRICT)
    assert adjustment.knob == first.adjustment.knob
    assert adjustment.says == first.adjustment.says
    assert proposed.canvas_mm == lattice_run.attempts[1].settings.canvas_mm
    # And the attempt the run kept is one the page can point at by number.
    assert lattice_run.attempts[lattice_run.best_index] is lattice_run.best


@needs_tracer
def test_an_attempt_that_passed_with_the_drawing_intact_ends_it(lattice_run):
    assert plan_next(lattice_run.best, STRICT) is None


def test_the_speck_cap_is_read_at_the_size_that_survived_clamping():
    """One pixel is worth more millimetres on a bigger canvas, so the cap moves."""
    small, _ = clamp(Settings(80.0, 2, 160, speck_area=9), STRICT)
    assert small.speck_area == 9
    big, notes = clamp(Settings(9999.0, 2, 160, speck_area=9), STRICT)
    assert big.canvas_mm == 300.0 and big.speck_area == 1
    assert any("would remove artwork at this size" in note for note in notes)


# -- the loop ----------------------------------------------------------------

def test_converting_without_a_tracer_is_an_error_not_an_empty_answer(monkeypatch):
    """Every other capability degrades; this one is the job itself."""
    monkeypatch.setenv(DISABLE_ENV_VAR, "1")
    with pytest.raises(ConvertError) as raised:
        convert(FINE_LINES, STRICT)
    assert "tracer" in str(raised.value)


@needs_tracer
def test_the_gate_an_image_too_fine_at_the_smallest_size_converts_at_a_larger_one(
    lattice_run,
):
    assert lattice_run.passes, [f.message for f in lattice_run.best.errors]
    assert lattice_run.tries > 1, "it passed first time, so it says nothing about retries"
    assert lattice_run.settings.canvas_mm > 80.0
    # The first attempt has to have genuinely failed on the rule the retry
    # answers, or the loop turned a knob for the wrong reason.
    assert "geometry.min_feature_size" in lattice_run.attempts[0].failing_rules
    assert lattice_run.adjustments()[0].knob == "canvas"


@needs_tracer
def test_the_same_image_fails_without_the_retry():
    """The control: one pass is B5, and B5 is what B6 was built to argue with."""
    once = convert(LATTICE, STRICT, tries=1, name="lattice")
    assert not once.passes
    assert once.tries == 1
    assert not once.adjustments()[0].turned  # out of attempts, not out of knobs


@needs_tracer
def test_the_converted_document_passes_an_independent_check(lattice_run):
    """Whatever the loop writes, ``svgemb check`` has to agree with."""
    assert not Checker(STRICT).check_source(lattice_run.svg).errors


@needs_tracer
def test_a_pass_bought_by_deleting_the_drawing_is_not_the_end_of_it(bars_run):
    """The reason a passing attempt can still be retried, measured not asserted.

    These bars satisfy the profile at the smallest canvas — by being cut off.
    Read as a statement about the size instead, the same drawing comes back
    whole at a size the same profile also allows.
    """
    first = bars_run.attempts[0]
    assert first.passes and first.cut_detail
    assert bars_run.passes and not bars_run.best.cut_detail
    assert bars_run.best.settings.canvas_mm > first.settings.canvas_mm
    # The point of the exercise: the bars are still in the document.
    assert bars_run.best.shapes > first.shapes


@needs_tracer
def test_at_the_profiles_largest_size_there_is_nothing_left_to_try():
    """A knob with no room left stops the loop rather than spending the budget."""
    result = convert(
        LATTICE,
        STRICT,
        name="lattice",
        settings=Settings(canvas_mm=300.0, colors=2, work_side=160),
    )
    assert result.tries == 1
    assert result.best is result.attempts[0]


@needs_tracer
def test_the_report_says_what_it_changed_and_why(lattice_run):
    text = render_conversion(lattice_run)
    assert "try 1" in text and "try 2" in text
    assert "stitched larger" in text
    assert "converted at" in text
    plain = render_conversion(lattice_run, color=False)
    assert "✅" not in plain and "❌" not in plain


# -- the command -------------------------------------------------------------

@needs_tracer
def test_the_command_writes_a_passing_svg(tmp_path, capsys):
    source = tmp_path / "lattice.png"
    source.write_bytes(encode_png(LATTICE))
    out = tmp_path / "design.svg"

    assert main(["convert", str(source), "-p", "embroidery-strict", "-o", str(out)]) == 0
    assert out.is_file()
    assert main(["check", str(out), "-p", "embroidery-strict"]) == 0
    assert "wrote" in capsys.readouterr().out


@needs_tracer
def test_nothing_is_written_without_being_asked(tmp_path, capsys):
    source = tmp_path / "lattice.png"
    source.write_bytes(encode_png(LATTICE))
    assert main(["convert", str(source), "-p", "embroidery-strict"]) == 0
    assert not list(tmp_path.glob("*.svg"))
    assert "nothing written" in capsys.readouterr().out


@needs_tracer
def test_a_conversion_that_still_fails_exits_one(tmp_path):
    source = tmp_path / "lattice.png"
    source.write_bytes(encode_png(LATTICE))
    assert main(
        ["convert", str(source), "-p", "embroidery-strict", "--no-retry"]
    ) == 1


def test_handing_convert_an_svg_points_at_the_command_that_takes_one(tmp_path, capsys):
    target = tmp_path / "already.svg"
    target.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    assert main(["convert", str(target)]) == 2
    assert "svgemb fix" in capsys.readouterr().err


def test_the_command_refuses_a_profile_it_cannot_load(capsys):
    assert main(["convert", "whatever.png", "-p", "no-such-profile"]) == 2
    assert "error" in capsys.readouterr().err


def test_two_images_cannot_share_one_output_file(tmp_path, capsys):
    for name in ("a.png", "b.png"):
        (tmp_path / name).write_bytes(encode_png(image(8, 8, lambda x, y: BLACK)))
    assert main(
        ["convert", str(tmp_path / "a.png"), str(tmp_path / "b.png"), "-o", "x.svg"]
    ) == 2
    assert "one image at a time" in capsys.readouterr().err


# -- the benchmark column ----------------------------------------------------

@needs_tracer
def test_the_bench_fills_the_tries_column_and_grades_the_gate(tmp_path, capsys):
    from tests.test_bench import tiny_corpus  # noqa: PLC0415

    directory = tiny_corpus(tmp_path / "c", {"lattice": LATTICE})
    assert main(
        [
            "bench", "--corpus", str(directory), "--no-compare",
            "-p", "embroidery-strict", "--convert",
        ]
    ) == 0
    out = capsys.readouterr().out
    assert "converted with retries (B6)" in out
    assert "aimed at embroidery-strict" in out
    assert "good or marginal" in out


def test_a_baseline_taken_at_another_profile_is_not_diffed():
    from svg_embroidery.bench import BenchRun, incomparable

    run = BenchRun(rows=[], work_side=None, corpus="", profile="embroidery-strict")
    reasons = incomparable(dict(run.to_dict(), profile=""), run)
    assert reasons and "profile" in reasons[0]
    assert incomparable(run.to_dict(), run) == []


def test_a_baseline_taken_without_the_loop_is_not_diffed():
    from svg_embroidery.bench import BenchRun, incomparable

    run = BenchRun(rows=[], work_side=None, corpus="", tracer="potrace", convert=True)
    reasons = incomparable(dict(run.to_dict(), convert=False), run)
    assert reasons and "conversion" in reasons[0]


def test_the_loop_and_the_tracer_comparison_are_different_questions(capsys):
    assert main(["bench", "--convert", "--tracers"]) == 2
    assert "different questions" in capsys.readouterr().err


# -- B8: the colour budget is threads, and the paper is not one --------------

def three_inks_on_paper(side=128):
    """Three flat inks on white, which is four colours at a budget of three."""
    def paint(x, y):
        if 10 <= x < 40:
            return BLACK
        if 50 <= x < 80:
            return (200, 16, 46, 255)
        if 90 <= x < 118:
            return (32, 74, 160, 255)
        return WHITE

    return image(side, side, paint)


@needs_tracer
def test_the_budget_buys_three_threads_of_ink_rather_than_two_and_the_paper():
    """B8's decision, made visible in the checker's own output.

    ``color.max_count`` at 3 used to mean *two inks and the garment*, because
    the paper was a palette entry like any other. Three inks on white now come
    out as three inks, and the document still passes the rule it was converted
    for — the +1 is spent on the artwork, not on the budget.
    """
    result = convert(three_inks_on_paper(), BASIC, name="three-inks")
    document = parse_svg(result.svg)

    assert result.passes, [f.message for f in result.best.errors]
    assert len(document.colors()) == 3
    assert "#ffffff" not in {color.lower() for color in document.colors()}
    assert any("left unstitched" in note for note in result.best.trace_notes)


@needs_tracer
def test_keeping_the_background_spends_a_thread_on_the_garment():
    """The opt-out, and the reading it restores.

    Not a retry knob — no failure the loop can see is answered by stitching the
    paper back in — so it is a flag on the command rather than something
    ``plan_next`` may turn. What it buys is the pre-B8 document: the paper is a
    colour, and it is one of the three the profile allows.
    """
    kept = convert(three_inks_on_paper(), BASIC, name="three-inks", drop_background=False)
    colors = {color.lower() for color in parse_svg(kept.svg).colors()}

    assert kept.best.prepared.background is None
    assert "#ffffff" in colors
    assert not any("left unstitched" in note for note in kept.best.trace_notes)
    # ...and the ink the paper displaced is the one that had to go.
    assert len(colors) == 3


@needs_tracer
def test_the_command_can_be_told_to_stitch_the_paper(tmp_path, capsys):
    source = tmp_path / "inks.png"
    source.write_bytes(encode_png(three_inks_on_paper()))
    out = tmp_path / "kept.svg"

    assert main(["convert", str(source), "--keep-background", "-o", str(out)]) == 0
    assert "#ffffff" in out.read_text(encoding="utf-8").lower()

    dropped = tmp_path / "dropped.svg"
    assert main(["convert", str(source), "-o", str(dropped)]) == 0
    assert "#ffffff" not in dropped.read_text(encoding="utf-8").lower()


@needs_tracer
def test_the_run_says_what_it_left_to_the_fabric_without_being_asked(tmp_path, capsys):
    """B8's news is the loudest thing the run did, and -v is too late to hear it.

    Same reasoning as A6's skip list: a run accounts for what it left out as
    plainly as for what it did. And the summary counts **inks**, not the
    profile's budget — a one-ink design on paper reported as "3 colour(s)"
    because three are allowed is a claim about the file, not the permission.
    """
    source = tmp_path / "one-ink.png"
    source.write_bytes(encode_png(image(128, 128, lambda x, y: BLACK if 40 <= x < 88 else WHITE)))

    assert main(["convert", str(source)]) == 0
    out = capsys.readouterr().out
    assert "1 ink(s)" in out
    assert "#ffffff is left unstitched" in out and "fabric shows through" in out

    # Stitch it instead and neither line appears: the paper is a colour again.
    assert main(["convert", str(source), "--keep-background"]) == 0
    kept = capsys.readouterr().out
    assert "2 ink(s)" in kept
    assert "left unstitched" not in kept


# -- C1: a person picking the colour that goes -----------------------------

def two_inks_on_paper(side=128):
    """Two flat inks on white, at a budget of three — so the +1 is not earned.

    B8 measured this case on the corpus: handed a spare entry, flat artwork
    spends it on the antialiasing ramp between two colours rather than on a
    colour, so the entry is given back and the image is quantised at the plain
    budget. That makes this the one fixture where the automatic route and the
    hand-picked one see the same palette, which is what lets them be compared
    byte for byte.
    """
    def paint(x, y):
        if 20 <= x < 60:
            return BLACK
        if 70 <= x < 110:
            return (200, 16, 46, 255)
        return WHITE

    return image(side, side, paint)


def three_bands(side=128):
    """Blue field, red band, white band — and the red is in the middle.

    Built so removing the red proves something: the blue underneath it is
    stitched *first*, so B4's trap has been growing blue a pixel into the red
    all along. A design where the removed colour is the bottom layer would pass
    the same test with the exclusion taken out. It also spans enough of the
    tonal range that B3 leaves the contrast alone, so the colours that come out
    are the colours that went in and the test can name them.

    It is **full-bleed**, so the corner fill calls the blue field a background
    and B8 drops it unless told otherwise — the case the roadmap names with
    ``logo-three-colour``, where whether the field is stitched depends on
    something the tool cannot see. The tests below convert it with
    ``drop_background=False``, which is a person saying *this is a patch, sew
    the field*, and is also what leaves a stitched layer under the red.
    """
    def paint(x, y):
        if 30 <= y < 70:
            return (200, 16, 46, 255)
        if 90 <= y < 110:
            return WHITE
        return (32, 74, 160, 255)

    return image(side, side, paint)


@needs_tracer
def test_a_colour_someone_removed_is_left_to_the_fabric():
    """C1's mechanism: the same skip B8 built, with the index picked by hand."""
    result = convert(
        three_bands(), BASIC, name="bands",
        drop_background=False, remove=["#c8102e"],
    )
    colors = {color.lower() for color in parse_svg(result.svg).colors()}

    assert "#c8102e" not in colors
    assert colors == {"#204aa0", "#ffffff"}
    assert [(layer.color, layer.reason) for layer in result.best.layers] == [
        ("#204aa0", ""), ("#c8102e", "removed"), ("#ffffff", "")
    ]
    assert any("#c8102e is left unstitched" in note for note in result.best.trace_notes)


@needs_tracer
def test_the_removed_colour_takes_its_thread_with_it_rather_than_freeing_one():
    """B8 hands the paper's thread back to the artwork; C1 deliberately does not.

    The paper was never a thread, so counting it as one was the defect B8 fixed.
    A colour a person removes *is* a thread they chose not to spend, and handing
    it back would re-quantise everything — so the panel's list would rearrange
    itself under the finger that tapped it. Removing removes; the colour slider
    is right next to it for spending the budget elsewhere.
    """
    plain = convert(three_bands(), BASIC, name="bands", drop_background=False)
    cut = convert(
        three_bands(), BASIC, name="bands",
        drop_background=False, remove=["#c8102e"],
    )

    assert cut.settings.colors == plain.settings.colors == 3
    # Every layer that is left is the layer it was, drawn the same way: the only
    # difference between the two documents is the one that was taken out.
    assert [layer.color for layer in cut.best.layers] == [
        layer.color for layer in plain.best.layers
    ]


@needs_tracer
def test_the_background_and_a_hand_picked_colour_go_down_one_path():
    """C1's gate, and the reason it is a small step rather than a feature.

    B8 finds the paper with a corner fill and leaves that entry out; C1 lets a
    person name the entry instead. Asked for the same colour both ways, the two
    produce **the same document, byte for byte** — there is one exclusion in the
    pipeline, not one for the tool and one for the user.

    Two inks rather than three, because B8's *other* half must not be in the
    comparison: with three inks on paper the +1 is earned and the automatic
    route quantises at four entries where the hand route quantises at three, so
    the two documents differ for a reason that has nothing to do with the
    exclusion. Here the extra entry merges back into its neighbour and both
    routes see one palette — which the test asserts before comparing bytes.
    """
    source = two_inks_on_paper()
    auto = convert(source, BASIC, name="two-inks")
    hand = convert(
        source, BASIC, name="two-inks", drop_background=False, remove=["white"]
    )

    paper = [layer for layer in auto.best.layers if layer.reason == "background"]
    assert [layer.color for layer in paper] == ["#ffffff"]
    # The fixture only says anything while both routes see the same palette;
    # B8's +1 is its own decision and is not what is being compared here.
    assert [layer.color for layer in hand.best.layers] == [
        layer.color for layer in auto.best.layers
    ]
    assert hand.svg == auto.svg
    assert hand.best.layers[0].reason == "removed"


@needs_tracer
@pytest.mark.skipif(not renderers(), reason="no renderer installed here")
def test_removing_a_colour_leaves_no_fringe_of_the_layer_beneath():
    """The other half of C1's gate, measured rather than reasoned about.

    Cutting the ``<g>`` out of the finished document would be one line and would
    leave exactly this: every layer stitched earlier was grown a pixel underneath
    this one by B4's trap, and taking the cover away exposes that growth as a
    hairline of the wrong colour round the hole. Re-tracing with the entry
    excluded produces the document that colour was never in.

    The counterfactual — what the same fixture looks like *without* the
    exclusion — is measured at the mechanism in ``test_tracer``: 0 painted
    pixels with it, 70 without, against a rim of 136.
    """
    result = convert(
        three_bands(), BASIC, name="bands",
        drop_background=False, remove=["#c8102e"],
    )
    quantisation = result.best.prepared.quantisation
    index = result.best.removed[0]

    # The layer beneath is stitched and larger, so the trap did have something
    # to grow. Without that, this test would pass with the exclusion removed.
    beneath = result.best.layers[0]
    assert beneath.stitched and beneath.share > quantisation.area(index)

    shot = render(result.svg, width=quantisation.width)
    assert shot is not None
    painted = 0
    hole = 0
    for y in range(shot.height):
        row = (y * quantisation.height // shot.height) * quantisation.width
        for x in range(shot.width):
            if quantisation.indices[row + x * quantisation.width // shot.width] != index:
                continue
            hole += 1
            if shot.pixels[(y * shot.width + x) * 4 + 3] >= 128:
                painted += 1
    assert hole > 1000, "the hole has to be big enough to see a hairline in"
    assert painted == 0, f"{painted} of {hole} pixels where the colour was are painted"


@needs_tracer
def test_a_pick_that_names_nothing_here_removes_nothing_and_says_which():
    """A removal is resolved per attempt, and a retry re-quantises underneath it.

    Removing *something* because it was the nearest thing left is the one way
    this mechanism could delete a colour nobody picked, so the tolerance is a
    colour anyone would call the same one and anything further off is reported.
    """
    result = convert(
        three_bands(), BASIC, name="bands",
        drop_background=False, remove=["#00ff00"],
    )

    assert not result.best.removed
    assert len(parse_svg(result.svg).colors()) == 3
    assert any("is not a colour of this conversion" in n for n in result.best.remove_notes)
    assert [pick.applied for pick in result.best.picks] == [False]


@needs_tracer
def test_the_last_colour_cannot_be_removed():
    """A document with no thread in it is not a conversion."""
    result = convert(
        three_bands(), BASIC, name="bands",
        drop_background=False, remove=["#204aa0", "#ffffff", "#c8102e"],
    )

    assert len(result.best.removed) == 2
    assert any("would leave nothing to stitch" in n for n in result.best.remove_notes)
    assert len(parse_svg(result.svg).colors()) == 1


@needs_tracer
def test_removing_the_colour_the_corners_already_found_says_so():
    """Otherwise the button looks broken: the picture does not change."""
    result = convert(three_inks_on_paper(), BASIC, name="three-inks", remove=["#ffffff"])

    assert not result.best.removed  # it was already out
    assert any("already being left to the fabric" in n for n in result.best.remove_notes)


@needs_tracer
def test_a_removal_is_carried_into_the_retry_rather_than_undone_by_it():
    """The loop turns knobs; it does not put back a colour someone took out."""
    settings = replace(
        initial_settings(STRICT, source_side=256)[0], remove=("#c8102e",)
    )
    proposed, _ = adjust(settings, ["geometry.min_feature_size"], STRICT)
    assert proposed.remove == ("#c8102e",)
    assert proposed.canvas_mm > settings.canvas_mm


def test_a_colour_that_is_not_a_colour_is_a_usage_error(tmp_path, capsys):
    """Found before the tracing rather than after a minute of it."""
    source = tmp_path / "bands.png"
    source.write_bytes(encode_png(three_bands()))

    assert main(["convert", str(source), "--remove", "burgundy-ish"]) == 2
    assert "not a colour" in capsys.readouterr().err


@needs_tracer
def test_the_command_prints_the_colours_that_remove_names(tmp_path, capsys):
    """A flag you cannot use without guessing is a flag nobody can use.

    ``--remove`` names colours out of the palette this image produced, so the
    run prints that palette — without ``-v``, since a list you have to turn on
    is not there when you need it.
    """
    source = tmp_path / "bands.png"
    source.write_bytes(encode_png(three_bands()))

    assert main(["convert", str(source), "-p", "embroidery-basic",
                 "--keep-background"]) == 0
    listed = capsys.readouterr().out
    assert "threads, in the order they are sewn:" in listed
    assert "#c8102e" in listed

    out = tmp_path / "cut.svg"
    assert main([
        "convert", str(source), "-p", "embroidery-basic", "--keep-background",
        "--remove", "#c8102e", "-o", str(out),
    ]) == 0
    assert "you removed it" in capsys.readouterr().out
    assert "#c8102e" not in out.read_text(encoding="utf-8").lower()
