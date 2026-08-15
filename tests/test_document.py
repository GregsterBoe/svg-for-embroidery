import pytest

from svg_embroidery.document import (
    SvgParseError,
    iter_subpaths,
    matrix_scale,
    parse_style,
    parse_svg,
    transform_matrix,
)


def test_size_from_attributes(make_doc):
    doc = make_doc("", attrs='width="20cm" height="10cm" viewBox="0 0 200 100"')
    assert doc.width_mm == pytest.approx(200)
    assert doc.height_mm == pytest.approx(100)
    assert doc.user_unit_mm == pytest.approx(1.0)
    assert doc.size_source == "attributes"


def test_size_from_viewbox_only(make_doc):
    doc = make_doc("", attrs='viewBox="0 0 96 48"')
    assert doc.width_mm == pytest.approx(25.4)
    assert doc.height_mm == pytest.approx(12.7)
    assert doc.size_source == "viewbox"


def test_size_unknown_for_percentages(make_doc):
    doc = make_doc("", attrs='width="100%" height="100%"')
    assert doc.width_mm is None
    assert doc.size_source == "unknown"


def test_style_and_inheritance(make_doc):
    doc = make_doc(
        '<g fill="#ff0000" style="stroke:#0000ff;stroke-width:2">'
        '<path d="M0 0 L1 1 Z"/>'
        '<path d="M0 0 L1 1 Z" fill="none" style="fill:#00ff00"/>'
        "</g>"
    )
    first, second = doc.by_tag("path")
    assert first.paint("fill") == "#ff0000"
    assert first.paint("stroke") == "#0000ff"
    # style beats the presentation attribute on the same element
    assert second.paint("fill") == "#00ff00"


def test_transform_scale_is_cumulative(make_doc):
    doc = make_doc(
        '<g transform="scale(2)"><g transform="translate(5,5) scale(3)">'
        '<path d="M0 0 Z" stroke="#000" stroke-width="1"/></g></g>'
    )
    node = doc.by_tag("path")[0]
    assert node.transform_scale == pytest.approx(6.0)
    # 1 user unit == 1 mm in this document, scaled by 6
    assert node.stroke_width_mm(doc.unit_scale) == pytest.approx(6.0)


def test_matrix_helpers():
    assert matrix_scale(transform_matrix("scale(3, 3)")) == pytest.approx(3.0)
    assert matrix_scale(transform_matrix("rotate(45)")) == pytest.approx(1.0)
    assert matrix_scale(transform_matrix("matrix(2 0 0 8 0 0)")) == pytest.approx(4.0)
    assert matrix_scale(transform_matrix(None)) == pytest.approx(1.0)


def test_parse_style():
    assert parse_style("fill:#fff; stroke : none ;bad") == {"fill": "#fff", "stroke": "none"}


def test_visibility_and_drawables(make_doc):
    doc = make_doc(
        '<path d="M0 0 Z" fill="#111"/>'
        '<path d="M0 0 Z" fill="#222" display="none"/>'
        '<g style="visibility:hidden"><path d="M0 0 Z" fill="#333"/></g>'
        '<g display="none"><path d="M0 0 Z" fill="#444" display="inline"/></g>'
    )
    assert set(doc.colors()) == {"#111111"}


def test_layers_prefers_inkscape_groupmode(make_doc):
    doc = make_doc(
        '<g id="wrapper"><g id="l1" inkscape:groupmode="layer" '
        'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"/></g>'
    )
    assert [layer.element_id for layer in doc.layers()] == ["l1"]


@pytest.mark.parametrize(
    "d,expected",
    [
        ("M0 0 L1 1 Z", 1),
        ("M0 0 L1 1 Z M2 2 L3 3 Z", 2),
        ("M0 0 L1 1 z m2 2 l3 3", 2),
    ],
)
def test_iter_subpaths(d, expected):
    assert len(list(iter_subpaths(d))) == expected


def test_parse_errors():
    with pytest.raises(SvgParseError):
        parse_svg("<svg><unclosed>")
    with pytest.raises(SvgParseError):
        parse_svg("<html><body/></html>")


def test_descendants_covers_the_whole_subtree(make_doc):
    doc = make_doc(
        '<g id="a"><g id="a1"><path d="M0 0 Z" fill="#111"/></g>'
        '<path d="M0 0 Z" fill="#222"/></g>'
        '<g id="b"><path d="M0 0 Z" fill="#333"/></g>'
    )
    layer_a = next(n for n in doc.nodes if n.element_id == "a")
    labels = [n.element_id or n.tag for n in doc.descendants(layer_a)]
    assert labels == ["a1", "path", "path"]

    layer_b = next(n for n in doc.nodes if n.element_id == "b")
    assert len(list(doc.descendants(layer_b))) == 1
