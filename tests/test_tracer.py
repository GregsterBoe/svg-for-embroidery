"""B0: the tracers, and the wrapper that makes them comparable.
B4: the layered document it assembles out of them, and its seams."""

import pytest

from svg_embroidery.checker import Checker
from svg_embroidery.document import parse_svg
from svg_embroidery.findings import Severity
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
    layer_order,
    layered_svg,
    measure_svg,
    pbm_bytes,
    trapped_claims,
)
from svg_embroidery.visual import (
    compare_rasters,
    default_renderer,
    render,
    show_through,
)

WHITE = (255, 255, 255)
BLACK = (26, 26, 26)
RED = (200, 16, 46)


def quantisation(width, height, ink, palette=(WHITE, BLACK)):
    """Two-colour image from a predicate: True where the ink is."""
    indices = [1 if ink(x, y) else 0 for y in range(height) for x in range(width)]
    return Quantisation(
        palette=list(palette), indices=indices, width=width, height=height
    )


def labelled(width, height, rows, palette):
    """A quantisation written out as a grid of palette indices."""
    indices = [value for row in rows for value in row]
    assert len(indices) == width * height
    return Quantisation(
        palette=list(palette), indices=indices, width=width, height=height
    )


def covered_by(claims, position):
    """Which pixels the layer at ``position`` in the order ends up painting."""
    return [bool(claim >> position & 1) for claim in claims]


