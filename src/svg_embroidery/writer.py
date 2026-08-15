"""Writing SVG documents back out.

This exists before any fixer does, because a fixer that silently corrupts files
is worse than no fixer at all. The rule here is: change what was asked for and
nothing else.

We do not use :func:`ElementTree.tostring`. It rewrites namespace prefixes to
``ns0``/``ns1`` via a process-global registry, which would rewrite every tag in
an Inkscape file. This serialiser resolves prefixes per document from the
declarations the file itself carried.

An element nobody edited is copied straight from the source text, using the
spans recorded in :class:`~svg_embroidery.document.VerbatimIndex`. So its start
tag keeps whatever the author wrote: line breaks between attributes, quote
style, and whether an empty element was ``<rect/>`` or ``<rect></rect>``. Text
and tail whitespace come from the parser unchanged, so indentation survives
too, along with comments, processing instructions, the XML declaration, the
DOCTYPE, unused namespace declarations, line endings and the byte order mark.

Two things still change.

*Anywhere in the file*, character references in text content are resolved by
the parser before we see them, so ``&#233;`` comes back as ``é``. It denotes
the same character; only the spelling differs.

*In an element a fixer edited*, the start tag is rebuilt rather than copied:
attributes are re-quoted with double quotes, newlines inside attribute values
become spaces, an empty element self-closes, and any namespace declared on that
element moves to the root (the parser consumed it). The rebuilt tag is
semantically identical to what it replaced — and it is only ever the tags that
actually changed, so a fix produces a small diff.

:mod:`svg_embroidery.roundtrip` proves all of this per file rather than
asserting it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union
from xml.etree import ElementTree

from .document import _XMLNS_RE, XML_NS, SourceFormat, SvgDocument, VerbatimIndex

#: Namespaces that never need declaring.
IMPLICIT_PREFIXES = {XML_NS: "xml"}

_TAG_NAME_RE = re.compile(r"<([^\s/>]+)")


class WriterError(Exception):
    """Raised when a document cannot be written back safely."""


def _source_tag_name(start_tag: str) -> str:
    """The qualified name out of a literal start tag, e.g. ``<svg:g ...>``."""
    match = _TAG_NAME_RE.match(start_tag)
    if not match:  # pragma: no cover - scanner only records real start tags
        raise WriterError(f"cannot read a tag name from {start_tag[:40]!r}")
    return match.group(1)


def _escape_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _escape_attribute(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("\n", "&#10;")
        .replace("\r", "&#13;")
        .replace("\t", "&#9;")
    )


class _PrefixResolver:
    """Maps namespace URIs back to the prefixes the source file used."""

    def __init__(self, nsmap: Dict[str, str]) -> None:
        self.uri_to_prefix: Dict[str, str] = dict(IMPLICIT_PREFIXES)
        #: Attributes cannot use the default namespace, so they need a real
        #: prefix even when one URI is declared both ways (Inkscape does this:
        #: xmlns and xmlns:svg both point at the SVG namespace).
        self.uri_to_attribute_prefix: Dict[str, str] = {}
        for prefix, uri in nsmap.items():
            if prefix:
                self.uri_to_attribute_prefix.setdefault(uri, prefix)
            # The default namespace wins for element names — it is what the
            # file's own tags use.
            if uri not in self.uri_to_prefix or prefix == "":
                self.uri_to_prefix[uri] = prefix
        self.generated: Dict[str, str] = {}
        #: Prefixes relied on by elements we rewrote rather than copied.
        self.used_in_rewrite: set = set()
        self._counter = 0

    def prefix_for(self, uri: str, *, attribute: bool) -> str:
        """Prefix for ``uri``. Attributes never inherit the default namespace."""
        prefix = self.uri_to_prefix.get(uri)
        if attribute and prefix == "":
            prefix = self.uri_to_attribute_prefix.get(uri)
        if prefix:
            return prefix
        if prefix == "" and not attribute:
            return prefix
        known = self.generated.get(uri)
        if known is not None:
            return known
        while True:
            self._counter += 1
            candidate = f"ns{self._counter}"
            if candidate not in self.uri_to_prefix.values() and candidate not in self.generated.values():
                break
        self.generated[uri] = candidate
        return candidate

    def qualify(self, tag: str, *, attribute: bool = False, rewriting: bool = False) -> str:
        if not tag.startswith("{"):
            return tag
        uri, _, local = tag[1:].partition("}")
        prefix = self.prefix_for(uri, attribute=attribute)
        if rewriting:
            # Remember what a rewritten tag depends on: the declaration may have
            # lived on the element we just replaced.
            self.used_in_rewrite.add(prefix)
        return f"{prefix}:{local}" if prefix else local


_ATTRIBUTE_RE = re.compile(r"""\s+([^\s=/>]+)\s*=\s*("[^"]*"|'[^']*')""")


