"""B5: vector cleanup — the rule, the repairs, and the pass over a trace.

The gate for this step is *the tracer's output passes the target profile after
cleanup*, and the last section here is that gate, run end to end on a generated
image. Everything above it is the machinery, tested on documents small enough
to read.
"""

import math

import pytest

from svg_embroidery import cleanup as b5
from svg_embroidery.checker import Checker
from svg_embroidery.cli import main
from svg_embroidery.document import parse_svg
from svg_embroidery.fixes import FixEngine, Risk, verify_fixer
from svg_embroidery.fixes.cleanup import DropSpecks, ReduceShapeCount, document_shapes
from svg_embroidery.geometry import (
    Contour,
    contour_area,
    simplify_contour,
    simplify_contours,
)
from svg_embroidery.profiles import load_profile
from svg_embroidery.rules.base import create_rule
from svg_embroidery.rules.path_rules import shape_count
from svg_embroidery.tracer import available_backends, default_backend, measure_svg
from svg_embroidery.visual import default_renderer

#: 12 cm across 120 units: one user unit is one millimetre, so every area in
#: these fixtures reads directly in mm².
HEADER = '<svg xmlns="http://www.w3.org/2000/svg" width="12cm" height="12cm" viewBox="0 0 120 120">'


def document(*paths: str) -> str:
    body = "\n".join(f'  <path fill="#123456" d="{d}"/>' for d in paths)
    return f"{HEADER}\n{body}\n</svg>\n"


def square(x: float, y: float, side: float) -> str:
    return f"M{x} {y} L{x + side} {y} L{x + side} {y + side} L{x} {y + side} Z"


def hole(x: float, y: float, side: float) -> str:
    """The same square wound the other way — a hole where it sits inside one."""
    return f"M{x} {y} L{x} {y + side} L{x + side} {y + side} L{x + side} {y} Z"


#: One solid 40 mm square with a 1 mm fleck beside it and a 1 mm hole in it.
SPECKLED = document(square(10, 10, 40) + " " + hole(20, 20, 1), square(70, 70, 1))


# -- the pure-Python measurements -------------------------------------------

def test_area_is_signed_by_winding():
    clockwise = Contour(points=[(0, 0), (0, 10), (10, 10), (10, 0)], closed=True)
    counter = Contour(points=[(0, 0), (10, 0), (10, 10), (0, 10)], closed=True)
    assert contour_area(counter) == 100.0
    assert contour_area(clockwise) == -100.0
    # A repeated closing point is noise, not a fifth corner.
    assert contour_area(Contour(points=[(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)])) == 100.0
    assert contour_area(Contour(points=[(0, 0), (10, 0)])) == 0.0


def test_simplify_keeps_the_shape_and_drops_the_padding():
    """Collinear points cost stitches and describe nothing."""
    padded = Contour(
        points=[(0, 0), (1, 0), (2, 0), (3, 0), (3, 3), (0, 3)], closed=True
    )
    simplified = simplify_contour(padded, 0.01)
    assert len(simplified.points) == 5  # four corners, first repeated to close
    assert abs(contour_area(simplified)) == abs(contour_area(padded))


def test_simplify_leaves_a_curve_a_curve():
    circle = Contour(
        points=[
            (10 * math.cos(t * math.pi / 64), 10 * math.sin(t * math.pi / 64))
            for t in range(128)
        ],
        closed=True,
    )
    simplified = simplify_contour(circle, 0.02)
    assert len(simplified.points) < len(circle.points)
    # Still a circle to within the tolerance it was simplified at.
    assert abs(contour_area(simplified) - contour_area(circle)) < 1.0
    assert simplify_contours([Contour(points=[(0, 0), (1, 1)])], 0.02) == []


# -- the rule ---------------------------------------------------------------

def check(rule_id: str, source: str, **params):
    return list(create_rule(rule_id, params).check(parse_svg(source)))


def test_the_rule_finds_specks_and_holes_and_says_which(make_doc):
    findings = check("geometry.min_area", SPECKLED, min_mm2=2.25)
    messages = " ".join(finding.message for finding in findings)
    assert "1 shape(s)" in messages and "1 hole(s)" in messages
    assert all(finding.data["min_mm2"] == 2.25 for finding in findings)


