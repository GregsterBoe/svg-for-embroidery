"""A3: the batch of safe fixers.

Every fixer here is held to the same contract via ``verify_fixer`` — fixes its
rule, breaks nothing else, idempotent, zero pixels moved.
"""

import pytest

from svg_embroidery.checker import Checker
from svg_embroidery.document import parse_svg
from svg_embroidery.fixes import FixEngine, Risk, available_fixers, verify_fixer
from svg_embroidery.units import to_mm

SMALL = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="5cm" height="5cm" viewBox="0 0 50 50">\n'
    '  <g id="a" fill="#000000"><path d="M5 5 L45 5 L45 45 L5 45 Z"/></g>\n'
    "</svg>\n"
)
HUGE = SMALL.replace('width="5cm" height="5cm"', 'width="60cm" height="60cm"')


def fix(source, profile="embroidery-basic", **kwargs):
    return FixEngine.from_profile_name(profile, **kwargs).fix_source(source)


def size_mm(source):
    doc = parse_svg(source)
    return doc.width_mm, doc.height_mm


# -- every safe fixer keeps its promise ------------------------------------

def test_a_default_run_only_applies_safe_fixes():
    """Lossy repairs exist since A4, but must never run without being asked."""
    engine = FixEngine.from_profile_name("embroidery-basic")
    assert engine.allow == {Risk.SAFE}
    assert any(fixer.risk is Risk.SAFE for fixer in available_fixers())


@pytest.mark.parametrize(
    "rule_id,sample",
    [
        ("geometry.canvas_size", SMALL),
        (
            "geometry.require_viewbox",
            '<svg xmlns="http://www.w3.org/2000/svg" width="12cm" height="12cm">'
            '<g id="a" fill="#000"><path d="M1 1 L10 1 L10 10 Z"/></g></svg>',
        ),
        (
            "color.no_gradients",
            '<svg xmlns="http://www.w3.org/2000/svg" width="12cm" height="12cm" '
            'viewBox="0 0 120 120"><defs><linearGradient id="dead"/></defs>'
            '<g id="a" fill="#000"><path d="M1 1 L100 1 L100 100 Z"/></g></svg>',
        ),
        (
            "element.forbidden",
            '<svg xmlns="http://www.w3.org/2000/svg" width="12cm" height="12cm" '
            'viewBox="0 0 120 120"><defs><filter id="dead"/></defs>'
            '<g id="a" fill="#000"><path d="M1 1 L100 1 L100 100 Z"/></g></svg>',
        ),
        (
            "document.no_editor_metadata",
            '<svg xmlns="http://www.w3.org/2000/svg" '
            'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
            'width="12cm" height="12cm" viewBox="0 0 120 120" inkscape:version="1.1">'
            '<metadata id="m"/><g id="a" fill="#000"><path d="M1 1 L100 1 L100 100 Z"/></g></svg>',
        ),
    ],
)
def test_fixer_satisfies_the_contract(rule_id, sample):
    result = verify_fixer(sample, rule_id)
    assert result.ok, f"{rule_id}:\n{result.summary()}"


# -- canvas scaling --------------------------------------------------------

def test_canvas_too_small_is_scaled_up():
    report = fix(SMALL)
    width, height = size_mm(report.source_after)
    assert width == pytest.approx(100, abs=0.01)
    assert height == pytest.approx(100, abs=0.01)
    assert "geometry.canvas_size" in report.fixed_rules


def test_canvas_too_large_is_scaled_down():
    report = fix(HUGE)
    width, _ = size_mm(report.source_after)
    assert width == pytest.approx(380, abs=0.01)


def test_scaling_keeps_the_viewbox_so_the_artwork_does_not_move():
    report = fix(SMALL)
    assert 'viewBox="0 0 50 50"' in report.source_after
    if report.visual is not None:
        assert report.visual.identical


def test_scaling_preserves_the_unit_the_file_used():
    in_px = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="200px" height="200px" '
        'viewBox="0 0 200 200"><g id="a" fill="#000"><path d="M1 1 L100 1 L100 100 Z"/>'
        "</g></svg>"
    )
    report = fix(in_px)
    assert "px" in report.source_after.split(">")[0]
    width, _ = size_mm(report.source_after)
    assert width >= 100  # rounded into the range, never just below it


def test_scaling_adds_a_viewbox_first_when_there_is_none():
    """Without a viewBox, resizing would move the artwork instead of scaling it."""
    no_viewbox = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="5cm" height="5cm">'
        '<g id="a" fill="#000"><path d="M5 5 L45 5 L45 45 Z"/></g></svg>'
    )
    report = fix(no_viewbox, only={"geometry.canvas_size"})
    assert "viewBox=" in report.source_after
    width, _ = size_mm(report.source_after)
    assert width == pytest.approx(100, abs=0.01)


def test_impossible_aspect_ratio_is_declined_with_a_reason():
    thin = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="2cm" height="30cm" '
        'viewBox="0 0 20 300"><g id="a" fill="#000"><path d="M1 1 L19 1 L19 299 Z"/>'
        "</g></svg>"
    )
    report = fix(thin)
    reasons = [skip.reason for skip in report.skipped if skip.rule_id == "geometry.canvas_size"]
    assert reasons and "aspect ratio" in reasons[0]
    assert 'width="2cm"' in report.source_after