def disc(cx, cy, r):
    return lambda x, y: (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def ring(cx, cy, outer, inner):
    return lambda x, y: inner * inner < (x - cx) ** 2 + (y - cy) ** 2 <= outer * outer


def traced(backend, quant, canvas_mm=100.0, overlap=1):
    return backend.trace(quant, canvas_mm=canvas_mm, overlap=overlap)


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


# -- B4: what order the layers go in -----------------------------------------

def test_the_biggest_colour_is_stitched_first_so_it_sits_underneath():
    quant = quantisation(8, 8, disc(4, 4, 2))  # a little ink on a lot of paper
    assert layer_order(quant) == [0, 1]


def test_a_dark_background_still_goes_underneath():
    """The counterexample that settles it: area decides, darkness does not.

    'Darkest last, so light backgrounds sit underneath' is true of the usual
    case and wrong as a rule — it is a claim about a colour, and stacking is a
    question about shape. On a dark background it puts the background on top of
    the artwork, which is not a seam, it is a blank picture.
    """
    quant = quantisation(8, 8, disc(4, 4, 2), palette=(BLACK, WHITE))
    assert layer_order(quant) == [0, 1]  # black paper first, white ink over it


def test_two_colours_covering_the_same_area_are_split_darkest_last():
    """Where there is no fact about shape to read, the roadmap's instinct wins."""
    half = labelled(2, 2, [[0, 1], [0, 1]], (BLACK, WHITE))
    assert layer_order(half) == [1, 0]  # equal areas: the white one goes under


# -- B4: the seam overlap ----------------------------------------------------

def test_a_layer_is_grown_into_the_pixels_of_layers_stitched_after_it():
    #  0 0 0
    #  0 1 0   — one pixel of colour 1, painted last, in a field of colour 0
    #  0 0 0
    quant = labelled(3, 3, [[0, 0, 0], [0, 1, 0], [0, 0, 0]], (WHITE, BLACK))
    claims = trapped_claims(quant, layer_order(quant), overlap=1)
    assert covered_by(claims, 0) == [True] * 9  # the background now runs underneath
    assert covered_by(claims, 1) == [
        False, False, False, False, True, False, False, False, False
    ]


def test_the_topmost_layer_is_never_grown():
    """It has nothing above it to hide the growth, so growing it would move the art."""
    quant = labelled(3, 3, [[0, 0, 0], [0, 1, 0], [0, 0, 0]], (WHITE, BLACK))
    top = covered_by(trapped_claims(quant, layer_order(quant), overlap=3), 1)
    assert sum(top) == 1


def test_growth_stops_at_the_layers_that_paint_over_it():
    """Three colours: the middle one may spread under the top one and no further.

    Not a detail — spreading into a layer stitched *earlier* would paint over
    artwork that is already down, which is the difference between a seam
    allowance and a mistake.
    """
    #  0 0 | 1 1 | 2 2   — three vertical bands, 0 the widest
    quant = labelled(6, 1, [[0, 0, 0, 1, 1, 2]], (WHITE, RED, BLACK))
    order = layer_order(quant)
    assert order == [0, 1, 2]
    claims = trapped_claims(quant, order, overlap=1)
    assert covered_by(claims, 0) == [True, True, True, True, False, False]
    assert covered_by(claims, 1) == [False, False, False, True, True, True]
    assert covered_by(claims, 2) == [False, False, False, False, False, True]


def test_no_pixel_is_left_for_the_page_to_show_through():
    """The property the whole step is for, stated on the labels themselves."""
    quant = labelled(4, 2, [[0, 0, 1, 1], [0, 2, 2, 1]], (WHITE, RED, BLACK))
    order = layer_order(quant)
    claims = trapped_claims(quant, order, overlap=1)
    for position in range(len(order)):
        mask = covered_by(claims, position)
        for index, painted in enumerate(mask):
            if not painted:
                continue
            # Anything a layer covers is either its own, or belongs to a layer
            # painted after it — never to one painted before.
            owner = order.index(quant.indices[index])
            assert owner >= position


def test_no_overlap_means_no_overlap():
    quant = labelled(3, 3, [[0, 0, 0], [0, 1, 0], [0, 0, 0]], (WHITE, BLACK))
    claims = trapped_claims(quant, layer_order(quant), overlap=0)
    assert sum(covered_by(claims, 0)) == 8
    assert sum(covered_by(claims, 1)) == 1


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


# -- B4's gate: a three-colour logo, three layers, no seams ------------------

def three_colour_logo(side=64):
    """White page, a red half, a black disc across the join.

    Built so that every pair of colours actually touches — a seam can only show
    where two layers meet, so a fixture whose colours never meet would prove
    nothing.
    """
    def label(x, y):
        if (x - side // 2) ** 2 + (y - side // 2) ** 2 <= (side // 4) ** 2:
            return 1  # black disc
        return 2 if x < side // 2 else 0  # red left half, white right

    return Quantisation(
        palette=[WHITE, BLACK, RED],
        indices=[label(x, y) for y in range(side) for x in range(side)],
        width=side,
        height=side,
    )


@installed
def test_a_three_colour_logo_traces_to_three_single_coloured_layers():
    for backend in available_backends():
        if backend.kind != "mask":
            continue  # a colour tracer writes its own document; that is a finding
        result = traced(backend, three_colour_logo(), canvas_mm=120.0)
        assert result.layers == 3, backend.name

        report = Checker.from_profile_name("embroidery-basic").check_source(result.svg)
        failed = {
            finding.rule_id
            for finding in report.findings
            if finding.severity is Severity.ERROR
        }
        # The point of tracing one mask per colour: the layer rule passes by
        # construction rather than by repair.
        assert "structure.color_layers" not in failed, backend.name
        assert "color.max_count" not in failed, backend.name


#: What counts as "no seam left". Not zero: three layers meeting at a point
#: leave a few pixels a fraction short, because the layer that should cover the
#: junction is doing it with a one-pixel spur and a tracer smooths those away.
#: 0.0005 is the benchmark's own epsilon for a ratio that has not moved, and the
#: measured residue is a tenth of it. Growing everything by a second pixel does
#: close it, and costs a millimetre of extra stitching everywhere to do it.
SEAM_BUDGET = 0.0005


@installed
@renderable
def test_the_layers_meet_with_no_bare_fabric_between_them():
    """B4's gate. Butt joints leave a hairline; the overlap closes it."""
    quant = three_colour_logo()
    for backend in available_backends():
        if backend.kind != "mask":
            continue
        butted = show_through(render(traced(backend, quant, overlap=0).svg, width=quant.width))
        trapped = show_through(render(traced(backend, quant, overlap=1).svg, width=quant.width))
        assert butted.area > 0.001, backend.name
        assert butted.worst < 128, backend.name  # an outright hole, not a soft edge
        assert trapped.area < SEAM_BUDGET, f"{backend.name}: {trapped}"
        assert trapped.worst > 230, f"{backend.name}: {trapped}"


@installed
@renderable
def test_closing_the_seams_does_not_move_the_artwork():
    """The claim the design rests on: the overlap changes gaps and little else.

    A layer is grown *only* into pixels that later layers paint over, so every
    colour's visible edge is still drawn by its own outline. What that does not
    quite promise is that the outline comes out identical: a tracer fits a
    layer's boundary as a whole, so changing the mask at the disc can shift a
    straight run somewhere else by a fraction of a pixel. Here that happens to
    two pixels out of four thousand, and it moves them *towards* the source
    colour — so the run is also held to being no less faithful than the butt
    joint it replaces, which is the property anyone actually cares about.
    """
    quant = three_colour_logo()
    want = quant.raster()
    for backend in available_backends():
        if backend.kind != "mask":
            continue
        butted = render(traced(backend, quant, overlap=0).svg, width=quant.width)
        trapped = render(traced(backend, quant, overlap=1).svg, width=quant.width)
        before, after = butted.composite_over(), trapped.composite_over()

        solid_moved = 0
        for pixel in range(quant.width * quant.height):
            moved = max(
                abs(before[pixel * 3 + c] - after[pixel * 3 + c]) for c in range(3)
            )
            if moved > 8 and butted.pixels[pixel * 4 + 3] == 255:
                solid_moved += 1
        assert solid_moved <= 0.002 * quant.width * quant.height, backend.name

        assert (
            compare_rasters(want, trapped).ratio <= compare_rasters(want, butted).ratio
        ), f"{backend.name} traced the artwork less faithfully with the overlap on"
