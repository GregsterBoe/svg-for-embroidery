"""B2: suitability triage — the verdict, the reasons, and the corpus gate."""

import json

import pytest

from svg_embroidery.bench import (
    METRIC_BY_KEY,
    Measurement,
    disagreements,
    graded,
    load_corpus,
    measure_file,
    run,
)
from svg_embroidery.cli import main
from svg_embroidery.raster import encode_png
from svg_embroidery.triage import (
    ALL_TOO_FINE,
    FLAT_ENOUGH,
    SOME_COLOUR_LOSS,
    SOME_TOO_FINE,
    SPECKLE_EDGES,
    TONE_LOSS,
    Band,
    assess,
    render_assessment,
    render_json,
    render_summary,
    render_thresholds,
)
from svg_embroidery.visual import Raster

from test_bench import BLACK, RED, WHITE, image, tiny_corpus

#: Any corpus image will do here; it is read for the profile's needle width.
NEEDLE_FIXTURE = load_corpus()[0].path


def row(**values) -> Measurement:
    """A measurement with everything healthy, overridden where the test cares."""
    defaults = dict(
        name="x", category="test", expect="good", profile="embroidery-basic",
        size="256x256", res=200, mm_px=0.5, min_mm=1.5, colors=8, k=3,
        flat=0.95, quant=0.01, edges=0.02, thin=0.001,
    )
    defaults.update(values)
    return Measurement(**defaults)


# -- the bands ---------------------------------------------------------------

def test_flat_artwork_within_its_colour_budget_is_good():
    result = assess(row())
    assert result.verdict is Band.GOOD
    assert result.readings == []      # nothing to say against it
    assert result.abstained == []


def test_the_verdict_is_the_worst_thing_about_the_image():
    """Two marginal readings and one hopeless one is hopeless, not an average."""
    result = assess(row(quant=0.95, thin=0.5, flat=0.1))
    assert result.verdict is Band.HOPELESS
    assert {reading.key for reading in result.readings} >= {"colour loss", "fineness"}
    assert [reading.key for reading in result.deciding] == ["colour loss"]


def test_smooth_shading_is_hopeless_and_says_which_number_decided():
    result = assess(row(colors=39219, quant=1.0, flat=0.0))
    assert result.verdict is Band.HOPELESS
    says = result.deciding[0].says
    assert "3 colours" in says and "100%" in says
    assert "shading" in says


def test_each_ordinary_threshold_costs_exactly_one_band():
    assert assess(row(quant=SOME_COLOUR_LOSS + 0.01)).verdict is Band.MARGINAL
    assert assess(row(thin=SOME_TOO_FINE + 0.01)).verdict is Band.MARGINAL
    assert assess(row(flat=FLAT_ENOUGH - 0.01)).verdict is Band.MARGINAL
    # ...and sitting just inside each one does not.
    assert assess(row(quant=SOME_COLOUR_LOSS - 0.01)).verdict is Band.GOOD
    assert assess(row(thin=SOME_TOO_FINE - 0.001)).verdict is Band.GOOD
    assert assess(row(flat=FLAT_ENOUGH + 0.01)).verdict is Band.GOOD


def test_the_thresholds_are_ordered_the_way_the_bands_are():
    assert SOME_COLOUR_LOSS < TONE_LOSS
    assert SOME_TOO_FINE < ALL_TOO_FINE


# -- speckle, which is the interesting half ----------------------------------

def test_a_speckled_image_stops_colour_loss_and_flatness_from_voting():
    """The judgement B2 rests on.

    Reduce a grainy scan of two-colour artwork to three colours and every grain
    pixel moves, so ``quant`` reports a catastrophe about a design that is two
    flat colours. Left to vote it would call every scan hopeless.
    """
    scan = row(edges=0.65, thin=0.83, quant=0.87, flat=0.0)
    result = assess(scan)
    assert result.verdict is Band.MARGINAL
    assert [reading.key for reading in result.readings] == ["speckle"]
    assert result.abstained_on("colour loss") and result.abstained_on("flatness")
    assert "denoising" in result.deciding[0].says

    # Without the suppression those same numbers would read as hopeless.
    assert assess(row(quant=0.87, flat=0.0, edges=0.02)).verdict is Band.HOPELESS


def test_speckle_with_nothing_wider_than_the_needle_is_hopeless_anyway():
    """Denoising cannot create width, so this verdict survives B3."""
    result = assess(row(edges=0.86, thin=ALL_TOO_FINE + 0.01, quant=0.38))
    assert result.verdict is Band.HOPELESS
    assert "nothing in here wider than the needle" in result.deciding[0].says


def test_the_speckle_threshold_is_what_switches_the_reading():
    assert assess(row(edges=SPECKLE_EDGES + 0.01, quant=0.9)).verdict is Band.MARGINAL
    assert assess(row(edges=SPECKLE_EDGES - 0.01, quant=0.9)).verdict is Band.HOPELESS


