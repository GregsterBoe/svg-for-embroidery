"""A5: the geometry layer, the minimum-feature check and the fixes on top.

Split the way the module is: flattening is pure Python and must work
everywhere, so those tests carry no marker. Everything that offsets or
measures needs a backend and is skipped without one — the same bargain A1 made
with renderers.
"""

import math
from pathlib import Path

import pytest

from svg_embroidery import geometry
from svg_embroidery.checker import Checker
from svg_embroidery.document import parse_svg
from svg_embroidery.findings import Severity
from svg_embroidery.fixes import FixEngine, Risk, verify_fixer, verify_no_op
from svg_embroidery.geometry import (
    Contour,
    contours_to_path_data,
    corner_allowance,
    default_backend,
    flatten_path,
    measure_thinness,
    node_contours,
    node_contours_mm,
    stroke_outline,
    trim_thin_detail,
)
from svg_embroidery.rules import RuleConfigError, create_rule

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
THIN_DETAIL = EXAMPLES / "thin-detail.svg"

needs_backend = pytest.mark.skipif(
    default_backend() is None, reason="no path geometry backend installed"
)

ALL_RISKS = [Risk.SAFE, Risk.LOSSY, Risk.DESTRUCTIVE]


def only(doc, element_id):
    return next(node for node in doc.nodes if node.element_id == element_id)


# -- flattening: pure Python, always available ------------------------------

def test_straight_path_keeps_its_own_corners():
    contours = flatten_path("M0 0 L10 0 L10 10 Z")
    assert len(contours) == 1
    assert contours[0].closed
    assert contours[0].points == [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)]


def test_relative_and_shorthand_commands_track_the_pen():
    contours = flatten_path("m5 5 h10 v10 h-10 z")
    assert contours[0].points == [(5.0, 5.0), (15.0, 5.0), (15.0, 15.0), (5.0, 15.0)]


def test_a_cubic_is_flattened_within_its_tolerance():
    """Checked against the curve itself, not against a previous run."""
    control = ((0.0, 0.0), (0.0, 40.0), (40.0, 40.0), (40.0, 0.0))
    points = flatten_path("M0 0 C0 40 40 40 40 0", tolerance=0.05)[0].points

    def on_curve(t):
        u = 1 - t
        return tuple(
            u ** 3 * control[0][i] + 3 * u * u * t * control[1][i]
            + 3 * u * t * t * control[2][i] + t ** 3 * control[3][i]
            for i in range(2)
        )

    # Every flattened vertex is a point of the real curve, and the chords in
    # between never stray further than the tolerance allowed.
    steps = len(points) - 1
    for index, point in enumerate(points):
        exact = on_curve(index / steps)
        assert math.dist(point, exact) < 1e-9
    for index in range(steps):
        middle = on_curve((index + 0.5) / steps)
        chord = tuple((points[index][i] + points[index + 1][i]) / 2 for i in range(2))
        assert math.dist(middle, chord) < 0.05


def test_a_tighter_tolerance_buys_more_segments():
    coarse = flatten_path("M0 0 C0 40 40 40 40 0", tolerance=1.0)[0]
    fine = flatten_path("M0 0 C0 40 40 40 40 0", tolerance=0.01)[0]
    assert len(fine.points) > len(coarse.points)


def test_smooth_curves_reflect_the_previous_control_point():
    """S with no preceding curve is a straight-ish segment, not a jump."""
    curved = flatten_path("M0 0 C0 10 10 10 10 0 S20 -10 20 0", tolerance=0.05)
    assert curved[0].points[-1] == (20.0, 0.0)
    # The reflection puts the curve above the axis after the first hump.
    assert min(y for _, y in curved[0].points) < 0


def test_an_arc_lands_on_its_endpoint_and_bulges_the_way_the_sweep_says():
    def midpoint(d):
        points = flatten_path(d, tolerance=0.001)[0].points
        assert points[-1] == (20.0, 0.0)
        return min(points, key=lambda point: abs(point[0] - 10))

    # SVG's y axis points down, so the "positive angle" sweep of 1 puts the
    # bulge of a semicircle above the chord, and a sweep of 0 below it.
    assert midpoint("M0 0 A10 10 0 0 1 20 0") == pytest.approx((10.0, -10.0), abs=0.01)
    assert midpoint("M0 0 A10 10 0 0 0 20 0") == pytest.approx((10.0, 10.0), abs=0.01)