def test_a_design_of_real_shapes_passes():
    findings = check("geometry.min_area", document(square(10, 10, 40)), min_mm2=2.25)
    assert findings[0].severity.value == "info"
    assert findings[0].data["measured"] is True


def test_it_needs_no_geometry_backend(monkeypatch):
    """The point of measuring area with arithmetic: this runs on a phone."""
    monkeypatch.setenv("SVGEMB_NO_GEOMETRY", "1")
    findings = check("geometry.min_area", SPECKLED, min_mm2=2.25)
    assert findings and findings[0].severity.value == "error"


def test_a_design_drawn_the_other_way_round_is_not_one_big_hole():
    """Winding is read relative to the largest contour, never assumed."""
    reversed_square = hole(10, 10, 40) + " " + square(20, 20, 1)
    shapes = document_shapes(parse_svg(document(reversed_square)))
    assert [shape.hole for shape in shapes] == [False, True]


# -- path.max_count counts shapes, not elements (the B5 measurement fix) -----

def test_shapes_are_counted_as_subpaths():
    """B4's traced hatching put 864 shapes in two elements and passed a limit of 60."""
    packed = document(" ".join(square(x, 10, 5) for x in range(0, 60, 10)))
    assert len(parse_svg(packed).by_tag("path")) == 1
    assert shape_count(parse_svg(packed)) == 6

    findings = check("path.max_count", packed, max_paths=3)
    assert findings[0].severity.value == "warning"
    assert "6 shapes found (in 1 element(s))" in findings[0].message
    assert findings[0].data == {"count": 6, "elements": 1}


def test_the_element_count_is_only_mentioned_when_it_differs():
    one_each = document(square(10, 10, 20), square(50, 50, 20))
    assert "element(s)" not in check("path.max_count", one_each, max_paths=9)[0].message


# -- the repairs ------------------------------------------------------------

def test_dropping_specks_holds_the_fixer_contract():
    result = verify_fixer(
        SPECKLED, "geometry.min_area", profile="embroidery-basic", choice=DropSpecks.OPTION
    )
    assert result.ok, result.summary()
    assert result.risk is Risk.DESTRUCTIVE


#: Eighty 3 mm squares — over ``embroidery-strict``'s limit of sixty, and every
#: one of them big enough to sew, which is what makes the count the only
#: problem with it.
CROWDED = document(
    " ".join(square(x, y, 3) for x in range(5, 105, 10) for y in range(5, 85, 10))
)


def test_reducing_the_shape_count_holds_the_fixer_contract():
    crowded = CROWDED
    result = verify_fixer(
        crowded,
        "path.max_count",
        profile="embroidery-strict",
        choice=ReduceShapeCount.OPTION,
    )
    assert result.ok, result.summary()


def test_surviving_curves_are_kept_exactly_as_they_were_written():
    """The curve that stays is not re-emitted, re-rounded or re-flattened.

    A boolean repair rebuilds a whole element as a polyline; deleting a subpath
    is text surgery, so every number the tracer chose for the shapes that stay
    is still there. That difference is most of why this repair exists.
    """
    curved = "M10 10 C10 60 60 60 60 10 Z"
    source = document(curved + " " + square(100, 100, 1))
    report = fix_with(source, "geometry.min_area", DropSpecks.OPTION)
    assert curved in report.source_after
    assert "M100 100" not in report.source_after


def fix_with(source, rule_id, answer, profile="embroidery-basic"):
    from svg_embroidery.fixes import answer_from_mapping

    engine = FixEngine(
        load_profile(profile),
        allow=list(Risk),
        only={rule_id},
        decide=answer_from_mapping({rule_id: answer}),
    )
    return engine.fix_source(source)


def test_an_element_that_is_all_speck_goes_with_its_empty_group():
    source = (
        f"{HEADER}\n"
        '  <g fill="#123456"><path d="' + square(10, 10, 40) + '"/></g>\n'
        '  <g fill="#abcdef"><path d="' + square(70, 70, 1) + '"/></g>\n'
        "</svg>\n"
    )
    report = fix_with(source, "geometry.min_area", DropSpecks.OPTION)
    assert "#abcdef" not in report.source_after
    assert "nothing in them" in str(report.applied[0])
    assert "#123456" in report.source_after


