"""A4: fixes that change the picture on purpose."""

import pytest

from svg_embroidery.checker import Checker
from svg_embroidery.colors import (
    blend_over,
    color_distance,
    nearest_color,
    reduce_palette,
    srgb_to_lab,
)
from svg_embroidery.fixes import FixEngine, Risk, available_fixers, verify_fixer
from svg_embroidery.fixes._path import iter_commands, subpaths
from svg_embroidery.visual import default_renderer

LOSSY = [Risk.SAFE, Risk.LOSSY]

FOUR_COLOURS = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="12cm" height="12cm" viewBox="0 0 120 120">\n'
    '  <g id="a"><path d="M0 0 L50 0 L50 50 Z" fill="#111111"/>'
    '<path d="M50 0 L100 0 L100 50 Z" fill="#141414"/>'
    '<path d="M0 50 L50 50 L50 100 Z" fill="#c8102e"/>'
    '<path d="M50 50 L100 50 L100 100 Z" fill="#ffffff"/></g>\n</svg>\n'
)

FADED = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="12cm" height="12cm" viewBox="0 0 120 120">\n'
    '  <g id="a"><path d="M10 10 L110 10 L110 110 L10 110 Z" fill="#ff0000" '
    'fill-opacity="0.5"/></g>\n</svg>\n'
)

NEAR_MISS = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="12cm" height="12cm" viewBox="0 0 120 120">\n'
    '  <g id="a" fill="none" stroke="#000000" stroke-width="2">\n'
    '    <path d="M10 10 L110 10 L110 110 L10 110 L10 10.5"/>\n'
    "  </g>\n</svg>\n"
)

WIDE_GAP = NEAR_MISS.replace('L10 110 L10 10.5', 'L10 110')


def fix(source, profile="embroidery-basic", allow=LOSSY, **kwargs):
    return FixEngine.from_profile_name(profile, allow=allow, **kwargs).fix_source(source)


needs_renderer = pytest.mark.skipif(
    default_renderer() is None, reason="no SVG renderer installed"
)


# -- perceptual colour ------------------------------------------------------

def test_lab_endpoints():
    assert srgb_to_lab("#ffffff")[0] == pytest.approx(100, abs=0.1)
    assert srgb_to_lab("#000000")[0] == pytest.approx(0, abs=0.1)


def test_equal_rgb_steps_are_not_equal_to_the_eye():
    """Why Lab: the same numeric step in RGB is a different-sized change."""
    grey_step = color_distance("#000000", "#0a0a0a")   # +10 on all channels
    green_step = color_distance("#000000", "#000a00")  # +10 on one channel

    assert green_step > 1.5 * grey_step
    # An RGB-based merge would treat these as interchangeable; Lab does not.


def test_nearest_color_is_deterministic():
    assert nearest_color("#ee0000", ["#000000", "#c8102e", "#ffffff"]) == "#c8102e"
    assert nearest_color("#123456", []) is None


def test_blend_over_matches_what_the_page_shows():
    assert blend_over("#ff0000", 0.5) == "#ff8080"
    assert blend_over("#000000", 1.0) == "#000000"
    assert blend_over("#000000", 0.0) == "#ffffff"


def test_reduce_palette_keeps_real_colours_and_the_popular_ones():
    weights = {"#111111": 5.0, "#141414": 1.0, "#c8102e": 3.0, "#ffffff": 2.0}
    mapping = reduce_palette(weights, 3)

    assert mapping["#141414"] == "#111111"  # near-duplicate gives way
    assert set(mapping.values()) <= set(weights)  # no invented shades
    assert len(set(mapping.values())) == 3


def test_reduce_palette_is_stable():
    weights = {"#111111": 1.0, "#121212": 1.0, "#131313": 1.0, "#ffffff": 1.0}
    assert reduce_palette(weights, 2) == reduce_palette(weights, 2)


