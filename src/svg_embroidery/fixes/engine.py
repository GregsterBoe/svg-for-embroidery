"""Running fixers over a document, and proving the result.

The engine does four things in order: check, repair what it is allowed to
repair, re-check, and then *verify its own work*. That last step is what A1 was
built for — a fix claiming to be ``SAFE`` is rendered before and after, and if a
pixel moved, the run is reported as failed instead of written out.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Sequence, Set

from ..checker import Checker
from ..document import SvgDocument, parse_svg
from ..findings import Report, Severity
from ..profiles import Profile, load_profile
from ..visual import Difference, RenderError, visual_difference
from ..writer import serialize
from .base import DEFAULT_ALLOWED, Change, Fixer, Risk, build_fixer


@dataclass
class AppliedFix:
    rule_id: str
    risk: Risk
    changes: List[Change]

    def __str__(self) -> str:
        return f"{self.rule_id} [{self.risk.value}]: " + "; ".join(
            str(change) for change in self.changes
        )


@dataclass
class SkippedFix:
    rule_id: str
    reason: str
    risk: Optional[Risk] = None

    def __str__(self) -> str:
        risk = f" [{self.risk.value}]" if self.risk else ""
        return f"{self.rule_id}{risk}: {self.reason}"


@dataclass
class FixReport:
    """Everything that happened during one fix run."""

    profile: str
    source_before: str
    source_after: str
    before: Report
    after: Optional[Report] = None
    applied: List[AppliedFix] = field(default_factory=list)
    skipped: List[SkippedFix] = field(default_factory=list)
    #: Measured pixel difference; ``None`` when no renderer is installed.
    visual: Optional[Difference] = None
    #: Set when the engine caught its own output misbehaving.
    verification_error: Optional[str] = None
    file: Optional[Path] = None

    @property
    def changed(self) -> bool:
        return self.source_before != self.source_after

    @property
    def ok(self) -> bool:
        """False when verification caught a problem — do not write the output."""
        return self.verification_error is None

    @property
    def fixed_rules(self) -> List[str]:
        """Rules that were failing before and are clean afterwards."""
        if self.after is None:
            return []
        before = {finding.rule_id for finding in self.before.errors + self.before.warnings}
        after = {finding.rule_id for finding in self.after.errors + self.after.warnings}
        return sorted(before - after)

    @property
    def remaining_rules(self) -> List[str]:
        if self.after is None:
            return []
        return sorted({finding.rule_id for finding in self.after.errors})

    def diff(self, context: int = 3) -> str:
        """Unified diff of the file, for a dry run."""
        name = self.file.name if self.file else "design.svg"
        return "".join(
            difflib.unified_diff(
                self.source_before.splitlines(keepends=True),
                self.source_after.splitlines(keepends=True),
                fromfile=f"a/{name}",
                tofile=f"b/{name}",
                n=context,
            )
        )

    def summary(self) -> str:
        lines: List[str] = []
        if not self.ok:
            lines.append(f"verification FAILED: {self.verification_error}")
        if not self.applied:
            lines.append("no fixes applied")
        for fix in self.applied:
            lines.append(f"fixed  {fix}")
        for skip in self.skipped:
            lines.append(f"skip   {skip}")
        if self.after is not None:
            before_errors = len(self.before.errors)
            after_errors = len(self.after.errors)
            lines.append(f"errors: {before_errors} -> {after_errors}")
        if self.visual is not None:
            lines.append(f"visual: {self.visual}")
        return "\n".join(lines)


class FixEngine:
    """Applies the fixers a profile's rules have, within a risk budget."""

    def __init__(
        self,
        profile: Profile,
        allow: Sequence[Risk] = tuple(DEFAULT_ALLOWED),
        only: Optional[Set[str]] = None,
        verify: bool = True,
        visual_budget: float = 0.0,
    ) -> None:
        self.profile = profile
        self.checker = Checker(profile)
        self.allow: FrozenSet[Risk] = frozenset(allow)
        self.only = set(only) if only else None
        self.verify = verify
        #: Fraction of pixels a non-safe fix may change before it counts as wrong.
        self.visual_budget = visual_budget

    @classmethod
    def from_profile_name(cls, name: str, **kwargs) -> "FixEngine":
        return cls(load_profile(name), **kwargs)

    # -- internals --------------------------------------------------------
    def _failing_rule_ids(self, report: Report) -> Set[str]:
        return {
            finding.rule_id
            for finding in report.findings
            if finding.severity in (Severity.ERROR, Severity.WARNING)
        }

    def _errors_per_rule(self, report: Report) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for finding in report.errors:
            counts[finding.rule_id] = counts.get(finding.rule_id, 0) + 1
        return counts

    def _verify(self, report: FixReport) -> None:
        """Check the engine's own output before anyone trusts it."""
        if report.after is None:  # pragma: no cover - always set by fix_source
            return

        if not report.applied and report.changed:
            report.verification_error = (
                "no fix was applied but the file changed anyway"
            )
            return

        before = self._errors_per_rule(report.before)
        after = self._errors_per_rule(report.after)
        worsened = [
            rule_id
            for rule_id, count in after.items()
            if count > before.get(rule_id, 0)
        ]
        if worsened:
            report.verification_error = (
                f"fixing introduced new errors in: {', '.join(sorted(worsened))}"
            )
            return

        if report.visual is None or not report.applied:
            return

        riskiest = max((fix.risk for fix in report.applied), key=lambda r: r.rank)
        if riskiest is Risk.SAFE:
            if not report.visual.identical:
                report.verification_error = (
                    f"a safe fix changed the rendered image ({report.visual})"
                )
        elif not report.visual.within(self.visual_budget):
            report.verification_error = (
                f"visual change {report.visual} exceeds the budget "
                f"({self.visual_budget:.2%})"
            )

    # -- the run ----------------------------------------------------------
    def fix_source(self, source: str, path: Optional[Path] = None) -> FixReport:
        doc = parse_svg(source, path=path)
        before = self.checker.check_document(doc)
        report = FixReport(
            profile=self.profile.name,
            source_before=source,
            source_after=source,
            before=before,
            file=path,
        )

        failing = self._failing_rule_ids(before)
        current_source = source

        for rule in self.checker.rules:
            if rule.id not in failing:
                continue

            fixer: Optional[Fixer] = build_fixer(rule)
            if fixer is None:
                report.skipped.append(SkippedFix(rule.id, "no automatic fix available"))
                continue
            if self.only is not None and rule.id not in self.only:
                report.skipped.append(SkippedFix(rule.id, "not selected", fixer.risk))
                continue
            if fixer.risk not in self.allow:
                report.skipped.append(
                    SkippedFix(rule.id, f"needs --allow {fixer.risk.value}", fixer.risk)
                )
                continue

            outcome = fixer.apply(doc)
            if not outcome.applied:
                report.skipped.append(
                    SkippedFix(
                        rule.id, outcome.declined or "fixer made no changes", fixer.risk
                    )
                )
                continue

            report.applied.append(AppliedFix(rule.id, fixer.risk, outcome.changes))
            # Re-parse between fixers: the document model caches resolved styles
            # and geometry, so the next fixer must not read a stale view.
            current_source = serialize(doc)
            doc = parse_svg(current_source, path=path)

        report.source_after = current_source
        report.after = self.checker.check_document(doc)

        if self.verify:
            if report.changed:
                try:
                    report.visual = visual_difference(source, current_source)
                except RenderError as exc:
                    report.skipped.append(SkippedFix("render", f"could not render: {exc}"))
            self._verify(report)
        return report

    def fix_document(self, doc: SvgDocument) -> FixReport:
        return self.fix_source(serialize(doc), path=doc.path)

    def fix_file(self, path) -> FixReport:
        file_path = Path(path)
        source = file_path.read_bytes().decode("utf-8")
        return self.fix_source(source, path=file_path)