def test_a_zero_radius_arc_degenerates_to_a_line():
    points = flatten_path("M0 0 A0 0 0 0 1 20 0")[0].points
    assert points == [(0.0, 0.0), (20.0, 0.0)]


def test_subpaths_are_separated_and_their_closure_recorded():
    contours = flatten_path("M0 0 L10 0 L10 10 Z M20 20 L30 20 L30 30")
    assert [c.closed for c in contours] == [True, False]


def test_a_malformed_path_stops_rather_than_guessing():
    assert flatten_path("M10") == []
    assert flatten_path("") == []


# -- shapes other than <path> -----------------------------------------------

def test_basic_shapes_become_contours(make_doc):
    doc = make_doc(
        '<rect id="r" x="1" y="2" width="10" height="4"/>'
        '<circle id="c" cx="0" cy="0" r="5"/>'
        '<ellipse id="e" cx="0" cy="0" rx="8" ry="4"/>'
        '<polygon id="p" points="0,0 10,0 10,10"/>'
        '<polyline id="pl" points="0,0 10,0"/>'
        '<line id="l" x1="0" y1="0" x2="10" y2="0"/>'
    )
    assert node_contours(only(doc, "r"))[0].points == [
        (1.0, 2.0), (11.0, 2.0), (11.0, 6.0), (1.0, 6.0)
    ]
    circle = node_contours(only(doc, "c"), tolerance=0.01)[0]
    assert circle.closed and all(abs(math.hypot(x, y) - 5) < 1e-9 for x, y in circle.points)
    ellipse = node_contours(only(doc, "e"), tolerance=0.01)[0]
    assert max(x for x, _ in ellipse.points) == pytest.approx(8.0)
    assert max(y for _, y in ellipse.points) == pytest.approx(4.0)
    assert node_contours(only(doc, "p"))[0].closed
    assert not node_contours(only(doc, "pl"))[0].closed
    assert node_contours(only(doc, "l"))[0].points == [(0.0, 0.0), (10.0, 0.0)]


def test_a_rounded_rect_is_rounded_by_exactly_its_radius(make_doc):
    doc = make_doc('<rect id="r" x="0" y="0" width="20" height="10" rx="3"/>')
    points = node_contours(only(doc, "r"), tolerance=0.001)[0].points

    assert all(-1e-9 <= x <= 20 + 1e-9 and -1e-9 <= y <= 10 + 1e-9 for x, y in points)
    # The outline's closest approach to the sharp corner is r(√2 − 1): the arc
    # is centred at (r, r) with radius r, so the corner sits that far outside.
    # A sharp corner would put a vertex at zero; the slack is the flattening.
    closest = min(math.dist(point, (0.0, 0.0)) for point in points)
    assert closest == pytest.approx(3 * (math.sqrt(2) - 1), abs=0.005)


def test_a_rect_radius_is_clamped_to_half_the_side(make_doc):
    """An rx wider than the rect is legal input and means "fully rounded"."""
    doc = make_doc('<rect id="r" x="0" y="0" width="20" height="10" rx="50"/>')
    points = node_contours(only(doc, "r"), tolerance=0.01)[0].points
    assert all(-1e-9 <= x <= 20 + 1e-9 and -1e-9 <= y <= 10 + 1e-9 for x, y in points)


def test_shapes_with_no_knowable_outline_return_nothing(make_doc):
    doc = make_doc('<text id="t" x="0" y="0">hi</text><image id="i" href="a.png"/>')
    assert node_contours(only(doc, "t")) == []
    assert node_contours(only(doc, "i")) == []


def test_millimetres_account_for_transforms_and_the_viewbox(make_doc):
    """The whole point of carrying the matrix: a scaled shape measures bigger."""
    doc = make_doc(
        '<rect id="plain" x="0" y="0" width="10" height="10"/>'
        '<g transform="translate(5 0) scale(3)">'
        '<rect id="scaled" x="0" y="0" width="10" height="10"/></g>',
        attrs='width="24cm" height="12cm" viewBox="0 0 120 60"',
    )
    assert doc.unit_scale == pytest.approx(2.0)  # 240 mm over 120 units

    plain = node_contours_mm(only(doc, "plain"), doc.unit_scale)[0].points
    assert max(x for x, _ in plain) == pytest.approx(20.0)

    scaled = node_contours_mm(only(doc, "scaled"), doc.unit_scale)[0].points
    assert min(x for x, _ in scaled) == pytest.approx(10.0)   # translate(5) * 2 mm
    assert max(x for x, _ in scaled) == pytest.approx(70.0)   # + 10 * 3 * 2 mm


