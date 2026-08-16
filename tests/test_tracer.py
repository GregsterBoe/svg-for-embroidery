"""B0: the tracers, and the wrapper that makes them comparable."""

import pytest

from svg_embroidery.document import parse_svg
from svg_embroidery.raster import Quantisation
from svg_embroidery.tracer import (
    ALL_BACKENDS,
    Layer,
    TracerError,
    available_backends,
    backend_named,
    count_nodes,
    default_backend,
    hex_color,
    layered_svg,
    measure_svg,
    pbm_bytes,
)
from svg_embroidery.visual import compare_rasters, default_renderer, render

WHITE = (255, 255, 255)
BLACK = (26, 26, 26)
RED = (200, 16, 46)


def quantisation(width, height, ink, palette=(WHITE, BLACK)):
    """Two-colour image from a predicate: True where the ink is."""
    indices = [1 if ink(x, y) else 0 for y in range(height) for x in range(width)]
    return Quantisation(
        palette=list(palette), indices=indices, width=width, height=height
    )


def disc(cx, cy, r):
    return lambda x, y: (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def ring(cx, cy, outer, inner):
    return lambda x, y: inner * inner < (x - cx) ** 2 + (y - cy) ** 2 <= outer * outer


def traced(backend, quant, canvas_mm=100.0):
    return backend.trace(quant, canvas_mm=canvas_mm)


installed = pytest.mark.skipif(
    not available_backends(), reason="no tracer installed here"
)
renderable = pytest.mark.skipif(
    default_renderer() is None, reason="no renderer installed here"
)


# -- the pieces, which need nothing installed --------------------------------

def test_a_mask_becomes_the_bitmap_a_tracer_expects():
    # P4 packs eight pixels per byte, high bit first.
    mask = [True, False, False, False, False, False, False, True, False]
    data = pbm_bytes(mask, 9, 1)
    assert data.startswith(b"P4\n9 1\n")
    assert data[len(b"P4\n9 1\n"):] == bytes([0b10000001, 0b00000000])


def test_the_bitmap_pads_each_row_to_a_whole_byte():
    # 3 wide is 1 byte per row, not three bits crammed together.
    data = pbm_bytes([True] * 6, 3, 2)
    assert data[len(b"P4\n3 2\n"):] == bytes([0b11100000, 0b11100000])


def test_a_colour_becomes_the_hex_a_profile_can_count():
    assert hex_color(RED) == "#c8102e"


def test_nodes_are_drawing_commands_and_a_close_is_not_one():
    assert count_nodes("M0,0 L10,0 L10,10 Z") == 3
    assert count_nodes("") == 0


def test_a_document_is_measured_the_same_however_its_author_split_it():
    """The point of counting subpaths rather than elements.

    potrace writes one <path> holding every outline; potracer hands back loose
    curves. Both drawings are the same drawing, and the table has to say so.
    """
    one = '<svg><path d="M0,0L1,0L1,1Z M4,4L5,4L5,5Z"/></svg>'
    many = '<svg><path d="M0,0L1,0L1,1Z"/><path d="M4,4L5,4L5,5Z"/></svg>'
    assert measure_svg(one) == measure_svg(many) == (2, 6)


# -- the document the wrapper builds -----------------------------------------

def test_the_document_carries_millimetres_outside_and_pixels_inside():
    svg = layered_svg([Layer(1, BLACK, 0.5, ["M0,0L4,0L4,4Z"])], 64, 32, canvas_mm=100.0)
    document = parse_svg(svg)
    assert document.width_mm == 100.0
    assert document.height_mm == 50.0  # half as tall, at the same mm per pixel
    assert document.user_unit_mm == pytest.approx(100.0 / 64.0)
    assert 'viewBox="0 0 64 32"' in svg


def test_every_colour_gets_its_own_top_level_group():
    svg = layered_svg(
        [Layer(0, WHITE, 0.9, ["M0,0L8,0L8,8Z"]), Layer(1, BLACK, 0.1, ["M2,2L4,2L4,4Z"])],
        8, 8,
    )
    assert svg.count("<g ") == 2
    assert 'fill="#ffffff"' in svg and 'fill="#1a1a1a"' in svg
    # ...which is what structure.color_layers is looking for.
    assert svg.index("#ffffff") < svg.index("#1a1a1a")


def test_one_layer_is_one_path_so_its_holes_stay_holes():
    """A counter is a subpath wound against its container.

    Split it into its own <path> element and there is nothing left for it to cut
    a hole in, so it fills in solid — which is how an 'O' becomes a blob.
    """
    svg = layered_svg([Layer(1, BLACK, 0.3, ["M0,0L9,0L9,9Z", "M3,3L6,3L6,6Z"])], 9, 9)
    assert svg.count("<path") == 1


# -- the backends themselves -------------------------------------------------

def test_an_unknown_tracer_is_named_along_with_the_ones_that_exist():
    with pytest.raises(TracerError) as caught:
        backend_named("inkscape")
    assert "potrace" in str(caught.value)


def test_asking_an_absent_tracer_to_work_says_how_to_install_it(monkeypatch):
    monkeypatch.setenv("SVGEMB_NO_TRACER", "1")
    backend = backend_named("potrace")
    with pytest.raises(TracerError) as caught:
        backend.trace(quantisation(8, 8, disc(4, 4, 3)))
    assert "install" in str(caught.value).lower()


def test_turning_tracers_off_leaves_no_default(monkeypatch):
    monkeypatch.setenv("SVGEMB_NO_TRACER", "1")
    assert available_backends() == []
    assert default_backend() is None


def test_the_preference_order_is_the_spikes_conclusion():
    # potrace first: same output as potracer and much faster; vtracer last,
    # because it fits line art about half as well. docs/spikes/B0-tracers.md.
    assert [backend.name for backend in ALL_BACKENDS] == [
        "potrace",
        "potracer",
        "vtracer",
    ]


@installed
def test_every_installed_tracer_produces_a_document_we_can_parse():
    quant = quantisation(48, 48, disc(24, 24, 16))
    for backend in available_backends():
        result = traced(backend, quant)
        assert result.paths >= 1, backend.name
        assert result.nodes >= result.paths, backend.name
        parse_svg(result.svg)  # raises if it is not well-formed


@installed
@renderable
def test_a_trace_looks_like_what_it_traced():
    quant = quantisation(64, 64, disc(32, 32, 20))
    for backend in available_backends():
        result = traced(backend, quant)
        shot = render(result.svg, width=64)
        difference = compare_rasters(quant.raster(), shot)
        assert difference.ratio < 0.1, f"{backend.name} drew something else"


@installed
@renderable
def test_a_hole_survives_the_trip_through_every_tracer():
    """The regression that matters most: an 'O' must not come back as an 'O'-shaped blob."""
    quant = quantisation(64, 64, ring(32, 32, 24, 12))
    for backend in available_backends():
        shot = render(traced(backend, quant).svg, width=64)
        centre = shot.pixels[(32 * 64 + 32) * 4:(32 * 64 + 32) * 4 + 3]
        assert tuple(centre) != BLACK, f"{backend.name} filled the counter in"


@installed
def test_the_largest_colour_is_stitched_first():
    quant = quantisation(32, 32, disc(16, 16, 6))  # small ink on a big background
    for backend in available_backends():
        if backend.kind != "mask":
            continue  # a colour tracer orders its own document; that is a finding
        svg = traced(backend, quant).svg
        assert svg.index(hex_color(WHITE)) < svg.index(hex_color(BLACK))


@installed
def test_an_empty_colour_costs_no_layer():
    quant = Quantisation(
        palette=[WHITE, BLACK, RED], indices=[0] * 64, width=8, height=8
    )
    for backend in available_backends():
        if backend.kind != "mask":
            continue
        result = traced(backend, quant)
        assert result.layers == 1, backend.name


@installed
def test_a_tracer_says_which_version_answered():
    for backend in available_backends():
        assert backend.version(), f"{backend.name} could not say what it is"
