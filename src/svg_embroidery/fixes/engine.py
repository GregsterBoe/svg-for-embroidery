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
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Set

from ..checker import Checker
from ..document import SvgDocument, parse_svg
from ..findings import Finding, Report, Severity
from ..profiles import Profile, load_profile
from ..visual import Difference, RenderError, visual_difference
from ..writer import serialize
from .base import DEFAULT_ALLOWED, Change, Risk, build_fixers


@dataclass
class AppliedFix:
    rule_id: str
    risk: Risk
    changes: List[Change]
    #: How much of the image this fix was allowed to change.
    budget: float = 0.0

    def __str__(self) -> str:
        return f"{self.rule_id} [{self.risk.value}]: " + "; ".join(
            str(change) for change in self.changes
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule": self.rule_id,
            "risk": self.risk.value,
            "changes": [
                {"description": change.description, "location": change.location}
                for change in self.changes
            ],
        }


@dataclass
class SkippedFix:
    rule_id: str
    reason: str
    risk: Optional[Risk] = None

    def __str__(self) -> str:
        risk = f" [{self.risk.value}]" if self.risk else ""
        return f"{self.rule_id}{risk}: {self.reason}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule": self.rule_id,
            "risk": self.risk.value if self.risk else None,
            "reason": self.reason,
        }


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

    @property
    def unmeasured(self) -> List[Finding]:
        """Checks this machine could not run.

        Reported alongside the skips because they are the same kind of news: not
        a fault in the design, but something the run could not do — and, unlike
        most skips, one an install fixes.
        """
        return self.before.unmeasured

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

    def to_dict(
        self,
        strict: bool = False,
        include_source: bool = False,
        include_diff: bool = False,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "file": str(self.file) if self.file else None,
            "profile": self.profile,
            "ok": self.ok,
            "changed": self.changed,
            "verification_error": self.verification_error,
            "applied": [fix.to_dict() for fix in self.applied],
            "skipped": [skip.to_dict() for skip in self.skipped],
            "unmeasured": [finding.to_dict() for finding in self.unmeasured],
            "fixed_rules": self.fixed_rules,
            "remaining_rules": self.remaining_rules,
            "before": self.before.to_dict(strict=strict),
            "after": None if self.after is None else self.after.to_dict(strict=strict),
            "visual": None if self.visual is None else self.visual.to_dict(),
        }
        if include_source:
            payload["svg"] = self.source_after
        if include_diff:
            payload["diff"] = self.diff()
        return payload


class FixEngine:
    """Applies the fixers a profile's rules have, within a risk budget."""

    def __init__(
        self,
        profile: Profile,
        allow: Sequence[Risk] = tuple(DEFAULT_ALLOWED),
        only: Optional[Set[str]] = None,
        verify: bool = True,
        visual_budget: Optional[float] = None,
    ) -> None:
        self.profile = profile
        self.checker = Checker(profile)
        self.allow: FrozenSet[Risk] = frozenset(allow)
        self.only = set(only) if only else None
        self.verify = verify
        #: Overrides each fixer's declared budget when set.
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

    def _regression(self, before: Dict[str, int], after: Dict[str, int]) -> str:
        """Rules whose error count went up, as a readable list."""
        worsened = [
            rule_id for rule_id, count in after.items() if count > before.get(rule_id, 0)
        ]
        return ", ".join(sorted(worsened))

    def _verify(self, report: FixReport) -> None:
        """Check the engine's own output before anyone trusts it."""
        if report.after is None:  # pragma: no cover - always set by fix_source
            return

        if not report.applied and report.changed:
            report.verification_error = (
                "no fix was applied but the file changed anyway"
            )
            return

        worsened = self._regression(
            self._errors_per_rule(report.before), self._errors_per_rule(report.after)
        )
        if worsened:
            report.verification_error = f"fixing introduced new errors in: {worsened}"
            return

        if report.visual is None or not report.applied:
            return

        # Each fix declares how much of the image it may move; the run is held
        # to the most generous one it actually used, unless the caller set a
        # tighter budget explicitly.
        budget = (
            self.visual_budget
            if self.visual_budget is not None
            else max(fix.budget for fix in report.applied)
        )
        if report.visual.within(budget):
            return
        riskiest = max((fix.risk for fix in report.applied), key=lambda r: r.rank)
        if riskiest is Risk.SAFE:
            report.verification_error = (
                f"a safe fix changed the rendered image ({report.visual})"
            )
        else:
            report.verification_error = (
                f"visual change {report.visual} exceeds the budget ({budget:.2%})"
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

            fixers = build_fixers(rule)
            if not fixers:
                report.skipped.append(SkippedFix(rule.id, "no automatic fix available"))
                continue
            if self.only is not None and rule.id not in self.only:
                report.skipped.append(SkippedFix(rule.id, "not selected", fixers[0].risk))
                continue

            # Safest first: a dead gradient is deleted before anyone considers
            # flattening a live one.
            for fixer in fixers:
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
                    doc = parse_svg(current_source, path=path)  # drop partial edits
                    continue

                # Re-parse between fixers: the document model caches resolved
                # styles and geometry, so the next one must not read a stale view.
                candidate_source = serialize(doc)
                candidate = parse_svg(candidate_source, path=path)
                candidate_report = self.checker.check_document(candidate)

                # Each fix stands on its own. Scaling a canvas down, for
                # instance, can push strokes under the minimum width — that fix
                # gets rolled back rather than poisoning the whole run.
                regression = self._regression(
                    self._errors_per_rule(before), self._errors_per_rule(candidate_report)
                )
                if regression:
                    report.skipped.append(
                        SkippedFix(
                            rule.id,
                            f"rolled back: it would introduce errors in {regression}",
                            fixer.risk,
                        )
                    )
                    doc = parse_svg(current_source, path=path)
                    continue

                report.applied.append(
                    AppliedFix(rule.id, fixer.risk, outcome.changes, fixer.visual_budget)
                )
                current_source = candidate_source
                doc = candidate

                if rule.id not in self._failing_rule_ids(candidate_report):
                    # The safe repair was enough. Stopping here keeps the run
                    # from reporting "skipped: needs --allow lossy" about a rule
                    # that is already satisfied, which reads as an unmet need.
                    break

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