def test_contours_round_trip_through_a_path_attribute():
    d = contours_to_path_data([Contour([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)], True)])
    assert d == "M 0 0 L 10 0 L 10 10 Z"
    assert flatten_path(d)[0].points == [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)]


# -- the corner allowance: also pure Python ---------------------------------

def test_a_square_is_allowed_exactly_what_its_corners_cost():
    """Four right angles lose r²(4−π)/4 each — hand-computed, not observed."""
    square = [Contour([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)], True)]
    radius = 0.75
    expected = 4 * radius ** 2 * (1 - math.pi / 4)
    assert corner_allowance(square, radius * 2) == pytest.approx(expected)


def test_a_smooth_curve_is_allowed_almost_nothing():
    circle = flatten_path("M10 0 A10 10 0 1 1 -10 0 A10 10 0 1 1 10 0", tolerance=0.001)
    assert corner_allowance(circle, 1.5) < 0.01


@needs_backend
def test_the_allowance_matches_what_the_opening_actually_costs():
    square = [Contour([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)], True)]
    thinness = measure_thinness(square, 1.5)
    assert thinness.lost_area == pytest.approx(thinness.corner_allowance, rel=0.01)
    assert thinness.excess_ratio < 1e-3  # nothing is actually thin


# -- offsetting -------------------------------------------------------------

@needs_backend
def test_outlining_a_square_matches_the_hand_computed_area():
    """A 100 wide square stroked at 2 is the 102 square minus the 98 square."""
    contours = flatten_path("M0 0 H100 V100 H0 Z")
    outline = stroke_outline(contours, 2.0, cap="butt", join="miter")

    backend = default_backend()
    assert backend.area(backend.polygon(outline)) == pytest.approx(102 ** 2 - 98 ** 2)
    outer = max(x for contour in outline for x, _ in contour.points)
    inner = min(
        max(x for x, _ in contour.points) for contour in outline
    )
    assert outer == pytest.approx(101.0)  # centred on the path: 100 + width/2
    assert inner == pytest.approx(99.0)


@needs_backend
def test_a_butt_cap_stops_at_the_end_and_a_square_cap_runs_past_it():
    line = [Contour([(0.0, 0.0), (10.0, 0.0)], False)]
    backend = default_backend()
    butt = backend.area(backend.polygon(stroke_outline(line, 2.0, cap="butt")))
    square = backend.area(backend.polygon(stroke_outline(line, 2.0, cap="square")))

    assert butt == pytest.approx(10 * 2)               # length x width
    assert square == pytest.approx(10 * 2 + 2 * 2)     # + a 1x2 block at each end


@needs_backend
def test_offsetting_is_unavailable_rather_than_wrong_without_a_backend(monkeypatch):
    monkeypatch.setattr(geometry, "available_backends", lambda: [])
    assert geometry.default_backend() is None
    assert stroke_outline([Contour([(0.0, 0.0), (10.0, 0.0)], False)], 2.0) is None


def test_the_env_var_forces_the_degraded_path(monkeypatch):
    monkeypatch.setenv(geometry.DISABLE_ENV_VAR, "1")
    assert geometry.available_backends() == []
    assert geometry.default_backend() is None


# -- measuring thinness -----------------------------------------------------

@needs_backend
def test_a_hairline_loses_everything_and_a_block_loses_nothing():
    hairline = [Contour([(0.0, 0.0), (50.0, 0.0), (50.0, 0.4), (0.0, 0.4)], True)]
    block = [Contour([(0.0, 0.0), (50.0, 0.0), (50.0, 50.0), (0.0, 50.0)], True)]

    assert measure_thinness(hairline, 1.5).vanishes
    assert not measure_thinness(block, 1.5).vanishes
    assert measure_thinness(block, 1.5).excess_ratio < 1e-3


@needs_backend
def test_a_shape_that_encloses_no_area_is_not_measured():
    assert measure_thinness([Contour([(0.0, 0.0), (10.0, 0.0)], False)], 1.5) is None


