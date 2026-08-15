import pytest

from svg_embroidery.colors import is_paint_reference, normalize_color
from svg_embroidery.units import parse_length, to_mm


@pytest.mark.parametrize(
    "value,expected_mm",
    [
        ("10mm", 10.0),
        ("1cm", 10.0),
        ("1in", 25.4),
        ("96px", 25.4),
        ("96", 25.4),
        ("72pt", 25.4),
        ("1e2mm", 100.0),
        (" 5.5 cm ", 55.0),
    ],
)
def test_length_conversion(value, expected_mm):
    assert to_mm(value) == pytest.approx(expected_mm)


@pytest.mark.parametrize("value", ["50%", "2em", "abc", "", None, "12 34"])
def test_unresolvable_lengths(value):
    assert to_mm(value) is None


def test_user_unit_scale_applies_to_px():
    # One user unit is 0.5 mm here, so "4" user units are 2 mm.
    assert to_mm("4", user_unit_mm=0.5) == pytest.approx(2.0)
    # Absolute units ignore the document scale.
    assert to_mm("4mm", user_unit_mm=0.5) == pytest.approx(4.0)


def test_parse_length_keeps_unit():
    length = parse_length("38.5cm")
    assert (length.value, length.unit) == (38.5, "cm")


@pytest.mark.parametrize(
    "value,expected",
    [
        ("white", "#ffffff"),
        ("#FFF", "#ffffff"),
        ("#ffffff", "#ffffff"),
        ("rgb(255, 255, 255)", "#ffffff"),
        ("rgb(100%, 100%, 100%)", "#ffffff"),
        ("rgba(255,255,255,0.5)", "#ffffff"),
        ("#12345678", "#123456"),
        ("REBECCAPURPLE", "#663399"),
    ],
)
def test_color_normalisation(value, expected):
    assert normalize_color(value) == expected


@pytest.mark.parametrize("value", ["none", "transparent", "currentColor", "url(#grad)", "nope", None, ""])
def test_non_colors(value):
    assert normalize_color(value) is None


def test_paint_reference():
    assert is_paint_reference("url(#grad1)") == "grad1"
    assert is_paint_reference("url('#grad1')") == "grad1"
    assert is_paint_reference("#ff0000") is None
