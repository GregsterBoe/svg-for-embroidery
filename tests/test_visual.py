"""A1: the visual regression harness."""

import struct
import zlib

import pytest

from svg_embroidery import visual
from svg_embroidery.visual import (
    DEFAULT_WIDTH,
    Difference,
    PngError,
    Raster,
    available_renderers,
    compare_rasters,
    decode_png,
    default_renderer,
    render,
    show_through,
    visual_difference,
)

SQUARE = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="4cm" height="4cm" viewBox="0 0 40 40">'
    '<rect x="5" y="5" width="30" height="30" fill="#c8102e"/></svg>'
)
SQUARE_OTHER_COLOUR = SQUARE.replace("#c8102e", "#00843d")
SQUARE_REFORMATTED = SQUARE.replace('<rect x="5"', '<rect\n      x="5"')

needs_renderer = pytest.mark.skipif(
    default_renderer() is None, reason="no SVG renderer installed"
)


# -- PNG decoding (pure Python, no renderer needed) ------------------------

def make_png(width, height, rgba_rows, color_type=6, filter_type=0):
    """Build a PNG by hand, to test the decoder without a renderer."""
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color_type]
    raw = bytearray()
    for row in rgba_rows:
        raw.append(filter_type)
        raw.extend(row)

    def chunk(kind, body):
        return (
            struct.pack(">I", len(body))
            + kind
            + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (
        visual.PNG_SIGNATURE
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw)))
        + chunk(b"IEND", b"")
    )


def test_decode_rgba():
    png = make_png(2, 1, [bytes([255, 0, 0, 255, 0, 255, 0, 128])])
    raster = decode_png(png)
    assert raster.size == (2, 1)
    assert raster.pixels == bytes([255, 0, 0, 255, 0, 255, 0, 128])


def test_decode_greyscale_and_rgb():
    grey = decode_png(make_png(2, 1, [bytes([0, 255])], color_type=0))
    assert grey.pixels == bytes([0, 0, 0, 255, 255, 255, 255, 255])

    rgb = decode_png(make_png(1, 1, [bytes([10, 20, 30])], color_type=2))
    assert rgb.pixels == bytes([10, 20, 30, 255])


@pytest.mark.parametrize("filter_type", [0, 1, 2, 3, 4])
def test_all_png_filters_decode(filter_type):
    """Renderers pick filters per scanline; all five must reconstruct."""
    rows = [bytes(range(0, 16)), bytes(range(16, 32))]
    plain = decode_png(make_png(4, 2, rows))

    # Re-encode the same pixels using the filter under test.
    filtered = []
    previous = bytes(16)
    for row in rows:
        if filter_type == 0:
            filtered.append(row)
        elif filter_type == 1:
            filtered.append(bytes((row[i] - (row[i - 4] if i >= 4 else 0)) & 0xFF for i in range(16)))
        elif filter_type == 2:
            filtered.append(bytes((row[i] - previous[i]) & 0xFF for i in range(16)))
        elif filter_type == 3:
            filtered.append(
                bytes(
                    (row[i] - (((row[i - 4] if i >= 4 else 0) + previous[i]) >> 1)) & 0xFF
                    for i in range(16)
                )
            )
        else:
            filtered.append(
                bytes(
                    (row[i] - visual._paeth(
                        row[i - 4] if i >= 4 else 0,
                        previous[i],
                        previous[i - 4] if i >= 4 else 0,
                    )) & 0xFF
                    for i in range(16)
                )
            )
        previous = row

    assert decode_png(make_png(4, 2, filtered, filter_type=filter_type)).pixels == plain.pixels


def test_decode_rejects_what_it_cannot_read():
    with pytest.raises(PngError, match="not a PNG"):
        decode_png(b"nope")
    with pytest.raises(PngError, match="interlaced"):
        header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 1)
        body = struct.pack(">I", 13) + b"IHDR" + header + b"\0\0\0\0"
        decode_png(visual.PNG_SIGNATURE + body)


# -- comparison ------------------------------------------------------------

def test_transparent_pixels_compare_equal_whatever_their_colour():
    """Renderers leave junk RGB behind full transparency; it must not count."""
    left = Raster(1, 1, bytes([255, 0, 0, 0]))
    right = Raster(1, 1, bytes([0, 0, 255, 0]))
    assert compare_rasters(left, right).identical


def test_visible_difference_is_counted():
    left = Raster(2, 1, bytes([0, 0, 0, 255, 255, 255, 255, 255]))
    right = Raster(2, 1, bytes([255, 255, 255, 255, 255, 255, 255, 255]))
    diff = compare_rasters(left, right)
    assert not diff.identical
    assert diff.changed_pixels == 1
    assert diff.max_delta == 255
    assert diff.ratio == 0.5
    assert diff.within(0.5) and not diff.within(0.1)


def test_tolerance_absorbs_a_rounding_wobble():
    left = Raster(1, 1, bytes([100, 100, 100, 255]))
    right = Raster(1, 1, bytes([101, 100, 100, 255]))
    assert compare_rasters(left, right, tolerance=2).identical
    assert not compare_rasters(left, right, tolerance=0).identical


def test_size_mismatch_is_a_total_difference():
    diff = compare_rasters(Raster(1, 1, bytes(4)), Raster(2, 1, bytes(8)))
    assert not diff.identical
    assert "different sizes" in diff.note


def test_difference_reads_well():
    assert str(Difference(0, 100, 0, 0.0)) == "images identical"
    assert "10.00%" in str(Difference(10, 100, 255, 1.0))


# -- show-through, B4's seam instrument ------------------------------------