@needs_backend
def test_a_stroke_counts_towards_what_gets_painted():
    """A hairline carrying a fat stroke is not a thin feature."""
    hairline = [Contour([(0.0, 0.0), (50.0, 0.0), (50.0, 0.2), (0.0, 0.2)], True)]
    assert measure_thinness(hairline, 1.5).vanishes
    assert not measure_thinness(hairline, 1.5, stroke_width=3.0).vanishes


@needs_backend
def test_trimming_cuts_the_spike_and_leaves_the_corners_where_they_were():
    spike = flatten_path("M0 0 L20 0 L20 9.8 L80 10 L20 10.2 L20 20 L0 20 Z")
    kept = trim_thin_detail(spike, 1.5)
    points = [point for contour in kept for point in contour.points]

    # every corner of the block survives untouched...
    for corner in ((0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)):
        assert any(math.dist(point, corner) < 1e-6 for point in points)
    # ...and the spike is gone but for a stub of at most half the width.
    assert max(x for x, _ in points) < 20 + 1.5


# -- the rule ---------------------------------------------------------------

def check(doc, **params):
    return list(create_rule("geometry.min_feature_size", params).check(doc))


def errors(findings):
    return [f for f in findings if f.severity is Severity.ERROR]


def test_the_rule_rejects_nonsense_parameters():
    with pytest.raises(RuleConfigError):
        create_rule("geometry.min_feature_size", {"min_mm": 0})
    with pytest.raises(RuleConfigError):
        create_rule("geometry.min_feature_size", {"tolerance": 1.5})


@needs_backend
def test_the_check_finds_every_defect_in_the_purpose_built_file():
    """The A5 gate. Every other check in the profile passes on this file."""
    report = Checker.from_profile_name("embroidery-strict").check_file(THIN_DETAIL)

    assert {finding.rule_id for finding in report.errors} == {"geometry.min_feature_size"}
    assert [finding.location for finding in report.errors] == [
        "/svg/g[1]/rect[3]",   # hairline
        "/svg/g[1]/path[1]",   # spike
        "/svg/g[1]/path[2]",   # waist
    ]
    assert "throughout" in report.errors[0].message  # the hairline vanishes entirely
    assert report.errors[1].data["excess_ratio"] > 0.01


@needs_backend
def test_small_and_round_shapes_are_not_false_positives():
    """Without the corner allowance the 3 mm square fails on its own corners."""
    doc = parse_svg(THIN_DETAIL.read_text(encoding="utf-8"))
    reported = {finding.location for finding in errors(check(doc))}
    assert "/svg/g[1]/rect[2]" not in reported    # the 3 mm square
    assert "/svg/g[1]/circle[1]" not in reported  # the disc


@needs_backend
def test_unfilled_shapes_are_the_stroke_rule_s_business(make_doc):
    doc = make_doc(
        '<path d="M10 10 L100 10 L100 10.2 L10 10.2 Z" fill="none" '
        'stroke="#000" stroke-width="0.1"/>'
    )
    assert not errors(check(doc))


def test_without_a_backend_the_rule_measures_nothing_and_says_so(make_doc, monkeypatch):
    monkeypatch.setenv(geometry.DISABLE_ENV_VAR, "1")
    doc = make_doc('<rect x="0" y="0" width="100" height="0.2" fill="#000"/>')
    findings = check(doc)

    assert not errors(findings)
    assert findings[0].severity is Severity.INFO
    assert findings[0].data["measured"] is False
    assert "svg-for-embroidery[geometry]" in findings[0].message


def test_a_verdict_never_depends_on_what_is_installed(monkeypatch):
    """The degraded path may lose a measurement; it may not fail a good file."""
    source = (EXAMPLES / "good-design.svg").read_text(encoding="utf-8")
    checker = Checker.from_profile_name("plotter-vinyl")
    with_backend = checker.check_source(source)

    monkeypatch.setenv(geometry.DISABLE_ENV_VAR, "1")
    without = checker.check_source(source)

    assert {f.rule_id for f in without.errors} == {f.rule_id for f in with_backend.errors}
    assert without.passed(strict=True) == with_backend.passed(strict=True)


# -- the fixers -------------------------------------------------------------

THIN_STROKE = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="12cm" height="12cm" viewBox="0 0 120 120">\n'
    '  <g id="a" fill="none" stroke="#000000" stroke-width="0.4">\n'
    '    <path d="M10 10 L110 10 L110 110 L10 110 Z"/>\n'
    "  </g>\n</svg>\n"
)