def _tag_without_attributes(start_tag: str, drop: Sequence[str]) -> Optional[str]:
    """Cut named attributes out of a literal start tag, keeping its layout.

    Returns ``None`` if any of them cannot be found, so the caller rebuilds the
    tag rather than writing something half-edited.
    """
    remaining = set(drop)
    pieces: List[str] = []
    position = 0
    for match in _ATTRIBUTE_RE.finditer(start_tag):
        if match.group(1) in remaining:
            pieces.append(start_tag[position : match.start()])
            position = match.end()
            remaining.discard(match.group(1))
    if remaining:
        return None
    pieces.append(start_tag[position:])
    return "".join(pieces)


def _reusable_start_tag(
    element: ElementTree.Element,
    verbatim: Optional[VerbatimIndex],
    resolver: _PrefixResolver,
) -> Optional[Tuple[str, bool]]:
    """The element's original start tag, if it can still be used verbatim.

    Either nothing changed, or the only change was attributes being removed —
    which is scissors work on the original text.
    """
    if verbatim is None or id(element) not in verbatim.spans:
        return None
    source_tag, self_closing = verbatim.text_for(element)
    if verbatim.unchanged(element):
        return source_tag, self_closing

    dropped = verbatim.removed_attributes(element)
    if dropped is None:
        return None
    names = [resolver.qualify(key, attribute=True) for key in dropped]
    edited = _tag_without_attributes(source_tag, names)
    return (edited, self_closing) if edited is not None else None


def _write_element(
    element: ElementTree.Element,
    out: List[str],
    resolver: _PrefixResolver,
    verbatim: Optional[VerbatimIndex] = None,
) -> None:
    tag = element.tag

    if tag is ElementTree.Comment:
        out.append(f"<!--{element.text or ''}-->")
        if element.tail:
            out.append(_escape_text(element.tail))
        return
    if tag is ElementTree.ProcessingInstruction:
        out.append(f"<?{element.text or ''}?>")
        if element.tail:
            out.append(_escape_text(element.tail))
        return
    if not isinstance(tag, str):
        raise WriterError(f"cannot serialise element with tag {tag!r}")

    children = list(element)

    # An untouched element is reproduced exactly as the author wrote it, so a
    # fix produces a diff of the lines it actually changed and nothing else.
    reusable = _reusable_start_tag(element, verbatim, resolver)
    if reusable is not None:
        source_tag, self_closing = reusable
        out.append(source_tag)
        if not self_closing:
            if element.text:
                out.append(_escape_text(element.text))
            for child in children:
                _write_element(child, out, resolver, verbatim=verbatim)
            # Close with the name the source opened with: a file may declare the
            # same namespace twice and open the tag with either prefix.
            out.append(f"</{_source_tag_name(source_tag)}>")
        if element.tail:
            out.append(_escape_text(element.tail))
        return

    name = resolver.qualify(tag, rewriting=True)
    out.append(f"<{name}")
    for key, value in element.attrib.items():
        qualified = resolver.qualify(key, attribute=True, rewriting=True)
        out.append(f' {qualified}="{_escape_attribute(value)}"')

    if not children and not element.text:
        out.append("/>")
    else:
        out.append(">")
        if element.text:
            out.append(_escape_text(element.text))
        for child in children:
            _write_element(child, out, resolver, verbatim=verbatim)
        out.append(f"</{name}>")

    if element.tail:
        out.append(_escape_text(element.tail))


