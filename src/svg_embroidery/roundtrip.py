"""Proving that read → write does not damage a file.

This is the gate the whole fixing phase stands on. It answers four questions
about a document, in increasing order of importance:

``byte_identical``
    Did the bytes come back unchanged? Nice to have. Some normalisation is
    imposed by the XML parser and cannot be undone (see :mod:`writer`), so a
    ``False`` here is informative, not a failure.
``model_identical``
    Does re-parsing the output give the same tree — same elements, attributes,
    text, comments? A ``False`` here means we corrupted the document.
``checks_identical``
    Does the checker report exactly the same findings for input and output? A
    stronger, semantic version of the previous question.
``stable``
    Does writing twice give the same bytes as writing once? Without this, every
    fix would produce a diff full of unrelated churn.

A file passes when the last three hold.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple, Union
from xml.etree import ElementTree

from .document import SvgDocument, parse_svg
from .writer import serialize

#: Renderers we know how to drive, best first. Each entry is
#: (executable, argument builder). Used only to confirm the image is unchanged.
_RENDERERS = (
    ("resvg", lambda src, out: ["resvg", str(src), str(out)]),
    ("rsvg-convert", lambda src, out: ["rsvg-convert", "-o", str(out), str(src)]),
    ("inkscape", lambda src, out: ["inkscape", str(src), "--export-filename", str(out)]),
)


def tree_signature(element: ElementTree.Element, *, ordered_attributes: bool = False) -> Any:
    """A comparable structure describing everything semantic about a subtree."""
    tag = element.tag
    if tag is ElementTree.Comment:
        return ("#comment", element.text, element.tail)
    if tag is ElementTree.ProcessingInstruction:
        return ("#pi", element.text, element.tail)

    attributes: Any = (
        tuple(element.attrib.items())
        if ordered_attributes
        else tuple(sorted(element.attrib.items()))
    )
    return (
        tag,
        attributes,
        element.text,
        element.tail,
        tuple(
            tree_signature(child, ordered_attributes=ordered_attributes) for child in element
        ),
    )


def find_renderer() -> Optional[Tuple[str, Any]]:
    """First available SVG renderer, or ``None`` when the machine has none."""
    for name, builder in _RENDERERS:
        if shutil.which(name):
            return name, builder
    return None


def render_png(source: str, renderer: Optional[Tuple[str, Any]] = None) -> Optional[bytes]:
    """Render SVG text to PNG bytes, or ``None`` if no renderer is installed."""
    renderer = renderer or find_renderer()
    if renderer is None:
        return None
    _, builder = renderer
    with tempfile.TemporaryDirectory() as tmp:
        svg_path = Path(tmp) / "in.svg"
        png_path = Path(tmp) / "out.png"
        svg_path.write_text(source, encoding="utf-8")
        try:
            subprocess.run(
                builder(svg_path, png_path),
                check=True,
                capture_output=True,
                timeout=60,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        return png_path.read_bytes() if png_path.exists() else None


@dataclass
class RoundTripResult:
    """Outcome of writing one document back out."""

    name: str
    byte_identical: bool = False
    model_identical: bool = False
    attribute_order_kept: bool = False
    checks_identical: bool = False
    stable: bool = False
    #: ``None`` when no renderer is installed, so nothing could be compared.
    render_identical: Optional[bool] = None
    differences: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Passing means: unchanged document, unchanged verdict, stable output."""
        if not (self.model_identical and self.checks_identical and self.stable):
            return False
        return self.render_identical is not False

    def summary(self) -> str:
        if not self.ok:
            return f"FAIL  {self.name}: " + "; ".join(self.differences)
        notes = []
        if not self.byte_identical:
            notes.append("normalised")
        if self.render_identical is None:
            notes.append("render unchecked")
        suffix = f" ({', '.join(notes)})" if notes else " (byte-identical)"
        return f"ok    {self.name}{suffix}"


def _first_difference(left: Any, right: Any, path: str = "/svg") -> str:
    """Locate where two tree signatures diverge, for a useful error message."""
    if type(left) is not type(right) or not isinstance(left, tuple):
        return f"{path}: {left!r} != {right!r}"
    if left[0] != right[0]:
        return f"{path}: tag {left[0]!r} != {right[0]!r}"
    if len(left) < 5 or len(right) < 5:
        return f"{path}: {left!r} != {right!r}"
    if left[1] != right[1]:
        changed = set(left[1]) ^ set(right[1])
        return f"{path}: attributes differ: {sorted(changed)}"
    for index, label in ((2, "text"), (3, "tail")):
        if left[index] != right[index]:
            return f"{path}: {label} {left[index]!r} != {right[index]!r}"
    if len(left[4]) != len(right[4]):
        return f"{path}: {len(left[4])} children != {len(right[4])}"
    for index, (a, b) in enumerate(zip(left[4], right[4])):
        if a != b:
            tag = a[0] if isinstance(a, tuple) else "?"
            return _first_difference(a, b, f"{path}/{tag}[{index}]")
    return f"{path}: unknown difference"


def check_roundtrip(
    source: str,
    name: str = "<memory>",
    profiles: Sequence[str] = ("embroidery-basic",),
    check_render: bool = True,
) -> RoundTripResult:
    """Write ``source`` back out and report what, if anything, changed."""
    from .checker import Checker  # local import: checker imports document, not us
    from .profiles import load_profile

    result = RoundTripResult(name=name)

    original = parse_svg(source)
    written = serialize(original)
    result.byte_identical = written == source

    reparsed = parse_svg(written)
    before = tree_signature(original.root)
    after = tree_signature(reparsed.root)
    result.model_identical = before == after
    if not result.model_identical:
        result.differences.append("document model changed at " + _first_difference(before, after))

    result.attribute_order_kept = tree_signature(
        original.root, ordered_attributes=True
    ) == tree_signature(reparsed.root, ordered_attributes=True)
    if result.model_identical and not result.attribute_order_kept:
        result.differences.append("attribute order changed")

    result.stable = serialize(reparsed) == written
    if not result.stable:
        result.differences.append("writing twice differs from writing once")

    identical_checks = True
    for profile_name in profiles:
        checker = Checker(load_profile(profile_name))
        before_findings = [f.to_dict() for f in checker.check_source(source).findings]
        after_findings = [f.to_dict() for f in checker.check_source(written).findings]
        if before_findings != after_findings:
            identical_checks = False
            result.differences.append(f"checker findings changed under '{profile_name}'")
    result.checks_identical = identical_checks

    if check_render:
        renderer = find_renderer()
        if renderer is not None:
            before_png = render_png(source, renderer)
            after_png = render_png(written, renderer)
            if before_png is not None and after_png is not None:
                result.render_identical = before_png == after_png
                if not result.render_identical:
                    result.differences.append(f"rendered output changed ({renderer[0]})")

    return result


def check_file(path: Union[str, Path], **kwargs: Any) -> RoundTripResult:
    """Round-trip one file from disk."""
    file_path = Path(path)
    # Plain utf-8, not utf-8-sig: a BOM must reach the parser as a character so
    # it is recorded and written back.
    text = file_path.read_bytes().decode("utf-8")
    kwargs.setdefault("name", file_path.name)
    return check_roundtrip(text, **kwargs)