def test_a_group_that_was_already_empty_is_left_where_it_was():
    """"Removed a group" has to mean this removal emptied it."""
    source = (
        f"{HEADER}\n"
        '  <g id="spare"/>\n'
        '  <g fill="#123456"><path d="' + square(10, 10, 40) + " " + square(70, 70, 1) + '"/></g>\n'
        "</svg>\n"
    )
    report = fix_with(source, "geometry.min_area", DropSpecks.OPTION)
    assert 'id="spare"' in report.source_after
    assert "nothing in them" not in str(report.applied[0])


def test_the_count_repair_declines_rather_than_delete_for_nothing():
    """Most of a document can be shapes it cannot touch."""
    unfilled = " ".join(
        f'<path fill="none" stroke="#000" d="{square(x, 90, 3)}"/>' for x in range(5, 105, 10)
    )
    source = CROWDED.replace("</svg>", f"  {unfilled}\n</svg>")
    rule = create_rule("path.max_count", {"max_paths": 5})
    assert ReduceShapeCount(rule).decision(parse_svg(source)) is None


def test_the_question_prices_itself_before_anything_is_deleted():
    """A destructive repair the user can reach by tapping has to say what it costs."""
    doc = parse_svg(SPECKLED)
    rule = create_rule("geometry.min_area", {"min_mm2": 2.25})
    decision = DropSpecks(rule).decision(doc)
    assert decision.question.startswith("delete 2 shape(s)")
    assert "1.00 mm²" in decision.context and "1 of them are holes" in decision.context
    assert [option.risk for option in decision.options] == [Risk.DESTRUCTIVE]


def test_nothing_is_deleted_without_an_answer():
    report = FixEngine(
        load_profile("embroidery-basic"), allow=list(Risk)
    ).fix_source(SPECKLED)
    assert not report.applied
    assert [decision.rule_id for decision in report.pending] == ["geometry.min_area"]


def test_the_shape_count_question_names_the_biggest_casualty():
    crowded = document(" ".join(square(x, 10, 3) for x in range(0, 100, 10)))
    rule = create_rule("path.max_count", {"max_paths": 5})
    decision = ReduceShapeCount(rule).decision(parse_svg(crowded))
    assert "drop the 5 smallest" in decision.question
    assert "9.00 mm²" in decision.context


# -- the policy -------------------------------------------------------------

def test_cleanup_answers_the_question_the_profile_already_decided():
    result = b5.clean(SPECKLED, load_profile("embroidery-basic"))
    assert result.changed and result.passes
    assert "geometry.min_area" in str(result.fix.applied[0])


def test_cleanup_leaves_the_judgement_to_a_person():
    """Which artwork to sacrifice to a shape count is not the tool's to decide."""
    result = b5.clean(CROWDED, load_profile("embroidery-strict"))
    assert [decision.rule_id for decision in result.fix.pending] == ["path.max_count"]
    assert not result.changed


def test_a_run_that_cannot_be_cleaned_hands_back_what_it_was_given():
    """The engine's verification still applies to a generated document."""
    profile = load_profile("embroidery-basic")
    result = b5.clean(SPECKLED, profile, allow=[])
    assert result.svg == SPECKLED and not result.changed
    assert result.passes is (not b5.check(SPECKLED, profile).errors)


def test_answers_can_be_switched_off_entirely():
    result = b5.clean(SPECKLED, load_profile("embroidery-basic"), answers={})
    assert not result.changed and result.fix.pending


# -- the gate ---------------------------------------------------------------

