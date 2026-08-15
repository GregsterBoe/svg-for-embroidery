"""A0: the writer must not damage what it writes."""

from pathlib import Path

import pytest

from svg_embroidery.document import (
    SvgParseError,
    analyze_source,
    build_verbatim_index,
    parse_svg,
    scan_start_tags,
)
from svg_embroidery.writer import WriterError, serialize, write_svg


def roundtrip(source: str) -> str:
    return serialize(parse_svg(source))


# -- what the source format capture has to notice -------------------------

def test_prologue_and_epilogue_are_kept():
    source = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!-- made by hand -->\n"
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"/>\n'
        "<!-- goodbye -->\n"
    )
    assert roundtrip(source) == source


def test_doctype_is_kept():
    source = (
        '<?xml version="1.0"?>\n'
        '<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" '
        '"http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">\n'
        '<svg xmlns="http://www.w3.org/2000/svg"/>'
    )
    assert roundtrip(source) == source


def test_internal_entity_subset_is_refused_not_mangled():
    # The parser expands &logo; on read, so writing back would silently inline
    # it and drop the declaration. Refuse loudly instead.
    source = (
        '<!DOCTYPE svg [<!ENTITY logo "ACME">]>\n'
        '<svg xmlns="http://www.w3.org/2000/svg"><desc>&logo;</desc></svg>'
    )
    with pytest.raises(WriterError, match="internal DTD subset"):
        roundtrip(source)


def test_crlf_and_bom_survive():
    source = '﻿<svg xmlns="http://www.w3.org/2000/svg">\r\n  <desc>x</desc>\r\n</svg>\r\n'
    assert roundtrip(source) == source


def test_conflicting_namespace_prefix_is_refused():
    source = (
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:a="urn:one">'
        '<g xmlns:a="urn:two" a:x="1"/></svg>'
    )
    with pytest.raises(SvgParseError, match="declared twice"):
        parse_svg(source)


def test_unused_namespace_declaration_survives():
    source = '<svg xmlns="http://www.w3.org/2000/svg" xmlns:dc="http://purl.org/dc/elements/1.1/"/>'
    assert "dc" in parse_svg(source).format.nsmap
    assert roundtrip(source) == source


# -- verbatim spans --------------------------------------------------------

def test_scan_start_tags_skips_comments_pis_and_cdata():
    source = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        "<!-- <fake/> --><?pi <fake/>?><![CDATA[ <fake/> ]]>"
        "<g><path/></g></svg>"
    )
    assert len(scan_start_tags(source)) == 3  # svg, g, path


def test_multiline_start_tags_are_reproduced_exactly():
    source = (
        '<svg xmlns="http://www.w3.org/2000/svg"\n'
        '     width="10cm"\n'
        '     height="10cm">\n'
        "  <path\n"
        '     d="M0 0 Z"\n'
        '     fill="#000"/>\n'
        "</svg>"
    )
    assert roundtrip(source) == source


def test_empty_element_spelling_is_preserved():
    source = '<svg xmlns="http://www.w3.org/2000/svg"><rect></rect><circle/></svg>'
    assert roundtrip(source) == source


def test_single_quoted_attributes_are_preserved_when_untouched():
    source = "<svg xmlns='http://www.w3.org/2000/svg'><g fill='#abc'/></svg>"
    assert roundtrip(source) == source


def test_editing_an_attribute_rewrites_only_that_element():
    source = (
        '<svg xmlns="http://www.w3.org/2000/svg"\n'
        '     width="5cm">\n'
        '  <g id="a"\n     fill="#000"/>\n'
        '  <g id="b"\n     fill="#111"/>\n'
        "</svg>"
    )
    doc = parse_svg(source)
    target = doc.root[0]
    target.set("fill", "#222")
    written = serialize(doc)

    assert '<g id="a" fill="#222"/>' in written  # rewritten, on one line
    assert '<g id="b"\n     fill="#111"/>' in written  # untouched, byte for byte
    assert written.count("\n") == source.count("\n") - 1


def test_verbatim_index_gives_up_rather_than_guessing():
    # A tag count mismatch must disable verbatim mode, not misalign it.
    doc = parse_svg('<svg xmlns="http://www.w3.org/2000/svg"><g/></svg>')
    assert build_verbatim_index(doc.root, "<svg><g/><extra/></svg>") is None


def test_namespace_declared_on_a_rewritten_element_is_hoisted():
    # xmlns:extra lives on the <g>; rewriting that <g> drops the declaration,
    # so it has to reappear on the root or the file becomes invalid.
    source = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<g xmlns:extra="urn:extra" extra:note="keep" fill="#000"/></svg>'
    )
    doc = parse_svg(source)
    doc.root[0].set("fill", "#111")
    written = serialize(doc)

    assert 'xmlns:extra="urn:extra"' in written
    reparsed = parse_svg(written)
    assert reparsed.root[0].get("{urn:extra}note") == "keep"


def test_same_namespace_declared_twice_keeps_element_names():
    # Inkscape declares both xmlns and xmlns:svg for the SVG namespace.
    source = (
        '<svg xmlns:svg="http://www.w3.org/2000/svg" xmlns="http://www.w3.org/2000/svg">'
        "<metadata>x</metadata></svg>"
    )
    written = roundtrip(source)
    assert "</metadata>" in written and "svg:metadata" not in written


def test_attributes_still_get_a_real_prefix():
    source = (
        '<svg xmlns:svg="http://www.w3.org/2000/svg" xmlns="http://www.w3.org/2000/svg"/>'
    )
    doc = parse_svg(source)
    doc.root.set("{http://www.w3.org/2000/svg}custom", "1")
    assert 'svg:custom="1"' in serialize(doc)


def test_escaping_round_trips():
    source = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<desc>a &amp; b &lt; c</desc><g data-x="&quot;q&quot; &amp; &lt;"/></svg>'
    )
    assert roundtrip(source) == source


# -- writing files ---------------------------------------------------------

def test_write_svg_refuses_to_clobber(tmp_path):
    target = tmp_path / "out.svg"
    target.write_text("existing", encoding="utf-8")
    doc = parse_svg('<svg xmlns="http://www.w3.org/2000/svg"/>')

    with pytest.raises(WriterError, match="already exists"):
        write_svg(doc, target)

    write_svg(doc, target, overwrite=True, backup=True)
    assert target.read_text(encoding="utf-8").startswith("<svg")
    assert (tmp_path / "out.svg.bak").read_text(encoding="utf-8") == "existing"


def test_written_file_keeps_line_endings_and_bom(tmp_path):
    source = '﻿<svg xmlns="http://www.w3.org/2000/svg">\r\n</svg>\r\n'
    doc = parse_svg(source)
    target = write_svg(doc, tmp_path / "out.svg")
    raw = target.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in raw


def test_analyze_source_reports_the_format():
    fmt, normalised = analyze_source('﻿<?xml version="1.0"?>\r\n<svg xmlns="urn:x"/>')
    assert fmt.bom is True
    assert fmt.newline == "\r\n"
    assert "\r" not in normalised
    assert fmt.prologue.startswith("<?xml")