def test_the_headline_drops_a_number_the_readings_refused_to_trust():
    """A metric that can't answer the question must not print one — first line included."""
    speckled = assess(row(edges=0.65, thin=0.83, quant=0.87))
    assert "repainted" not in speckled.headline()
    assert assess(row(quant=0.87, edges=0.02)).headline().endswith("is repainted")


# -- what triage cannot measure ----------------------------------------------

def test_a_reading_with_no_number_behind_it_abstains_out_loud():
    """A verdict resting on fewer readings than it looks like is unactionable."""
    result = assess(row(thin=None, notes=["thin: not measurable — 3.12 mm/px is too coarse"]))
    assert result.verdict is Band.GOOD
    assert result.abstained_on("fineness")
    assert "3.12 mm/px" in result.abstained[0].why, "B1 already wrote the reason"
    assert "did not vote" in render_assessment(result)


def test_an_unmeasurable_row_has_no_verdict_rather_than_a_bad_one():
    result = assess(row(unmeasured="photo.jpg: reading '.jpg' needs Pillow"))
    assert result.verdict is None
    assert "Pillow" in render_assessment(result)
    assert result.to_dict()["verdict"] is None


# -- the gate: does it separate the corpus? ----------------------------------

@pytest.mark.slow
def test_triage_separates_the_corpus_into_the_bands_a_human_would():
    """B2's gate, and the one row it misses.

    ``scan-clean`` is a line drawing on paper: a human says *good*, triage says
    *marginal*, because 84% of it is finer than the needle and that 84% is
    grain. Telling the drawing from the paper needs the denoising B3 owes, so
    this is the same gap B1 recorded — pinned here so that when B3 lands, this
    assertion starts failing and the exception gets deleted rather than
    forgotten.
    """
    result = run(load_corpus())
    # Every row this machine could measure gets a verdict — on a bare install
    # the JPEG is a row you don't get, which is not a row triage failed on.
    assert len(graded(result)) == len(result.measured) >= 19
    missed = {row.name: (row.expect, row.verdict) for row in disagreements(result)}
    assert missed == {"scan-clean": ("good", "marginal")}


@pytest.mark.slow
def test_no_image_is_misgraded_by_more_than_one_band():
    """Triage may hedge; it must never call a photograph good, or a logo hopeless."""
    for row in graded(run(load_corpus())):
        distance = abs(Band(row.verdict).rank - Band(row.expect).rank)
        assert distance <= 1, f"{row.name}: expected {row.expect}, got {row.verdict}"


def test_the_bench_table_grades_itself(tmp_path, capsys):
    directory = tiny_corpus(tmp_path / "c", {"flat": image(64, 64, lambda x, y: RED)})
    assert main(["bench", "--corpus", str(directory), "--no-compare"]) == 0
    out = capsys.readouterr().out
    assert "verdict" in out
    assert "triage agrees with 'expect' on 1/1 image(s)" in out


def test_a_disagreement_is_printed_with_the_run_not_hidden_in_a_test(tmp_path, capsys):
    ramp = image(64, 64, lambda x, y: (x * 4 % 256, 40, 255 - x * 4 % 256, 255))
    directory = tiny_corpus(tmp_path / "c", {"ramp": ramp})  # manifest says 'good'
    main(["bench", "--corpus", str(directory), "--no-compare"])
    out = capsys.readouterr().out
    assert "triage agrees with 'expect' on 0/1" in out
    assert "expected good, triage says" in out


def test_the_verdict_is_a_column_the_baseline_can_diff():
    metric = METRIC_BY_KEY["verdict"]
    assert metric.kind == "text"
    assert metric.better == "", "a verdict that moves is news in either direction"


# -- the command -------------------------------------------------------------

def png(tmp_path, name, raster):
    path = tmp_path / name
    path.write_bytes(encode_png(raster))
    return path


def test_assess_grades_one_file_and_exits_on_the_verdict(tmp_path, capsys):
    good = png(tmp_path, "logo.png", image(128, 128, lambda x, y: RED if x < 64 else WHITE))
    assert main(["assess", str(good)]) == 0
    out = capsys.readouterr().out
    assert "GOOD" in out and "logo.png" in out and "embroidery-basic" in out

    ramp = png(tmp_path, "ramp.png", image(128, 128, lambda x, y: (x * 2, 40, 255 - x * 2, 255)))
    assert main(["assess", str(ramp)]) == 1
    assert "HOPELESS" in capsys.readouterr().out


def test_strict_makes_marginal_a_failure_too(tmp_path, capsys):
    # Two flat colours, but a quarter of the image is 1px stripes: too fine.
    striped = image(
        128, 128,
        lambda x, y: BLACK if (x < 64 and y % 2 == 0) else WHITE,
    )
    path = png(tmp_path, "striped.png", striped)
    assert main(["assess", str(path)]) == 0
    assert "MARGINAL" in capsys.readouterr().out
    assert main(["assess", str(path), "--strict"]) == 1