def test_reduce_palette_leaves_small_palettes_alone():
    weights = {"#000000": 1.0, "#ffffff": 1.0}
    assert reduce_palette(weights, 5) == {"#000000": "#000000", "#ffffff": "#ffffff"}


# -- several fixers per rule ------------------------------------------------

def test_a_rule_can_have_a_fix_at_every_risk_level():
    """``path.closed`` ended up with all three, and they escalate cleanly.

    Safe closes what the fill already renders shut, lossy closes a gap small
    enough to be a drawing slip, and destructive closes one wide enough that it
    has to ask first (A7).
    """
    from svg_embroidery.fixes import fixer_classes_for

    risks = [fixer.risk for fixer in fixer_classes_for("path.closed")]
    assert risks == [Risk.SAFE, Risk.LOSSY, Risk.DESTRUCTIVE]  # safest first


def test_the_destructive_tier_is_exactly_the_repairs_that_lose_artwork():
    """Every one of them removes something the designer drew, and no other does.

    A5 opened the tier with "cut detail too fine to stitch". A7 added two more,
    both of which ask before running: deleting a forbidden element that is on
    the canvas, and closing a gap wide enough that the segment is invented. B5
    added the two that delete a whole shape rather than part of one.
    """
    destructive = {
        fixer.rule_id for fixer in available_fixers() if fixer.risk is Risk.DESTRUCTIVE
    }
    assert destructive == {
        "element.forbidden",
        "geometry.min_area",
        "geometry.min_feature_size",
        "path.closed",
        "path.max_count",
    }


def test_lossy_fixes_stay_out_of_a_default_run():
    report = FixEngine.from_profile_name("embroidery-basic").fix_source(FOUR_COLOURS)
    assert "color.max_count" not in [fix.rule_id for fix in report.applied]
    assert any("needs --allow lossy" in skip.reason for skip in report.skipped)


# -- colour reduction -------------------------------------------------------

def test_quantise_merges_the_near_duplicate():
    report = fix(FOUR_COLOURS)
    assert "#141414" not in report.source_after
    assert "#c8102e" in report.source_after  # a distinct colour survives
    assert "color.max_count" in report.fixed_rules


def test_quantise_reports_every_substitution():
    report = fix(FOUR_COLOURS)
    changes = [str(change) for fixed in report.applied for change in fixed.changes]
    assert any("#141414 -> #111111" in change for change in changes)


def test_quantise_satisfies_the_contract():
    result = verify_fixer(FOUR_COLOURS, "color.max_count", risk=Risk.LOSSY)
    assert result.ok, result.summary()


def test_palette_remap_snaps_to_stocked_thread():
    # example-shop stocks black, white, red, green and amber. This blue is not
    # one of them, and red is the nearest thing on the shelf to this pink.
    off_palette = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="12cm" height="12cm" '
        'viewBox="0 0 120 120"><g id="a">'
        '<path d="M0 0 L50 0 L50 50 Z" fill="#000000"/>'
        '<path d="M50 0 L100 0 L100 50 Z" fill="#e33b52"/>'
        "</g></svg>"
    )
    report = fix(off_palette, profile="example-shop")

    assert "color.allowed_palette" in report.fixed_rules
    assert "#c8102e" in report.source_after
    after = Checker.from_profile_name("example-shop").check_source(report.source_after)
    assert not any(f.rule_id == "color.allowed_palette" for f in after.errors)


# -- transparency -----------------------------------------------------------

@needs_renderer
def test_flattening_transparency_looks_the_same_on_the_page():
    """50% red on white *is* #ff8080 — the flattened file must render identically."""
    report = fix(FADED)
    assert "#ff8080" in report.source_after
    assert "fill-opacity" not in report.source_after
    assert report.visual.identical


def test_flattening_satisfies_the_contract():
    result = verify_fixer(FADED, "color.no_transparency", risk=Risk.LOSSY)
    assert result.ok, result.summary()


