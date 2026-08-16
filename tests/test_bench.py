"""B1: the benchmark — the corpus, the metrics, and the baseline diff."""

import json

import pytest

from svg_embroidery.bench import (
    METRICS,
    BenchError,
    CorpusEntry,
    canvas_and_stroke,
    color_budget,
    compare,
    load_baseline,
    load_corpus,
    measure,
    render_changes,
    render_metric_help,
    render_table,
    run,
    scale_for,
)
from svg_embroidery.cli import main
from svg_embroidery.profiles import load_profile
from svg_embroidery.raster import encode_png
from svg_embroidery.visual import Raster

BASIC = load_profile("embroidery-basic")


def image(width, height, paint):
    pixels = bytearray()
    for y in range(height):
        for x in range(width):
            pixels += bytes(paint(x, y))
    return Raster(width=width, height=height, pixels=bytes(pixels))


WHITE = (255, 255, 255, 255)
BLACK = (26, 26, 26, 255)
RED = (200, 16, 46, 255)


def tiny_corpus(directory, images):
    """Write a corpus of ``{name: raster}`` and its manifest, return the path."""
    directory.mkdir(parents=True, exist_ok=True)
    lines = ["images:"]
    for name, raster in images.items():
        (directory / f"{name}.png").write_bytes(encode_png(raster))
        lines += [
            f"  - name: {name}",
            f"    file: {name}.png",
            "    profile: embroidery-basic",
            "    category: test",
            "    expect: good",
        ]
    (directory / "manifest.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return directory


# -- the corpus manifest -----------------------------------------------------

def test_the_real_corpus_covers_the_range_the_roadmap_names():
    entries = load_corpus()
    assert len(entries) >= 20
    assert {entry.category for entry in entries} >= {
        "logo", "line-art", "scan", "photo", "alpha", "gradient", "junk"
    }
    assert {entry.expect for entry in entries} == {"good", "marginal", "hopeless"}
    for entry in entries:
        assert entry.path.is_file(), f"{entry.name} names a file that is not there"
        assert entry.note, f"{entry.name} should say what it is for"


def test_a_missing_corpus_says_how_to_build_one(tmp_path):
    with pytest.raises(BenchError, match="make_corpus"):
        load_corpus(tmp_path)


def test_a_broken_manifest_is_reported_rather_than_crashed(tmp_path):
    (tmp_path / "manifest.yaml").write_text("images: []\n", encoding="utf-8")
    with pytest.raises(BenchError, match="non-empty list"):
        load_corpus(tmp_path)

    (tmp_path / "manifest.yaml").write_text("images:\n  - name: x\n", encoding="utf-8")
    with pytest.raises(BenchError, match="'name' and 'file'"):
        load_corpus(tmp_path)


# -- the profile decides how the image is measured ---------------------------

def test_the_kernel_asks_the_profile_its_own_question():
    """The bug that made line-art-thick score worse than line-art-thin.

    A feature ``w`` pixels wide survives an opening of radius ``(w - 1) / 2``.
    Halving ``w`` instead builds a kernel nearly twice too wide, and 5px
    strokes get condemned as unstitchable.
    """
    canvas_mm, min_mm = canvas_and_stroke(BASIC)
    assert (canvas_mm, min_mm) == (100.0, 1.5)

    scale = scale_for(BASIC, source_side=256)
    assert scale.radius == 1
    assert scale.kernel_mm == pytest.approx(min_mm, abs=0.01)
    assert scale.faithful


def test_a_source_too_coarse_to_answer_says_so_instead_of_guessing():
    scale = scale_for(BASIC, source_side=32)
    assert scale.work_side == 32
    assert not scale.faithful
    assert scale.kernel_mm > scale.min_mm * 1.5


def test_forcing_a_resolution_overrides_the_profile():
    assert scale_for(BASIC, 256, work_side=64).work_side == 64


def test_the_colour_budget_comes_from_the_profile():
    assert color_budget(BASIC) == 3


# -- measuring ---------------------------------------------------------------

def test_flat_artwork_measures_as_flat_and_loses_nothing(tmp_path):
    logo = image(256, 256, lambda x, y: RED if (x - 128) ** 2 + (y - 128) ** 2 < 6400 else WHITE)
    directory = tiny_corpus(tmp_path / "c", {"logo": logo})
    row = measure(load_corpus(directory)[0])
    assert row.unmeasured == ""
    assert row.flat > 0.9
    assert row.quant < 0.02
    assert row.thin < 0.02
    assert row.res == 200 and row.mm_px == 0.5


def test_a_gradient_measures_as_the_problem_it_is(tmp_path):
    ramp = image(256, 256, lambda x, y: (x, 40, 255 - x, 255))
    directory = tiny_corpus(tmp_path / "c", {"ramp": ramp})
    row = measure(load_corpus(directory)[0])
    assert row.flat == 0.0
    assert row.quant > 0.5, "reducing a ramp to three colours has to cost something"


def test_a_low_resolution_source_leaves_the_thinness_cell_empty(tmp_path):
    icon = image(32, 32, lambda x, y: BLACK if x < 16 else WHITE)
    directory = tiny_corpus(tmp_path / "c", {"icon": icon})
    row = measure(load_corpus(directory)[0])
    assert row.thin is None
    assert row.flat is not None, "the other metrics still work"
    assert any("not measurable" in note for note in row.notes)


def test_an_unreadable_image_costs_a_row_not_the_run(tmp_path, monkeypatch):
    monkeypatch.setenv("SVGEMB_NO_RASTER", "1")
    directory = tmp_path / "c"
    tiny_corpus(directory, {"fine": image(64, 64, lambda x, y: RED)})
    (directory / "photo.jpg").write_bytes(b"nope")
    manifest = directory / "manifest.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + "  - name: photo\n    file: photo.jpg\n    profile: embroidery-basic\n"
          "    category: test\n    expect: good\n",
        encoding="utf-8",
    )
    result = run(load_corpus(directory))
    assert len(result.measured) == 1
    assert len(result.unmeasured) == 1
    assert "Pillow" in result.unmeasured[0].unmeasured
    assert "not measurable here" in render_table(result)