def test_assess_reads_a_whole_directory_and_summarises(tmp_path, capsys):
    png(tmp_path, "a.png", image(96, 96, lambda x, y: RED if x < 48 else WHITE))
    png(tmp_path, "b.png", image(96, 96, lambda x, y: (x * 2, 40, 255 - x * 2, 255)))
    assert main(["assess", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "a.png" in out and "b.png" in out
    assert "good" in out and "hopeless" in out
    assert "1 good, 0 marginal, 1 hopeless." in out


def test_verbose_shows_the_readings_that_did_not_decide(tmp_path, capsys):
    ramp = png(tmp_path, "ramp.png", image(96, 96, lambda x, y: (x * 2, 40, 255 - x * 2, 255)))
    main(["assess", str(ramp), "-v"])
    verbose = capsys.readouterr().out
    main(["assess", str(ramp)])
    assert len(verbose) > len(capsys.readouterr().out)


def test_assess_json_carries_the_verdict_and_the_numbers_behind_it(tmp_path, capsys):
    path = png(tmp_path, "logo.png", image(96, 96, lambda x, y: RED if x < 48 else WHITE))
    main(["assess", str(path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["verdict"] == "good"
    assert payload[0]["measurement"]["flat"] > 0.9
    assert "readings" in payload[0] and "headline" in payload[0]


def test_assess_can_explain_itself_without_being_given_a_file(capsys):
    assert main(["assess", "--explain"]) == 0
    out = capsys.readouterr().out
    assert "grain" in out and "0.40" in out
    assert main(["assess"]) == 2
    assert "at least one image" in capsys.readouterr().err


def test_pointing_it_at_an_svg_names_the_command_that_grades_those(tmp_path, capsys):
    """An SVG is already past the question triage answers."""
    vector = tmp_path / "design.svg"
    vector.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    assert main(["assess", str(vector)]) == 2
    assert "svgemb check" in capsys.readouterr().err


def test_a_missing_file_and_a_bad_profile_are_usage_errors(tmp_path, capsys):
    assert main(["assess", str(tmp_path / "nope.png")]) == 2
    assert "no such file" in capsys.readouterr().err

    path = png(tmp_path, "a.png", image(32, 32, lambda x, y: RED))
    assert main(["assess", str(path), "-p", "no-such-profile"]) == 2
    assert "error" in capsys.readouterr().err


def test_an_image_this_machine_cannot_decode_is_not_a_verdict_of_bad(tmp_path, capsys, monkeypatch):
    """The ground rule: a measurement you can't take is not a failure."""
    monkeypatch.setenv("SVGEMB_NO_RASTER", "1")
    broken = tmp_path / "photo.jpg"
    broken.write_bytes(b"nope")
    assert main(["assess", str(broken)]) == 2
    assert "Pillow" in capsys.readouterr().out

    good = png(tmp_path, "logo.png", image(96, 96, lambda x, y: RED if x < 48 else WHITE))
    # Alongside a readable one, it costs a row rather than the run.
    assert main(["assess", str(good), str(broken)]) == 0


def test_no_color_leaves_no_icons_behind(tmp_path, capsys):
    path = png(tmp_path, "a.png", image(96, 96, lambda x, y: RED if x < 48 else WHITE))
    main(["assess", str(path), "--no-color"])
    out = capsys.readouterr().out
    for icon in ("✅", "⚠️", "❌", "ℹ️", "🖼"):
        assert icon not in out


def test_assess_and_bench_cannot_disagree_about_an_image(tmp_path):
    """One source of truth: both go through the same measurement."""
    directory = tiny_corpus(
        tmp_path / "c", {"a": image(96, 96, lambda x, y: RED if x < 48 else WHITE)}
    )
    corpus_row = run(load_corpus(directory)).rows[0]
    direct = measure_file(directory / "a.png")
    assert direct.verdict == corpus_row.verdict
    assert (direct.flat, direct.quant, direct.edges, direct.thin) == (
        corpus_row.flat, corpus_row.quant, corpus_row.edges, corpus_row.thin
    )


def test_the_reason_quotes_the_width_the_thinness_was_measured_against():
    """``thin`` means nothing without it, so the row carries it and B2 says it."""
    assert measure_file(NEEDLE_FIXTURE).min_mm == 1.5
    assert "2.5 mm" in assess(row(thin=0.5, min_mm=2.5)).deciding[0].says


def test_render_helpers_are_safe_on_the_awkward_cases():
    assert "1 good, 0 marginal, 0 hopeless." in render_summary([assess(row())])
    unmeasured = assess(row(name="a", unmeasured="no reader"))
    assert "not assessed" in render_summary([unmeasured])
    assert json.loads(render_json([unmeasured]))[0]["verdict"] is None
    assert "worst thing about it" in render_thresholds()