def test_group_opacity_is_declined_with_a_reason():
    grouped = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="12cm" height="12cm" '
        'viewBox="0 0 120 120"><g id="a" opacity="0.4" fill="#000">'
        '<path d="M10 10 L110 10 L110 110 Z"/></g></svg>'
    )
    report = fix(grouped)
    reasons = [s.reason for s in report.skipped if s.rule_id == "color.no_transparency"]
    assert reasons and "group" in reasons[0]
    assert 'opacity="0.4"' in report.source_after


# -- closing contours -------------------------------------------------------

def test_filled_contours_close_for_free():
    """A fill already renders an open subpath closed, so this is a safe fix."""
    filled = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="12cm" height="12cm" '
        'viewBox="0 0 120 120"><g id="a" fill="#000000">'
        '<path d="M10 10 L110 10 L110 110 L10 110"/></g></svg>'
    )
    report = FixEngine.from_profile_name("embroidery-basic").fix_source(filled)
    assert report.source_after.count(" Z") == 1
    assert "path.closed" in report.fixed_rules
    if report.visual is not None:
        assert report.visual.identical


def test_a_small_gap_on_a_stroked_path_is_closed():
    report = fix(NEAR_MISS)
    assert 'L10 10.5 Z"' in report.source_after
    changes = [str(c) for fixed in report.applied for c in fixed.changes]
    assert any("0.50 mm gap" in change for change in changes)


def test_a_wide_gap_is_left_for_a_human():
    report = fix(WIDE_GAP)
    reasons = [s.reason for s in report.skipped if s.rule_id == "path.closed"]
    assert any("invent artwork" in reason for reason in reasons)
    assert not report.changed


def test_gap_closing_stays_within_its_budget():
    result = verify_fixer(NEAR_MISS, "path.closed", risk=Risk.LOSSY)
    assert result.ok, result.summary()
    if result.visual is not None:
        assert result.visual.within(0.02)


def test_closing_keeps_the_case_the_path_uses():
    lowercase = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="12cm" height="12cm" '
        'viewBox="0 0 120 120"><g id="a" fill="#000000">'
        '<path d="m10 10 l100 0 l0 100 l-100 0"/></g></svg>'
    )
    report = FixEngine.from_profile_name("embroidery-basic").fix_source(lowercase)
    assert report.source_after.rstrip().count(" z") == 1


# -- the path scanner -------------------------------------------------------

def test_implicit_repeats_after_a_moveto_become_linetos():
    assert list(iter_commands("M0 0 1 1 2 2")) == [
        ("M", [0.0, 0.0]),
        ("L", [1.0, 1.0]),
        ("L", [2.0, 2.0]),
    ]


def test_subpath_endpoints_absolute_and_relative():
    found = subpaths("M10 10 L110 10 L110 110 L10 110")
    assert len(found) == 1
    assert found[0].start == (10.0, 10.0)
    assert found[0].end == (10.0, 110.0)
    assert found[0].gap == pytest.approx(100.0)
    assert not found[0].closed

    relative = subpaths("m10 10 l100 0 l0 100 l-100 0")
    assert relative[0].end == (10.0, 110.0)


def test_a_closed_subpath_is_recognised_and_returns_to_its_start():
    found = subpaths("M0 0 L10 0 L10 10 Z M20 20 L30 20")
    assert [sub.closed for sub in found] == [True, False]
    assert found[1].start == (20.0, 20.0)


def test_curve_commands_track_only_the_endpoint():
    found = subpaths("M0 0 C10 0 10 10 20 20")
    assert found[0].end == (20.0, 20.0)

    arc = subpaths("M0 0 A5 5 0 0 1 30 40")
    assert arc[0].end == (30.0, 40.0)


def test_scanner_stops_rather_than_guessing_at_malformed_input():
    assert subpaths("M10") == []
    assert subpaths("") == []