def test_an_unknown_profile_is_a_row_not_a_crash(tmp_path):
    directory = tiny_corpus(tmp_path / "c", {"x": image(32, 32, lambda x, y: RED)})
    entry = CorpusEntry(
        name="x", file="x.png", profile="no-such-profile", category="test",
        expect="good", directory=directory,
    )
    assert measure(entry).unmeasured


# -- what the corpus actually shows ------------------------------------------

@pytest.fixture(scope="module")
def corpus_rows():
    """The whole corpus, measured once — it is the slowest thing in the suite."""
    return {row.name: row for row in run(load_corpus()).rows}


@pytest.mark.slow
def test_the_metrics_separate_drawable_artwork_from_shading(corpus_rows):
    """The claim B1 exists to support: these numbers discriminate.

    Deliberately not a check that every metric agrees with the ``expect``
    column — see the next test for where it does not.
    """
    rows = corpus_rows
    for name in ("logo-two-colour", "logo-three-colour", "monogram", "antialiased-edges"):
        assert rows[name].flat > 0.85, name
        assert rows[name].quant < 0.05, name
    for name in ("photo-portrait", "photo-landscape", "gradient-linear", "gradient-radial"):
        assert rows[name].flat < 0.10, name
        assert rows[name].quant > 0.80, name
    # Reducing five flat colours to three costs real pixels, unlike three to three.
    assert rows["logo-five-colour"].quant > 10 * rows["logo-three-colour"].quant


@pytest.mark.slow
def test_the_corpus_records_the_gap_b3_has_to_close(corpus_rows):
    """A scan of flat artwork is *drawable*, and currently measures as junk.

    Grain means no pixel equals its neighbour, so ``flat`` collapses and the
    quantiser turns speckle into unstitchable detail. That is not a wrong
    measurement — it is an accurate one of an unpreprocessed image, and it is
    the whole reason B3's denoising stage exists. When B3 lands this assertion
    should start failing, which is the point of writing it down.
    """
    rows = corpus_rows
    assert rows["scan-clean"].expect == "good"
    assert rows["scan-clean"].flat < 0.1, "if this now passes, B3 worked — update the test"
    assert rows["scan-clean"].thin > 0.5


# -- the baseline diff, which is the actual deliverable ----------------------

def small_run(tmp_path, color):
    directory = tiny_corpus(
        tmp_path / "c", {"one": image(64, 64, lambda x, y: color if x < 32 else WHITE)}
    )
    return run(load_corpus(directory))


def test_an_unchanged_run_reports_no_change(tmp_path):
    result = small_run(tmp_path, RED)
    assert compare(result.to_dict(), result) == []
    assert "no change" in render_changes([])


def test_a_metric_that_moved_is_graded_better_or_worse(tmp_path):
    result = small_run(tmp_path, RED)
    baseline = result.to_dict()
    baseline["rows"]["one"]["quant"] = 0.5   # we came down to roughly zero: better
    baseline["rows"]["one"]["flat"] = 0.99   # we came down from that: worse
    baseline["rows"]["one"]["colors"] = 7    # no direction: just changed

    verdicts = {change.metric: change.verdict for change in compare(baseline, result)}
    assert verdicts["quant"] == "better"   # quant is lower-is-better
    assert verdicts["flat"] == "worse"     # flat is higher-is-better
    assert verdicts["colors"] == "changed"

    text = render_changes(compare(baseline, result))
    assert "1 better, 1 worse, 1 changed" in text


def test_float_noise_is_not_a_regression(tmp_path):
    result = small_run(tmp_path, RED)
    baseline = result.to_dict()
    baseline["rows"]["one"]["flat"] += 0.0001
    assert compare(baseline, result) == []


def test_added_and_removed_rows_are_reported(tmp_path):
    result = small_run(tmp_path, RED)
    baseline = result.to_dict()
    baseline["rows"]["gone"] = dict(baseline["rows"]["one"], name="gone")
    del baseline["rows"]["one"]

    names = {(change.name, change.after) for change in compare(baseline, result)}
    assert ("one", "new") in names
    assert ("gone", None) in names