def trace_a_speckled_logo():
    """A disc on a background, quantised hard enough to leave islands behind.

    Not a hand-written SVG: the point of the gate is that what the *tracer*
    produces passes, and speckle is something a tracer produces rather than
    something anyone draws.
    """
    from tests.test_bench import image

    from svg_embroidery.bench import canvas_and_stroke, color_budget, scale_for
    from svg_embroidery.preprocess import Recipe
    from svg_embroidery.preprocess import run as preprocess_run

    profile = load_profile("embroidery-strict")
    side = 128

    def paint(x, y):
        inside = (x - 64) ** 2 + (y - 64) ** 2 < 30 * 30
        # A scattering of 2×2 flecks of the other colour: exactly what a
        # quantiser leaves when it forces a photograph into two colours, and
        # big enough that B3's denoising does not simply take them away — the
        # point of the gate is what survives preprocessing and gets traced.
        speck = ((x // 2) * 7 + (y // 2) * 13) % 61 == 0 and not inside
        return (20, 20, 20, 255) if inside or speck else (240, 240, 240, 255)

    source = image(side, side, paint)

    scale = scale_for(profile, side)
    cleaned = preprocess_run(source, Recipe(colors=color_budget(profile), radius=scale.radius))
    traced_svg = default_backend().trace(
        cleaned.quantisation, canvas_mm=canvas_and_stroke(profile)[0]
    )
    return profile, traced_svg.svg


@pytest.mark.skipif(not available_backends(), reason="no tracer installed here")
def test_the_gate_a_traced_document_passes_its_profile_after_cleanup():
    profile, svg = trace_a_speckled_logo()
    before = b5.check(svg, profile)
    result = b5.clean(svg, profile)

    assert result.ok, result.notes()
    assert result.passes, [str(finding) for finding in result.after.errors]
    # The cleanup has to have done something worth doing: fewer shapes to sew,
    # and the reason recorded rather than inferred.
    assert measure_svg(result.svg)[0] < measure_svg(svg)[0]
    assert any("min_area" in note for note in result.notes())
    # And it started from a document that genuinely failed, or it proves nothing.
    assert before.errors


@pytest.mark.skipif(not available_backends(), reason="no tracer installed here")
def test_cleanup_does_not_go_behind_the_checkers_back():
    """Whatever cleanup writes, ``svgemb check`` has to agree with."""
    profile, svg = trace_a_speckled_logo()
    result = b5.clean(svg, profile)
    report = Checker(profile).check_source(result.svg)
    assert not report.errors


# -- the column -------------------------------------------------------------

@pytest.mark.skipif(not available_backends(), reason="no tracer installed here")
def test_the_passes_column_is_filled_by_a_trace(tmp_path):
    from tests.test_bench import BLACK, RED, image, tiny_corpus  # noqa: PLC0415

    from svg_embroidery.bench import load_corpus, run

    directory = tiny_corpus(
        tmp_path / "c",
        {"a": image(64, 64, lambda x, y: RED if 16 < x < 48 else BLACK)},
    )
    row = run(load_corpus(directory), backend=default_backend()).rows[0]
    assert row.passes in ("yes",) or row.passes.endswith("err")


@pytest.mark.skipif(not available_backends(), reason="no tracer installed here")
def test_a_baseline_taken_without_cleanup_is_not_diffed(tmp_path):
    from tests.test_bench import RED, image, tiny_corpus  # noqa: PLC0415

    from svg_embroidery.bench import incomparable, load_corpus, run

    directory = tiny_corpus(tmp_path / "c", {"a": image(48, 48, lambda x, y: RED)})
    result = run(load_corpus(directory), backend=default_backend(), cleanup=True)
    reasons = incomparable(dict(result.to_dict(), cleanup=False), result)
    assert reasons and "cleanup" in reasons[0]
    assert incomparable(result.to_dict(), result) == []


@pytest.mark.skipif(not available_backends(), reason="no tracer installed here")
@pytest.mark.skipif(default_renderer() is None, reason="no renderer installed here")
def test_the_command_measures_the_cleaned_document(tmp_path, capsys):
    from tests.test_bench import BLACK, WHITE, image, tiny_corpus  # noqa: PLC0415

    directory = tiny_corpus(
        tmp_path / "c",
        {
            "a": image(
                96, 96,
                lambda x, y: BLACK
                if (x - 48) ** 2 + (y - 48) ** 2 < 400 or (x * 7 + y * 13) % 61 == 0
                else WHITE,
            )
        },
    )
    assert main(["bench", "--corpus", str(directory), "--no-compare", "--cleanup"]) == 0
    out = capsys.readouterr().out
    assert "cleaned up (B5)" in out