FAT_STROKE = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="12cm" height="12cm" viewBox="0 0 120 120">\n'
    '  <g id="a">\n'
    '    <path d="M20 20 L100 20 L100 100 L20 100 Z" fill="none" stroke="#000000" '
    'stroke-width="4"/>\n'
    "  </g>\n</svg>\n"
)


def test_widening_a_stroke_needs_no_backend(monkeypatch):
    monkeypatch.setenv(geometry.DISABLE_ENV_VAR, "1")
    result = verify_fixer(THIN_STROKE, "stroke.min_width", risk=Risk.LOSSY)
    assert result.ok, result.summary()


def test_widening_hits_the_profile_s_number_exactly():
    report = FixEngine.from_profile_name(
        "embroidery-basic", allow=[Risk.SAFE, Risk.LOSSY]
    ).fix_source(THIN_STROKE)

    assert 'stroke-width="1.5"' in report.source_after
    assert "stroke.min_width" in report.fixed_rules
    # A one-line diff: the verbatim spans keep the rest of the file untouched.
    added = [line for line in report.diff().splitlines() if line.startswith("+")]
    assert len(added) == 2  # the "+++" header and the single changed line


def test_widening_writes_into_the_style_the_element_already_has():
    """An element's own style beats its own attribute, so the attribute would
    be written and then ignored — the fix would silently do nothing."""
    styled = THIN_STROKE.replace(
        '<path d="M10 10 L110 10 L110 110 L10 110 Z"/>',
        '<path style="stroke:#000000;stroke-width:0.4" '
        'd="M10 10 L110 10 L110 110 L10 110 Z"/>',
    )
    report = FixEngine.from_profile_name(
        "embroidery-basic", allow=[Risk.SAFE, Risk.LOSSY]
    ).fix_source(styled)

    assert "stroke-width:1.5" in report.source_after
    assert 'stroke-width="1.5"' not in report.source_after
    assert "stroke.min_width" in report.fixed_rules


def test_widening_an_inherited_width_lands_on_the_shape_itself():
    """The group's style is inherited, which any declaration on the child
    outranks — so the child is where the repair belongs."""
    report = FixEngine.from_profile_name(
        "embroidery-basic", allow=[Risk.SAFE, Risk.LOSSY]
    ).fix_source(THIN_STROKE)

    assert 'stroke-width="0.4"' in report.source_after  # the group is untouched
    assert '<path d="M10 10 L110 10 L110 110 L10 110 Z" stroke-width="1.5"/>' \
        in report.source_after


def test_widening_keeps_the_unit_the_design_uses():
    in_mm = THIN_STROKE.replace('stroke-width="0.4"', 'stroke-width="0.4mm"')
    report = FixEngine.from_profile_name(
        "embroidery-basic", allow=[Risk.SAFE, Risk.LOSSY]
    ).fix_source(in_mm)
    assert 'stroke-width="1.5mm"' in report.source_after


def test_widening_declines_a_width_it_cannot_resolve():
    relative = THIN_STROKE.replace('stroke-width="0.4"', 'stroke-width="2%"')
    report = FixEngine.from_profile_name(
        "embroidery-basic", allow=[Risk.SAFE, Risk.LOSSY]
    ).fix_source(relative)
    assert not report.changed


@needs_backend
def test_stroke_to_outline_satisfies_the_contract():
    result = verify_fixer(
        FAT_STROKE, "stroke.forbidden", profile="plotter-vinyl", risk=Risk.LOSSY
    )
    assert result.ok, result.summary()


@needs_backend
def test_the_outline_is_the_shape_the_stroke_was_painting():
    report = FixEngine.from_profile_name(
        "plotter-vinyl", allow=[Risk.SAFE, Risk.LOSSY], only={"stroke.forbidden"}
    ).fix_source(FAT_STROKE)

    # An 80 mm box stroked at 4 mm: outer ring 18..102, inner ring 22..98.
    assert 'd="M 18 18 L 102 18 L 102 102 L 18 102 Z M 22 22 L 22 98 L 98 98 L 98 22 Z"' \
        in report.source_after
    # The stroke is gone, not merely overpainted: no colour, no width left.
    assert 'stroke="#000000"' not in report.source_after
    assert "stroke-width" not in report.source_after
    if report.visual is not None:
        assert report.visual.identical, "outlining a stroke should not move a pixel"