def solid(width, height, alpha_rows):
    """A raster from per-pixel alpha values; the colour never matters here."""
    pixels = bytearray()
    for row in alpha_rows:
        for alpha in row:
            pixels += bytes((200, 0, 0, alpha))
    return Raster(width, height, bytes(pixels))


def test_a_fully_painted_image_shows_nothing_through():
    result = show_through(solid(3, 3, [[255] * 3] * 3))
    assert result.pixels == 0
    assert result.area == 0.0
    assert result.worst == 255
    assert str(result) == "nothing shows through"


def test_a_hole_is_measured_by_how_much_is_missing_not_by_how_many_pixels():
    """A pixel at 99% coverage is not the same news as a pixel at 0%."""
    faint = show_through(solid(3, 3, [[255] * 3, [255, 128, 255], [255] * 3]))
    hole = show_through(solid(3, 3, [[255] * 3, [255, 0, 255], [255] * 3]))
    # One pixel each, either way — which is exactly why `ratio` is not the
    # number B4 is graded on.
    assert faint.ratio == hole.ratio == 1.0
    assert faint.area == pytest.approx(127 / 255)
    assert hole.area == 1.0
    assert (faint.worst, hole.worst) == (128, 0)


def test_the_edge_of_the_page_is_not_a_seam():
    """Every correctly drawn document half-covers its outermost pixels.

    Counting those would put a floor under the metric that no fix could reach,
    so the frame is dropped — and the inside is then measured against its own
    size, not the whole canvas.
    """
    edge_only = solid(3, 3, [[0, 0, 0], [0, 255, 0], [0, 0, 0]])
    assert show_through(edge_only).pixels == 0
    assert show_through(edge_only).total_pixels == 1
    assert show_through(edge_only, margin=0).ratio == pytest.approx(8 / 9)


def test_an_image_too_small_to_have_an_inside_reports_nothing():
    assert show_through(solid(1, 1, [[0]])).total_pixels == 0
    assert show_through(solid(1, 1, [[0]])).area == 0.0


@needs_renderer
def test_two_shapes_drawn_edge_to_edge_leave_a_seam():
    """The defect B4 exists to close, in three lines of SVG.

    Both rectangles are exact and they share an edge exactly — and it still
    shows, because each covers half of the boundary pixel and two halves
    composite to three quarters.
    """
    butted = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40" '
        'viewBox="0 0 40 40">'
        '<rect x="0" y="0" width="19.5" height="40" fill="#c8102e"/>'
        '<rect x="19.5" y="0" width="20.5" height="40" fill="#1a1a1a"/></svg>'
    )
    trapped = butted.replace('width="19.5"', 'width="21"')  # spread the lower one

    assert show_through(render(butted, width=40)).area > 0.0
    assert show_through(render(trapped, width=40)).area == 0.0


# -- with a real renderer --------------------------------------------------

@needs_renderer
def test_identical_input_scores_zero():
    assert visual_difference(SQUARE, SQUARE, width=64).identical


@needs_renderer
def test_a_colour_change_is_detected():
    diff = visual_difference(SQUARE, SQUARE_OTHER_COLOUR, width=64)
    assert not diff.identical
    assert diff.ratio > 0.1


@needs_renderer
def test_reformatting_is_not_a_visual_change():
    """The property every fixer depends on: layout edits must score zero."""
    assert visual_difference(SQUARE, SQUARE_REFORMATTED, width=64).identical


@needs_renderer
def test_rendering_is_deterministic():
    first = render(SQUARE, width=64)
    second = render(SQUARE, width=64)
    assert first.pixels == second.pixels


@needs_renderer
def test_every_installed_renderer_agrees_about_a_change():
    for renderer in available_renderers():
        same = visual_difference(SQUARE, SQUARE, width=64, renderer=renderer)
        changed = visual_difference(SQUARE, SQUARE_OTHER_COLOUR, width=64, renderer=renderer)
        assert same.identical, renderer.name
        assert not changed.identical, renderer.name


@needs_renderer
def test_decoder_agrees_with_pillow_if_present():
    pillow = pytest.importorskip("PIL.Image")
    import io

    png = default_renderer().render(SQUARE, DEFAULT_WIDTH)
    reference = pillow.open(io.BytesIO(png)).convert("RGBA")
    assert decode_png(png).pixels == reference.tobytes()


# -- graceful degradation --------------------------------------------------

def test_everything_skips_cleanly_without_a_renderer(monkeypatch):
    """A missing renderer is a missing measurement, never a failure."""
    monkeypatch.setattr(visual, "available_renderers", lambda: [])
    assert visual.default_renderer() is None
    assert visual.render(SQUARE) is None
    assert visual.visual_difference(SQUARE, SQUARE_OTHER_COLOUR) is None


def test_roundtrip_still_passes_without_a_renderer(monkeypatch):
    from svg_embroidery import roundtrip

    monkeypatch.setattr(roundtrip, "visual_difference", lambda *a, **k: None)
    result = roundtrip.check_roundtrip(SQUARE)
    assert result.ok
    assert result.render_identical is None


def test_capabilities_report_is_honest():
    from svg_embroidery.capabilities import platform_key, report

    statuses = {status.capability.key: status for status in report()}
    assert statuses["core"].available is True

    rendering = statuses["rendering"]
    assert rendering.available is (default_renderer() is not None)
    assert platform_key() in ("linux", "macos", "windows", "termux")


def test_every_capability_has_an_install_hint_for_every_platform():
    from svg_embroidery.capabilities import CAPABILITIES

    for capability in CAPABILITIES:
        if not capability.requirements:
            continue
        for key in ("termux", "linux", "macos", "windows"):
            assert capability.hints.get(key), f"{capability.key} has no hint for {key}"