def test_a_baseline_from_another_build_refuses_to_be_compared(tmp_path):
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"version": 99, "rows": {}}), encoding="utf-8")
    with pytest.raises(BenchError, match="re-record"):
        load_baseline(path)
    with pytest.raises(BenchError, match="no baseline"):
        load_baseline(tmp_path / "absent.json")


def test_every_metric_declares_which_way_is_up():
    for metric in METRICS:
        assert metric.better in ("lower", "higher", ""), metric.key
        assert metric.about, metric.key
    # The columns that arrive with the tracer are declared now, so the diff can
    # grade them from the first run that fills them in.
    assert {metric.key for metric in METRICS} >= {"paths", "passes"}
    help_text = render_metric_help()
    assert "lower is better" in help_text and "higher is better" in help_text


# -- the command -------------------------------------------------------------

def test_bench_prints_a_row_for_every_image(tmp_path, capsys):
    directory = tiny_corpus(
        tmp_path / "c",
        {"a": image(64, 64, lambda x, y: RED), "b": image(64, 64, lambda x, y: BLACK)},
    )
    assert main(["bench", "--corpus", str(directory), "--no-compare"]) == 0
    out = capsys.readouterr().out
    assert "a" in out and "b" in out
    assert "2 image(s) measured" in out
    for metric in METRICS:
        assert metric.header in out


def test_bench_saves_and_then_compares_against_what_it_saved(tmp_path, capsys):
    directory = tiny_corpus(tmp_path / "c", {"a": image(64, 64, lambda x, y: RED)})
    baseline = tmp_path / "baseline.json"
    assert main(["bench", "--corpus", str(directory), "--baseline", str(baseline), "--save"]) == 0
    assert baseline.is_file()

    capsys.readouterr()
    assert main(["bench", "--corpus", str(directory), "--baseline", str(baseline)]) == 0
    assert "no change against the baseline" in capsys.readouterr().out


def test_a_regression_can_fail_the_command(tmp_path, capsys):
    directory = tiny_corpus(tmp_path / "c", {"a": image(64, 64, lambda x, y: RED)})
    baseline = tmp_path / "baseline.json"
    main(["bench", "--corpus", str(directory), "--baseline", str(baseline), "--save"])

    data = json.loads(baseline.read_text(encoding="utf-8"))
    data["rows"]["a"]["flat"] = 0.5  # we are now above that: better, not worse
    baseline.write_text(json.dumps(data), encoding="utf-8")
    capsys.readouterr()
    assert main([
        "bench", "--corpus", str(directory), "--baseline", str(baseline),
        "--fail-on-regression",
    ]) == 0

    data["rows"]["a"]["edges"] = -1.0  # we are above that too, and lower is better
    baseline.write_text(json.dumps(data), encoding="utf-8")
    assert main([
        "bench", "--corpus", str(directory), "--baseline", str(baseline),
        "--fail-on-regression",
    ]) == 1


def test_a_partial_run_refuses_to_overwrite_the_baseline(tmp_path, capsys):
    directory = tiny_corpus(
        tmp_path / "c",
        {"a": image(64, 64, lambda x, y: RED), "b": image(64, 64, lambda x, y: BLACK)},
    )
    baseline = tmp_path / "baseline.json"
    code = main([
        "bench", "--corpus", str(directory), "--baseline", str(baseline),
        "--only", "a", "--save",
    ])
    assert code == 2
    assert "refusing to save a baseline from a partial run" in capsys.readouterr().err
    assert not baseline.exists()


def test_only_rejects_a_name_that_is_not_in_the_corpus(tmp_path, capsys):
    directory = tiny_corpus(tmp_path / "c", {"a": image(32, 32, lambda x, y: RED)})
    assert main(["bench", "--corpus", str(directory), "--only", "zzz"]) == 2
    assert "no such image" in capsys.readouterr().err


def test_bench_can_explain_itself_without_touching_the_corpus(capsys):
    assert main(["bench", "--explain"]) == 0
    out = capsys.readouterr().out
    assert "colour boundary" in out and "too fine" in out


def test_bench_json_carries_the_rows_and_the_changes(tmp_path, capsys):
    directory = tiny_corpus(tmp_path / "c", {"a": image(64, 64, lambda x, y: RED)})
    baseline = tmp_path / "baseline.json"
    main(["bench", "--corpus", str(directory), "--baseline", str(baseline), "--save"])
    capsys.readouterr()

    main(["bench", "--corpus", str(directory), "--baseline", str(baseline), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["rows"]["a"]["flat"] == 1.0
    assert payload["changes"] == []
    assert "averages" in payload and "created" in payload


def test_a_missing_corpus_is_a_usage_error(tmp_path, capsys):
    assert main(["bench", "--corpus", str(tmp_path / "nowhere")]) == 2
    assert "make_corpus" in capsys.readouterr().err
