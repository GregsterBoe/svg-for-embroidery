from pathlib import Path

import pytest

from svg_embroidery.findings import Severity
from svg_embroidery.rules import RuleConfigError, create_rule


def run(rule_id, doc, **params):
    severity = params.pop("severity", None)
    rule = create_rule(rule_id, params, severity)
    return list(rule.check(doc))


def errors(findings):
    return [f for f in findings if f.severity is Severity.ERROR]


def test_file_extension(make_doc):
    doc = make_doc("", path=Path("design.png"))
    assert errors(run("file.extension", doc))
    assert not errors(run("file.extension", doc, allowed=[".png", ".svg"]))


def test_canvas_size_limits(make_doc):
    small = make_doc("", attrs='width="5cm" height="5cm"')
    assert len(errors(run("geometry.canvas_size", small, min_cm=10, max_cm=38))) == 2

    fine = make_doc("", attrs='width="12cm" height="12cm"')
    assert not errors(run("geometry.canvas_size", fine, min_cm=10, max_cm=38))

    tall = make_doc("", attrs='width="12cm" height="40cm"')
    found = errors(run("geometry.canvas_size", tall, min_cm=10, max_cm=38))
    assert len(found) == 1 and found[0].data["axis"] == "height"


def test_canvas_size_per_axis(make_doc):
    doc = make_doc("", attrs='width="30cm" height="12cm"')
    assert errors(run("geometry.canvas_size", doc, max_width_cm=25, max_height_cm=35))


def test_canvas_size_unknown_warns(make_doc):
    doc = make_doc("", attrs='width="100%" height="100%"')
    findings = run("geometry.canvas_size", doc, min_cm=10)
    assert findings[0].severity is Severity.WARNING


def test_canvas_size_rejects_bad_params(make_doc):
    with pytest.raises(RuleConfigError):
        create_rule("geometry.canvas_size", {"min_cm": "ten"})
    with pytest.raises(RuleConfigError):
        create_rule("geometry.canvas_size", {"nope": 1})


def test_max_colors_counts_normalised(make_doc):
    doc = make_doc(
        '<path d="M0 0 Z" fill="#FFF"/><path d="M0 0 Z" fill="white"/>'
        '<path d="M0 0 Z" fill="rgb(255,255,255)"/>'
    )
    findings = run("color.max_count", doc, max_colors=1)
    assert not errors(findings)

    doc = make_doc(
        '<path d="M0 0 Z" fill="#111"/><path d="M0 0 Z" fill="#222"/>'
        '<path d="M0 0 Z" fill="#333"/><path d="M0 0 Z" fill="#444"/>'
    )
    assert errors(run("color.max_count", doc, max_colors=3))


def test_max_colors_can_ignore_strokes(make_doc):
    doc = make_doc('<path d="M0 0 Z" fill="#111" stroke="#222" stroke-width="2"/>')
    assert errors(run("color.max_count", doc, max_colors=1))
    assert not errors(run("color.max_count", doc, max_colors=1, include_stroke=False))


def test_allowed_palette(make_doc):
    doc = make_doc('<path d="M0 0 Z" fill="#c8102e"/><path d="M0 0 Z" fill="#abcdef"/>')
    found = errors(run("color.allowed_palette", doc, colors=["#c8102e", "black"]))
    assert found and found[0].data["colors"] == ["#abcdef"]

    doc = make_doc('<path d="M0 0 Z" fill="black"/>')
    assert not errors(run("color.allowed_palette", doc, colors=["#000000"]))

    with pytest.raises(RuleConfigError):
        create_rule("color.allowed_palette", {"colors": ["not-a-color"]})


def test_gradients(make_doc):
    doc = make_doc('<defs><radialGradient id="g"/></defs><rect fill="url(#g)"/>')
    assert errors(run("color.no_gradients", doc))
    assert not errors(run("color.no_gradients", make_doc('<rect fill="#000"/>')))


def test_transparency(make_doc):
    doc = make_doc('<path d="M0 0 Z" fill="#000" fill-opacity="0.4"/>')
    assert run("color.no_transparency", doc, severity="error")[0].severity is Severity.ERROR
    assert not errors(run("color.no_transparency", make_doc('<path d="M0 0 Z" fill="#000"/>')))


def test_forbidden_elements(make_doc):
    doc = make_doc('<text x="0" y="0">hi</text><image href="a.png"/>')
    found = errors(run("element.forbidden", doc, tags=["text", "image"]))
    assert {f.data["tag"] for f in found} == {"text", "image"}


def test_no_raster(make_doc):
    doc = make_doc('<image href="data:image/png;base64,AAAA"/>')
    assert errors(run("document.no_raster", doc))
    assert not errors(run("document.no_raster", make_doc('<rect fill="#000"/>')))