@needs_backend
def test_a_stroked_shape_that_is_also_filled_keeps_both():
    filled = FAT_STROKE.replace('fill="none"', 'fill="#ffffff"')
    report = FixEngine.from_profile_name(
        "plotter-vinyl", allow=[Risk.SAFE, Risk.LOSSY], only={"stroke.forbidden"}
    ).fix_source(filled)

    assert report.source_after.count("<path") == 2      # the fill, then the outline
    assert 'fill="#ffffff"' in report.source_after
    assert report.source_after.index('fill="#ffffff"') < report.source_after.index(
        'fill="#000000"'
    )  # a stroke paints over its own fill


@needs_backend
def test_a_gradient_stroke_is_declined_with_a_reason():
    gradient = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="12cm" height="12cm" '
        'viewBox="0 0 120 120"><defs><linearGradient id="g">'
        '<stop offset="0" stop-color="#000"/></linearGradient></defs>'
        '<path d="M20 20 L100 20 L100 100 Z" fill="none" stroke="url(#g)" '
        'stroke-width="4"/></svg>'
    )
    report = FixEngine.from_profile_name(
        "plotter-vinyl", allow=[Risk.SAFE, Risk.LOSSY], only={"stroke.forbidden"}
    ).fix_source(gradient)

    reasons = [skip.reason for skip in report.skipped if skip.rule_id == "stroke.forbidden"]
    assert reasons and "paint server" in reasons[0]


def test_outlining_declines_with_an_install_hint_when_it_cannot_run(monkeypatch):
    monkeypatch.setenv(geometry.DISABLE_ENV_VAR, "1")
    report = FixEngine.from_profile_name(
        "plotter-vinyl", allow=[Risk.SAFE, Risk.LOSSY]
    ).fix_source(FAT_STROKE)

    reasons = [skip.reason for skip in report.skipped if skip.rule_id == "stroke.forbidden"]
    assert reasons and "svg-for-embroidery[geometry]" in reasons[0]
    assert not report.changed


@needs_backend
def test_removing_thin_detail_satisfies_the_contract():
    result = verify_fixer(
        THIN_DETAIL.read_text(encoding="utf-8"),
        "geometry.min_feature_size",
        profile="embroidery-strict",
        risk=Risk.DESTRUCTIVE,
    )
    assert result.ok, result.summary()


@needs_backend
def test_removing_thin_detail_drops_hairlines_and_trims_spikes():
    report = FixEngine.from_profile_name(
        "embroidery-strict", allow=ALL_RISKS
    ).fix_source(THIN_DETAIL.read_text(encoding="utf-8"))

    assert 'id="hairline"' not in report.source_after   # nothing of it survived
    assert 'id="spike"' in report.source_after          # its block did
    assert "L 100 65" not in report.source_after        # the spike itself did not
    assert 'id="solid"' in report.source_after          # untouched shapes are untouched
    assert 'x="8" y="8" width="30" height="30"' in report.source_after
    assert "geometry.min_feature_size" in report.fixed_rules


@needs_backend
def test_removing_thin_detail_stays_out_of_a_lossy_run():
    report = FixEngine.from_profile_name(
        "embroidery-strict", allow=[Risk.SAFE, Risk.LOSSY]
    ).fix_source(THIN_DETAIL.read_text(encoding="utf-8"))

    assert not report.changed
    reasons = [s.reason for s in report.skipped if s.rule_id == "geometry.min_feature_size"]
    assert any("--allow destructive" in reason for reason in reasons)


@needs_backend
def test_a_stroked_shape_is_left_for_the_outline_fixer_first():
    stroked = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="12cm" height="12cm" '
        'viewBox="0 0 120 120"><g id="a">'
        '<rect x="10" y="10" width="100" height="0.4" fill="#000" stroke="#000" '
        'stroke-width="0.1"/></g></svg>'
    )
    report = FixEngine.from_profile_name(
        "embroidery-strict", allow=ALL_RISKS, only={"geometry.min_feature_size"}
    ).fix_source(stroked)

    reasons = [s.reason for s in report.skipped if s.rule_id == "geometry.min_feature_size"]
    assert reasons and "stroked" in reasons[0]


def test_a_run_that_fixes_nothing_still_returns_the_file_byte_for_byte():
    assert verify_no_op(THIN_DETAIL.read_text(encoding="utf-8"), profile="embroidery-strict")
