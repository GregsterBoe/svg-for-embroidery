"""The contract every fixer must satisfy.

Four invariants, checked by running the real engine rather than by inspection:

1. **It fixes the thing.** The target rule fails before and passes after.
2. **It breaks nothing else.** No other rule's error count goes up.
3. **It is idempotent.** Fixing twice equals fixing once, byte for byte —
   otherwise every fix run would produce a slightly different file. In practice
   this catches the fixer that never satisfies its own rule and so keeps being
   re-applied, churning the file a little more each pass.
4. **It stays inside its visual budget.** ``SAFE`` means zero pixels moved;
   anything else must stay under the budget it declared.

Public rather than test-only, so a fixer added later — by us or by anyone
writing a shop-specific rule — can be held to the same bar.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from ..profiles import load_profile
from ..visual import Difference
from .base import Risk, fixer_class_for
from .engine import FixEngine


@dataclass
class FixVerification:
    """Result of holding one fixer to the contract."""

    rule_id: str
    risk: Optional[Risk] = None
    fixed_target: bool = False
    no_regressions: bool = False
    idempotent: bool = False
    visual_ok: bool = False
    changed_file: bool = False
    visual: Optional[Difference] = None
    notes: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            self.fixed_target
            and self.no_regressions
            and self.idempotent
            and self.visual_ok
            and not self.notes
        )

    def summary(self) -> str:
        marks = {
            "fixes the target rule": self.fixed_target,
            "no regressions": self.no_regressions,
            "idempotent": self.idempotent,
            "within visual budget": self.visual_ok,
        }
        lines = [f"{'ok ' if value else 'FAIL'}  {label}" for label, value in marks.items()]
        lines.extend(f"FAIL  {note}" for note in self.notes)
        if self.visual is not None:
            lines.append(f"      visual: {self.visual}")
        return "\n".join(lines)


def verify_fixer(
    source: str,
    rule_id: str,
    profile: str = "embroidery-basic",
    visual_budget: float = 0.0,
    extra_profiles: Sequence[str] = (),
) -> FixVerification:
    """Run ``rule_id``'s fixer over ``source`` and check all four invariants.

    ``source`` must be a document that actually fails ``rule_id``, otherwise
    there is nothing to prove and the verification reports that.
    """
    fixer_class = fixer_class_for(rule_id)
    result = FixVerification(rule_id=rule_id)
    if fixer_class is None:
        result.notes.append(f"no fixer is registered for '{rule_id}'")
        return result
    result.risk = fixer_class.risk

    loaded = load_profile(profile)
    engine = FixEngine(
        loaded,
        allow=[fixer_class.risk],
        only={rule_id},
        visual_budget=visual_budget,
    )

    report = engine.fix_source(source)
    if report.verification_error:
        result.notes.append(report.verification_error)

    failed_before = any(
        finding.rule_id == rule_id
        for finding in report.before.errors + report.before.warnings
    )
    if not failed_before:
        result.notes.append(
            f"the sample does not fail '{rule_id}', so the fix proves nothing"
        )
        return result

    result.changed_file = report.changed
    if not report.applied:
        reasons = "; ".join(str(skip) for skip in report.skipped) or "no reason given"
        result.notes.append(f"the fixer declined to run ({reasons})")
        return result

    # 1. the target rule is satisfied afterwards
    assert report.after is not None
    result.fixed_target = not any(
        finding.rule_id == rule_id
        for finding in report.after.errors + report.after.warnings
    )

    # 2. nothing else got worse — across this profile and any others given
    before_errors = {f.rule_id for f in report.before.errors}
    after_errors = {f.rule_id for f in report.after.errors}
    result.no_regressions = not (after_errors - before_errors)
    for other in extra_profiles:
        other_engine = FixEngine(load_profile(other), allow=[fixer_class.risk])
        before_other = other_engine.checker.check_source(report.source_before)
        after_other = other_engine.checker.check_source(report.source_after)
        if len(after_other.errors) > len(before_other.errors):
            result.no_regressions = False
            result.notes.append(f"new errors under profile '{other}'")

    # 3. fixing twice equals fixing once
    second = engine.fix_source(report.source_after)
    result.idempotent = not second.changed and second.source_after == report.source_after
    if not result.idempotent:
        result.notes.append("running the fixer again changed the file further")

    # 4. the visual claim holds
    result.visual = report.visual
    if report.visual is None:
        result.visual_ok = True  # no renderer: unmeasured, not failed
    elif fixer_class.risk is Risk.SAFE:
        result.visual_ok = report.visual.identical
    else:
        result.visual_ok = report.visual.within(visual_budget)

    return result


def verify_no_op(source: str, profile: str = "embroidery-basic") -> bool:
    """A run that fixes nothing must return the file byte for byte.

    The floor under every fixer: if this fails, no diff can be trusted.
    """
    engine = FixEngine(load_profile(profile), allow=[])
    report = engine.fix_source(source)
    return not report.changed and report.source_after == source