def test_color_layers(make_doc):
    mixed = make_doc(
        '<g id="a"><path d="M0 0 Z" fill="#111"/><path d="M0 0 Z" fill="#222"/></g>'
    )
    assert errors(run("structure.color_layers", mixed))

    separated = make_doc(
        '<g id="a"><path d="M0 0 Z" fill="#111"/></g>'
        '<g id="b"><path d="M0 0 Z" fill="#222"/></g>'
    )
    assert not errors(run("structure.color_layers", separated))

    flat = make_doc('<path d="M0 0 Z" fill="#111"/><path d="M0 0 Z" fill="#222"/>')
    assert errors(run("structure.color_layers", flat))
    assert not errors(run("structure.color_layers", flat, require_layers=False))

    single = make_doc('<path d="M0 0 Z" fill="#111"/>')
    assert not errors(run("structure.color_layers", single))


def test_closed_paths(make_doc):
    assert errors(run("path.closed", make_doc('<path d="M0 0 L10 10"/>')))
    assert errors(run("path.closed", make_doc('<path d="M0 0 L10 0 Z M5 5 L9 9"/>')))
    assert not errors(run("path.closed", make_doc('<path d="M0 0 L10 0 Z M5 5 L9 9 z"/>')))
    # only the last subpath is closed, but subpath checking is off
    assert not errors(
        run("path.closed", make_doc('<path d="M0 0 L1 1 M5 5 L9 9 Z"/>'), check_subpaths=False)
    )


def test_open_shapes(make_doc):
    doc = make_doc('<polyline points="0,0 5,5"/><path d="M0 0 Z"/>')
    assert errors(run("path.closed", doc))
    assert not errors(run("path.closed", doc, allow_open_shapes=True))


def test_empty_document_warns(make_doc):
    findings = run("path.closed", make_doc(""))
    assert findings[0].severity is Severity.WARNING


def test_min_stroke_width(make_doc):
    # viewBox 0 0 120 120 at 12cm -> 1 user unit == 1 mm
    thin = make_doc('<path d="M0 0 Z" stroke="#000" stroke-width="0.5"/>')
    found = errors(run("stroke.min_width", thin, min_mm=1.5))
    assert found and found[0].data["width_mm"] == pytest.approx(0.5)

    thick = make_doc('<path d="M0 0 Z" stroke="#000" stroke-width="2"/>')
    assert not errors(run("stroke.min_width", thick, min_mm=1.5))

    # a transform scales the effective width above the limit
    scaled = make_doc(
        '<g transform="scale(4)"><path d="M0 0 Z" stroke="#000" stroke-width="0.5"/></g>'
    )
    assert not errors(run("stroke.min_width", scaled, min_mm=1.5))


def test_min_stroke_width_ignores_unstroked(make_doc):
    doc = make_doc('<path d="M0 0 Z" fill="#000" stroke="none" stroke-width="0.1"/>')
    assert not errors(run("stroke.min_width", doc, min_mm=1.5))


def test_no_strokes(make_doc):
    assert errors(run("stroke.forbidden", make_doc('<path d="M0 0 Z" stroke="#000"/>')))
    assert not errors(run("stroke.forbidden", make_doc('<path d="M0 0 Z" fill="#000"/>')))


def test_fill_required(make_doc):
    doc = make_doc('<path d="M0 0 Z" fill="none" stroke="#000"/>')
    findings = run("fill.required", doc)
    assert findings[0].severity is Severity.WARNING
    assert not errors(run("fill.required", make_doc('<path d="M0 0 Z" fill="#000"/>')))


def test_aspect_ratio(make_doc):
    wide = make_doc("", attrs='width="40cm" height="10cm"')
    assert errors(run("geometry.aspect_ratio", wide, max_ratio=2))
    assert not errors(run("geometry.aspect_ratio", wide, max_ratio=4))


def test_require_viewbox(make_doc):
    doc = make_doc("", attrs='width="12cm" height="12cm"')
    assert run("geometry.require_viewbox", doc)[0].severity is Severity.WARNING


def test_path_count(make_doc):
    doc = make_doc('<path d="M0 0 Z"/>' * 5)
    assert run("path.max_count", doc, max_paths=3)[0].severity is Severity.WARNING
    assert run("path.max_count", doc, max_paths=10)[0].severity is Severity.INFO


def test_unknown_rule():
    with pytest.raises(RuleConfigError):
        create_rule("does.not.exist")


def test_invalid_severity():
    with pytest.raises(RuleConfigError):
        create_rule("path.closed", severity="critical")
