"""Tree surgery helpers shared by the fixers.

The recurring question when removing something is *does anything still point at
it?* An unreferenced ``<linearGradient>`` sitting in ``<defs>`` draws nothing,
so deleting it cannot change the image. The same gradient referenced by a
``fill`` is load-bearing, and removing it is a different (lossy) operation.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Sequence, Set
from xml.etree import ElementTree

from ..document import SVG_NS, XLINK_NS, local_name

#: Elements that never paint on their own — they only do anything when
#: something references them by id.
REFERENCE_ONLY_TAGS = frozenset(
    {"linearGradient", "radialGradient", "meshgradient", "pattern", "filter",
     "clipPath", "mask", "marker", "symbol"}
)

_URL_REFERENCE_RE = re.compile(r"url\(\s*['\"]?#([^)'\"\s]+)", re.IGNORECASE)


def parent_map(root: ElementTree.Element) -> Dict[int, ElementTree.Element]:
    """``id(child) -> parent``. ElementTree has no parent pointers."""
    parents: Dict[int, ElementTree.Element] = {}
    for parent in root.iter():
        for child in parent:
            parents[id(child)] = parent
    return parents


def referenced_ids(root: ElementTree.Element) -> Set[str]:
    """Every id the document points at, however it points."""
    found: Set[str] = set()
    href_names = ("href", f"{{{XLINK_NS}}}href", f"{{{SVG_NS}}}href")
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        for name, value in element.attrib.items():
            if not value:
                continue
            found.update(_URL_REFERENCE_RE.findall(value))
            if name in href_names and value.startswith("#"):
                found.add(value[1:])
    return found


def is_inside_defs(element: ElementTree.Element, parents: Dict[int, ElementTree.Element]) -> bool:
    current = parents.get(id(element))
    while current is not None:
        if local_name(current.tag) == "defs":
            return True
        current = parents.get(id(current))
    return False


def is_unrendered(
    element: ElementTree.Element, parents: Dict[int, ElementTree.Element]
) -> bool:
    """True when this element cannot be drawing anything on the canvas.

    Either it lives in ``<defs>``, or it is one of the tags that only render
    through a reference, or it (or an ancestor) is hidden.
    """
    if local_name(element.tag) in REFERENCE_ONLY_TAGS:
        return True
    if is_inside_defs(element, parents):
        return True

    current: ElementTree.Element | None = element
    while current is not None:
        style = (current.get("style") or "").replace(" ", "").lower()
        if current.get("display", "").strip().lower() == "none" or "display:none" in style:
            return True
        current = parents.get(id(current))
    return False


def remove_elements(
    root: ElementTree.Element, elements: Sequence[ElementTree.Element]
) -> int:
    """Detach elements from their parents. Returns how many were removed."""
    parents = parent_map(root)
    removed = 0
    for element in elements:
        parent = parents.get(id(element))
        if parent is not None:
            parent.remove(element)
            removed += 1
    return removed


def prune_unreferenced(
    root: ElementTree.Element, candidates_for: Iterable[str]
) -> List[ElementTree.Element]:
    """Remove elements of the given tags that nothing references.

    Repeats until nothing more can go: a gradient may be referenced only by
    another gradient that is itself dead, and a single pass would leave it
    behind — which would also mean a second fix run kept changing the file.
    """
    wanted = {str(tag) for tag in candidates_for}
    removed: List[ElementTree.Element] = []

    while True:
        parents = parent_map(root)
        references = referenced_ids(root)
        doomed = [
            element
            for element in root.iter()
            if isinstance(element.tag, str)
            and local_name(element.tag) in wanted
            and element.get("id") not in references
            and id(element) in parents  # never the root itself
        ]
        if not doomed:
            return removed
        remove_elements(root, doomed)
        removed.extend(doomed)
