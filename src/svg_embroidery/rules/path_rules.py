"""Rules about path geometry."""

from __future__ import annotations

from typing import Iterable, List

from ..document import SvgDocument, iter_subpaths
from ..findings import Finding, Severity
from .base import Rule, register

#: Shapes that are closed by definition and never need a "Z".
IMPLICITLY_CLOSED = ("rect", "circle", "ellipse", "polygon")
#: Shapes that can never be closed outlines.
OPEN_SHAPES = ("line", "polyline")


@register
class ClosedPathsRule(Rule):
    id = "path.closed"
    summary = "Every path (and subpath) must be closed"
    params = {"check_subpaths": True, "allow_open_shapes": False}

    def check(self, doc: SvgDocument) -> Iterable[Finding]:
        findings: List[Finding] = []
        paths = doc.by_tag("path")
        open_paths = 0

        for node in paths:
            d = (node.element.get("d") or "").strip()
            if not d:
                findings.append(
                    self.fail(
                        f"{node.label} has an empty 'd' attribute.",
                        location=node.location,
                    )
                )
                continue
            if self.config["check_subpaths"]:
                pieces = list(iter_subpaths(d))
                unclosed = [p for p in pieces if not p.rstrip().lower().endswith("z")]
                if unclosed:
                    open_paths += 1
                    findings.append(
                        self.fail(
                            f"{node.label}: {len(unclosed)} of {len(pieces)} subpath(s) are not "
                            "closed (missing 'Z').",
                            hint="Close every contour; open outlines cannot be filled reliably.",
                            location=node.location,
                            unclosed=len(unclosed),
                        )
                    )
            elif not d.rstrip().lower().endswith("z"):
                open_paths += 1
                findings.append(
                    self.fail(
                        f"{node.label} does not end with 'Z'.",
                        hint="Close the contour before exporting.",
                        location=node.location,
                    )
                )

        if not self.config["allow_open_shapes"]:
            for node in doc.by_tag(*OPEN_SHAPES):
                findings.append(
                    self.fail(
                        f"{node.label} is an inherently open shape.",
                        hint="Convert lines/polylines into closed, filled outlines.",
                        location=node.location,
                    )
                )

        if findings:
            return findings

        implicit = doc.by_tag(*IMPLICITLY_CLOSED)
        if not paths and not implicit:
            return [
                self.warn(
                    "No path or shape elements found — the file appears to be empty.",
                )
            ]
        return [
            self.ok(
                f"All contours closed ({len(paths)} path(s), "
                f"{len(implicit)} implicitly closed shape(s))."
            )
        ]


@register
class PathCountRule(Rule):
    id = "path.max_count"
    summary = "Limit how many paths a design may contain"
    params = {"max_paths": 200}
    default_severity = Severity.WARNING

    def check(self, doc: SvgDocument) -> Iterable[Finding]:
        count = len(doc.by_tag("path", *IMPLICITLY_CLOSED))
        limit = int(self.config["max_paths"])
        if count > limit:
            return [
                self.fail(
                    f"{count} shapes found, more than the recommended {limit}.",
                    hint="Very detailed designs stitch badly; simplify the artwork.",
                    count=count,
                )
            ]
        return [self.ok(f"Shape count OK: {count} of max {limit}.")]
