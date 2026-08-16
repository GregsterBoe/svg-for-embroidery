"""B1: the raster layer — reading images and measuring what is in them."""

import pytest

from svg_embroidery.raster import (
    RasterError,
    available_readers,
    downsample,
    edge_density,
    encode_png,
    flat_ratio,
    has_alpha,
    load_image,
    pillow_available,
    quantise,
    rgb_pixels,
    thin_ratio,
    unique_colors,
)
from svg_embroidery.visual import Raster, compare_rasters, decode_png


def image(width, height, paint):
    pixels = bytearray()
    for y in range(height):
        for x in range(width):
            pixels += bytes(paint(x, y))
    return Raster(width=width, height=height, pixels=bytes(pixels))


WHITE = (255, 255, 255, 255)
BLACK = (26, 26, 26, 255)
RED = (200, 16, 46, 255)


def solid(color):
    return lambda x, y: color


# -- reading and writing -----------------------------------------------------

def test_a_png_survives_a_round_trip_through_our_own_codec():
    original = image(9, 5, lambda x, y: (x * 20, y * 40, 128, 255 if x else 0))
    assert decode_png(encode_png(original)).pixels == original.pixels


def test_reading_a_png_needs_nothing_installed(tmp_path):
    path = tmp_path / "flat.png"
    path.write_bytes(encode_png(image(4, 4, solid(RED))))
    assert load_image(path).pixels == image(4, 4, solid(RED)).pixels
    assert "built-in PNG" in available_readers()


def test_a_format_we_cannot_read_says_what_to_install(tmp_path, monkeypatch):
    monkeypatch.setenv("SVGEMB_NO_RASTER", "1")
    path = tmp_path / "photo.jpg"
    path.write_bytes(b"not really a jpeg")
    with pytest.raises(RasterError) as caught:
        load_image(path)
    assert "Pillow" in str(caught.value)
    assert "PNG works with no dependencies" in str(caught.value)
    assert not pillow_available()


def test_a_missing_file_is_reported_as_one(tmp_path):
    with pytest.raises(RasterError, match="no such image"):
        load_image(tmp_path / "nope.png")


# -- reshaping ---------------------------------------------------------------

def test_downsampling_averages_rather_than_dropping():
    """Dropping pixels would hide the thin detail these metrics exist to find."""
    stripes = image(8, 2, lambda x, y: BLACK if x % 2 else WHITE)
    small = downsample(stripes, 4)
    assert small.size == (4, 1)
    # Every output pixel straddles one black and one white column, so all of
    # them are grey. Nearest-neighbour would have produced a clean stripe.
    assert {pixel for pixel in rgb_pixels(small)} == {(140, 140, 140)}


def test_an_image_already_small_enough_is_returned_untouched():
    small = image(4, 4, solid(RED))
    assert downsample(small, 128) is small


def test_alpha_is_noticed_and_flattened_onto_white():
    assert not has_alpha(image(2, 2, solid(RED)))
    assert has_alpha(image(2, 2, solid((0, 0, 0, 0))))
    assert rgb_pixels(image(1, 1, solid((0, 0, 0, 0)))) == [(255, 255, 255)]


# -- quantisation ------------------------------------------------------------

def test_an_image_within_budget_keeps_every_colour():
    two = image(8, 8, lambda x, y: BLACK if x < 4 else RED)
    result = quantise(two, 3)
    assert sorted(result.palette) == sorted({(26, 26, 26), (200, 16, 46)})
    assert compare_rasters(two, result.raster()).identical


def test_flat_artwork_keeps_its_exact_colours_despite_an_antialiased_rim():
    """The failure that made the first benchmark table meaningless.

    A logo has one dominant red plus a few dozen blend pixels along its edge.
    Representing the cluster by its *mean* moves the entry several units off
    the brand colour, and then every pixel of the logo counts as changed — a
    2%% edge turns into a 20%% loss. The dominant colour has to survive intact.
    """
    def paint(x, y):
        if x < 30:
            return RED
        if x == 30:
            return (228, 136, 151, 255)  # the blend pixel
        return WHITE

    logo = image(64, 64, paint)
    result = quantise(logo, 3)
    assert (200, 16, 46) in result.palette, result.palette
    assert (255, 255, 255) in result.palette
    assert compare_rasters(logo, result.raster()).ratio < 0.05


def test_a_dominant_colour_does_not_swallow_the_palette():
    """Median cut alone cuts at the weighted median, which lands *inside* a
    colour holding more than half the image — leaving the two rarer colours to
    share one entry. Lloyd refinement is what pulls them apart."""
    def paint(x, y):
        if y < 52:
            return WHITE          # 81% of the image
        return BLACK if x < 32 else RED

    art = image(64, 64, paint)
    palette = quantise(art, 3).palette
    assert len(palette) == 3
    for expected in ((255, 255, 255), (26, 26, 26), (200, 16, 46)):
        assert expected in palette, palette


def test_a_photograph_is_represented_by_means_not_by_one_lucky_pixel():
    ramp = image(64, 64, lambda x, y: (x * 4, x * 4, x * 4, 255))
    result = quantise(ramp, 3)
    assert len(result.palette) == 3
    greys = sorted(color[0] for color in result.palette)
    assert greys[0] < 90 < greys[-1], greys  # spread across the ramp
    assert compare_rasters(ramp, result.raster()).ratio > 0.5  # a ramp really does lose


def test_asking_for_no_colours_is_a_programming_error():
    with pytest.raises(ValueError):
        quantise(image(2, 2, solid(RED)), 0)


# -- the metrics themselves --------------------------------------------------

def test_flat_separates_vector_art_from_shading():
    flat = image(32, 32, lambda x, y: BLACK if x < 16 else RED)
    shaded = image(32, 32, lambda x, y: (x * 8 % 256, y * 8 % 256, 40, 255))
    assert flat_ratio(flat) > 0.9
    assert flat_ratio(shaded) == 0.0
    assert unique_colors(flat) == 2


def test_edges_count_the_boundary_a_tracer_would_have_to_follow():
    one_seam = quantise(image(32, 32, lambda x, y: BLACK if x < 16 else RED), 2)
    checkerboard = quantise(image(32, 32, lambda x, y: BLACK if (x + y) % 2 else RED), 2)
    assert 0 < edge_density(one_seam) < 0.05
    assert edge_density(checkerboard) > 0.9


def test_thin_finds_the_features_a_needle_cannot_render():
    def bar(width):
        middle = 32
        return quantise(
            image(64, 64, lambda x, y: BLACK if abs(x - middle) < width / 2 else WHITE), 2
        )

    # A 9px bar survives a radius-2 opening (kernel 5); a 1px line does not.
    assert thin_ratio(bar(9), 2) == 0.0
    assert thin_ratio(bar(1), 2) > 0.0
    # Nothing at all is thin when the kernel is smaller than every feature.
    assert thin_ratio(bar(9), 1) == 0.0


def test_thin_is_a_fraction_of_the_whole_image_so_rows_compare():
    speckle = quantise(image(64, 64, lambda x, y: BLACK if (x + y) % 3 else WHITE), 2)
    assert thin_ratio(speckle, 1) > 0.9  # essentially all of it is unstitchable
    assert thin_ratio(quantise(image(64, 64, solid(RED)), 2), 1) == 0.0


def test_an_image_smaller_than_the_kernel_is_entirely_too_fine():
    tiny = quantise(image(3, 3, solid(RED)), 2)
    assert thin_ratio(tiny, 2) == 1.0
    assert thin_ratio(tiny, 0) == 0.0  # a kernel of nothing measures nothing