def test_scaling_is_declined_when_it_would_thin_the_strokes_too_far():
    """Shrinking a design makes its strokes physically thinner — caught and reverted."""
    wide = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="60cm" height="60cm" '
        'viewBox="0 0 600 600"><g id="a" fill="none" stroke="#000" stroke-width="1.6">'
        '<path d="M10 10 L590 10 L590 590 L10 590 Z"/></g></svg>'
    )
    before = Checker.from_profile_name("embroidery-basic").check_source(wide)
    assert not any(f.rule_id == "stroke.min_width" for f in before.errors)

    report = fix(wide)
    reasons = [skip.reason for skip in report.skipped if skip.rule_id == "geometry.canvas_size"]
    assert reasons and "rolled back" in reasons[0]
    assert "stroke.min_width" in reasons[0]
    assert 'width="60cm"' in report.source_after


# -- dead definitions ------------------------------------------------------

def test_unreferenced_gradient_is_removed():
    source = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="12cm" height="12cm" '
        'viewBox="0 0 120 120"><defs><linearGradient id="dead"/></defs>'
        '<g id="a" fill="#000"><path d="M1 1 L100 1 L100 100 Z"/></g></svg>'
    )
    report = fix(source)
    assert "linearGradient" not in report.source_after
    assert "color.no_gradients" in report.fixed_rules


def test_referenced_gradient_is_left_alone():
    source = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="12cm" height="12cm" '
        'viewBox="0 0 120 120"><defs><linearGradient id="live"/></defs>'
        '<g id="a"><path d="M1 1 L100 1 L100 100 Z" fill="url(#live)"/></g></svg>'
    )
    report = fix(source)
    assert "linearGradient" in report.source_after
    reasons = [skip.reason for skip in report.skipped if skip.rule_id == "color.no_gradients"]
    assert reasons and "still referenced" in reasons[0]


def test_a_chain_of_dead_gradients_goes_in_one_run():
    """Otherwise a second run would keep deleting, and the fix is not idempotent."""
    source = (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" width="12cm" height="12cm" '
        'viewBox="0 0 120 120"><defs>'
        '<linearGradient id="base"/><linearGradient id="derived" xlink:href="#base"/>'
        '</defs><g id="a" fill="#000"><path d="M1 1 L100 1 L100 100 Z"/></g></svg>'
    )
    report = fix(source)
    assert "linearGradient" not in report.source_after
    assert fix(report.source_after).changed is False


def test_unrendered_filter_is_removed_but_visible_text_is_not():
    source = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="12cm" height="12cm" '
        'viewBox="0 0 120 120"><defs><filter id="dead"/></defs>'
        '<g id="a" fill="#000"><path d="M1 1 L100 1 L100 100 Z"/>'
        '<text x="5" y="110">hello</text></g></svg>'
    )
    report = fix(source)
    assert "<filter" not in report.source_after
    assert "<text" in report.source_after
    applied = [str(change) for fixed in report.applied for change in fixed.changes]
    assert any("filter" in change for change in applied)
    assert any("visible artwork" in change for change in applied)


def test_hidden_forbidden_element_counts_as_unrendered():
    source = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="12cm" height="12cm" '
        'viewBox="0 0 120 120"><g display="none"><image href="a.png"/></g>'
        '<g id="a" fill="#000"><path d="M1 1 L100 1 L100 100 Z"/></g></svg>'
    )
    report = fix(source)
    assert "<image" not in report.source_after


# -- editor metadata -------------------------------------------------------

INKSCAPE = (
    '<svg xmlns="http://www.w3.org/2000/svg" '
    'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
    'xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd" '
    'width="12cm" height="12cm" viewBox="0 0 120 120" '
    'inkscape:version="1.1" sodipodi:docname="private-name.svg">\n'
    '  <sodipodi:namedview id="base" inkscape:zoom="1.4"/>\n'
    '  <metadata id="m"><rdf/></metadata>\n'
    '  <g inkscape:label="Outline" inkscape:groupmode="layer" id="l1" fill="#000">\n'
    '    <path d="M10 10 L110 10 L110 110 L10 110 Z"/>\n'
    "  </g>\n"
    "</svg>\n"
)


def test_editor_state_and_the_file_name_are_stripped():
    report = fix(INKSCAPE)
    after = report.source_after
    assert "private-name.svg" not in after
    assert "inkscape:version" not in after
    assert "namedview" not in after
    assert "<metadata" not in after


def test_layer_annotations_survive_because_other_rules_read_them():
    report = fix(INKSCAPE)
    assert "inkscape:groupmode" in report.source_after
    assert "inkscape:label" in report.source_after

    after = Checker.from_profile_name("embroidery-basic").check_source(report.source_after)
    assert not any(f.rule_id == "structure.color_layers" for f in after.errors)


def test_stripping_metadata_does_not_reflow_the_file():
    """Attribute removal is done with scissors, not by rebuilding the tag."""
    report = fix(INKSCAPE)
    assert report.source_after.count("\n") >= INKSCAPE.count("\n") - 3
    assert '<g inkscape:label="Outline"' in report.source_after


def test_corpus_files_still_verify_after_fixing(tmp_path):
    """Whatever the fixers do to a real export, the result must still be sound."""
    from pathlib import Path

    from svg_embroidery.roundtrip import check_roundtrip

    for path in sorted((Path(__file__).parent / "corpus").glob("*.svg")):
        source = path.read_bytes().decode("utf-8")
        report = fix(source)
        assert report.ok, f"{path.name}: {report.verification_error}"
        assert check_roundtrip(report.source_after).ok, path.name
