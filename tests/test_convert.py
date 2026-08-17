"""B6: the conversion loop — the knobs, the order they are tried, and the gate.

The gate for this step is *most of the images a human called convertible come
out passing their profile, unattended*. The corpus answers that in one command
(``svgemb bench -p embroidery-strict --convert``); what is tested here is the
machinery underneath it, plus the same gate in miniature on a purpose-built
image — one whose detail is too fine at the smallest size the profile allows
and stitchable at a size it also allows.
"""

import pytest

from svg_embroidery.checker import Checker
from svg_embroidery.cli import main
from svg_embroidery.convert import (
    CANVAS_STEP,
    MIN_COLORS,
    ConvertError,
    Settings,
    adjust,
    canvas_limits,
    convert,
    initial_settings,
    kernel_radius,
    min_area_mm2,
    prepare,
    render_conversion,
    working_side,
)
from svg_embroidery.profiles import load_profile
from svg_embroidery.raster import encode_png
from svg_embroidery.tracer import DISABLE_ENV_VAR, available_backends
from svg_embroidery.visual import Raster

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