def _declarations(nsmap: Dict[str, str], resolver: _PrefixResolver) -> str:
    parts = []
    for prefix, uri in nsmap.items():
        attribute = "xmlns" if not prefix else f"xmlns:{prefix}"
        parts.append(f' {attribute}="{_escape_attribute(uri)}"')
    # Namespaces used by the tree but never declared in the source.
    for uri, prefix in resolver.generated.items():
        parts.append(f' xmlns:{prefix}="{_escape_attribute(uri)}"')
    return "".join(parts)


def _root_declared_prefixes(start_tag: Optional[str]) -> Optional[set]:
    """Prefixes declared on the root's own start tag, or ``None`` if unknown."""
    if start_tag is None:
        return None
    return {match.group(1) or "" for match in _XMLNS_RE.finditer(start_tag)} | {"", "xml"}


def _serialize_body(
    doc: SvgDocument,
    resolver: _PrefixResolver,
    root_tag: Optional[Tuple[str, bool]],
) -> str:
    fmt = doc.format
    root = doc.root
    verbatim = doc.verbatim
    children = list(root)

    body: List[str] = []
    if root.text:
        body.append(_escape_text(root.text))
    for child in children:
        _write_element(child, body, resolver, verbatim=verbatim)

    if root_tag is not None:
        start_tag, self_closing = root_tag
        if self_closing:
            return start_tag
        return start_tag + "".join(body) + f"</{_source_tag_name(start_tag)}>"

    name = resolver.qualify(root.tag, rewriting=True)
    head = [f"<{name}", "\x00"]
    for key, value in root.attrib.items():
        qualified = resolver.qualify(key, attribute=True, rewriting=True)
        head.append(f' {qualified}="{_escape_attribute(value)}"')
    if not children and not root.text:
        return "".join(head) + "/>"
    return "".join(head) + ">" + "".join(body) + f"</{name}>"


def serialize(doc: SvgDocument) -> str:
    """Render ``doc`` back to SVG source text."""
    fmt: SourceFormat = doc.format
    if fmt.has_internal_subset:
        raise WriterError(
            "the file declares entities in an internal DTD subset; the parser "
            "expands those on read, so writing it back would inline them and drop "
            "the declaration. Refusing to write."
        )

    resolver = _PrefixResolver(fmt.nsmap)
    root_tag = _reusable_start_tag(doc.root, doc.verbatim, resolver)
    declared_on_root = _root_declared_prefixes(root_tag[0] if root_tag else None)
    rendered = _serialize_body(doc, resolver, root_tag)

    # Keeping the original root tag is only safe while every prefix a rewritten
    # element needs is declared there. A declaration that lived on a rewritten
    # element itself is gone, so fall back to hoisting them all to the root.
    if root_tag is not None and (
        resolver.generated or not resolver.used_in_rewrite <= (declared_on_root or set())
    ):
        resolver = _PrefixResolver(fmt.nsmap)
        rendered = _serialize_body(doc, resolver, root_tag=None)

    if "\x00" in rendered:
        rendered = rendered.replace("\x00", _declarations(fmt.nsmap, resolver), 1)

    text = fmt.prologue + rendered + fmt.epilogue
    if fmt.newline != "\n":
        text = text.replace("\n", fmt.newline)
    if fmt.bom:
        text = "﻿" + text
    return text


def write_svg(
    doc: SvgDocument,
    path: Union[str, Path],
    *,
    overwrite: bool = False,
    backup: bool = False,
) -> Path:
    """Write ``doc`` to ``path``.

    Refuses to overwrite an existing file unless ``overwrite`` is set, and can
    keep a ``.bak`` copy of what was there before.
    """
    target = Path(path)
    if target.exists():
        if not overwrite:
            raise WriterError(f"{target} already exists (pass overwrite=True to replace it)")
        if backup:
            backup_path = target.with_suffix(target.suffix + ".bak")
            backup_path.write_bytes(target.read_bytes())

    text = serialize(doc)
    try:
        target.write_bytes(text.encode(doc.format.encoding))
    except (LookupError, UnicodeEncodeError) as exc:
        raise WriterError(f"cannot encode document as {doc.format.encoding}: {exc}") from exc
    return target
