"""A0 gate: every corpus file must survive a round trip intact.

Drop real exports from any design tool into ``tests/corpus/`` and they are
picked up automatically — the point of this gate is real files, and the ones
committed here are modelled on real exporter output rather than captured from it.
"""

from pathlib import Path

import pytest

from svg_embroidery.document import parse_svg
from svg_embroidery.profiles import list_profiles
from svg_embroidery.roundtrip import check_file, check_roundtrip, tree_signature
from svg_embroidery.visual import default_renderer
from svg_embroidery.writer import serialize

CORPUS = sorted((Path(__file__).parent / "corpus").glob("*.svg"))
ALL_PROFILES = tuple(profile.name for profile in list_profiles())


def test_corpus_is_not_empty():
    assert CORPUS, "the round-trip gate needs files in tests/corpus/"


@pytest.mark.parametrize("path", CORPUS, ids=lambda p: p.name)
def test_corpus_file_survives_roundtrip(path):
    """The gate itself: unchanged document, unchanged verdict, stable output."""
    result = check_file(path, profiles=ALL_PROFILES)
    assert result.ok, result.summary()
    assert result.attribute_order_kept


@pytest.mark.parametrize("path", CORPUS, ids=lambda p: p.name)
def test_corpus_file_is_byte_identical(path):
    """Stronger than the gate: untouched files should come back unchanged.

    Only ``handwritten-entities.svg`` is expected to differ — it uses character
    references, which the XML parser resolves before we ever see them.
    """
    result = check_file(path)
    if path.name == "handwritten-entities.svg":
        pytest.xfail("character references are resolved by the parser")
    assert result.byte_identical, "output differs from input"


def test_character_references_are_the_only_normalisation():
    source = (Path(__file__).parent / "corpus" / "handwritten-entities.svg").read_text(
        encoding="utf-8"
    )
    written = serialize(parse_svg(source))
    assert written != source
    # ...and the difference is confined to resolving the references.
    assert source.replace("&#233;", "é").replace("&#8212;", "—") == written


@pytest.mark.parametrize("path", CORPUS, ids=lambda p: p.name)
def test_writing_twice_changes_nothing_more(path):
    """Idempotence: without this, every fix would carry unrelated churn."""
    once = serialize(parse_svg(path.read_bytes().decode("utf-8")))
    twice = serialize(parse_svg(once))
    assert once == twice


def test_roundtrip_detects_a_real_change():
    """The harness must fail when the document actually changes."""
    source = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><g fill="#000"/></svg>'
    doc = parse_svg(source)
    before = tree_signature(doc.root)
    doc.root[0].set("fill", "#fff")
    assert tree_signature(doc.root) != before


def test_result_reports_where_it_broke():
    left = tree_signature(parse_svg('<svg xmlns="urn:x"><g id="a"/></svg>').root)
    right = tree_signature(parse_svg('<svg xmlns="urn:x"><g id="b"/></svg>').root)
    from svg_embroidery.roundtrip import _first_difference

    assert "attributes differ" in _first_difference(left, right)


def test_check_roundtrip_on_a_string():
    result = check_roundtrip('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"/>')
    assert result.ok
    assert result.byte_identical
    assert "ok" in result.summary()


def test_render_comparison_is_skipped_without_a_renderer():
    result = check_roundtrip('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"/>')
    if default_renderer() is None:
        assert result.render_identical is None
        assert result.ok  # a missing renderer must not fail the gate
    else:
        assert result.render_identical is True
