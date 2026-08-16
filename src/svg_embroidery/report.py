"""Rendering reports for humans and for machines."""

from __future__ import annotations

import json
import textwrap
from typing import TYPE_CHECKING, Iterable, List

from .findings import Report, Severity

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from .fixes.engine import FixReport

LINE = "─" * 62

#: Marks a check that could not run here. Not a verdict, so not ✅ or ❌.
UNMEASURED_ICON = "ℹ️"

#: Risk names by rank, for turning a decision's cheapest answer into a flag.
_RISK_BY_RANK = {0: "safe", 1: "lossy", 2: "destructive"}


def render_text(report: Report, strict: bool = False, verbose: bool = False, color: bool = True) -> str:
    """Human readable report. ``verbose`` also prints the passing checks."""
    lines: List[str] = []
    name = str(report.file) if report.file else "<stdin>"
    lines.append(f"📄 {name}   [profile: {report.profile}]")
    lines.append(LINE)

    unmeasured = {id(finding) for finding in report.unmeasured}
    # A check that never ran is shown whatever the verbosity: "PASS" with a
    # silently skipped rule behind it is the one report nobody can act on.
    shown = [
        finding
        for finding in report.findings
        if verbose or finding.severity is not Severity.INFO or id(finding) in unmeasured
    ]
    if not shown:
        lines.append("No issues found.")
    for finding in shown:
        icon = UNMEASURED_ICON if id(finding) in unmeasured else finding.severity.icon
        lines.append(f"{icon} {finding.message}  [{finding.rule_id}]")
        if finding.location and finding.severity is not Severity.INFO:
            lines.append(f"    at {finding.location}")
        if finding.hint and finding.severity is not Severity.INFO:
            lines.append(f"    → {finding.hint}")

    counts = report.counts()
    lines.append(LINE)
    verdict = "PASS" if report.passed(strict=strict) else "FAIL"
    icon = "✅" if verdict == "PASS" else "❌"
    tail = f", {len(unmeasured)} check(s) not measured" if unmeasured else ""
    lines.append(
        f"{icon} {verdict}: {counts['error']} error(s), {counts['warning']} warning(s), "
        f"{counts['info'] - len(unmeasured)} check(s) passed{tail}."
    )
    text = "\n".join(lines)
    return text if color else _strip_icons(text)


def _strip_icons(text: str) -> str:
    for icon, replacement in (
        ("✅", "[ok]"),
        ("❌", "[error]"),
        ("⚠️", "[warn]"),
        ("ℹ️", "[note]"),
        ("⏭", "[skip]"),
        ("❓", "[ask]"),
        ("📄", "File:"),
        ("→", "->"),
    ):
        text = text.replace(icon, replacement)
    return text


def render_json(reports: Iterable[Report], strict: bool = False) -> str:
    payload = [report.to_dict(strict=strict) for report in reports]
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _verdict(report: Report, strict: bool) -> str:
    return "✅ PASS" if report.passed(strict=strict) else "❌ FAIL"


def _wrap(text: str, indent: str, width: int = 78) -> List[str]:
    return textwrap.wrap(text, width=width, initial_indent=indent, subsequent_indent=indent)


def _render_decision(decision) -> List[str]:
    """An open question, with the command that answers it.

    Printed rather than prompted: a run that stops for input cannot go in a
    script, and the answer is worth recording in one anyway.
    """
    lines = [f"❓ your call  {decision.rule_id}"]
    if decision.context:
        lines.extend(_wrap(decision.context, "       "))
    lines.append(f"       {decision.question}")
    for option in decision.options:
        mark = "*" if option.recommended else " "
        lines.append(f"       {mark} {option.key:<10} {option.label}  [{option.risk.value}]")
        if option.detail:
            lines.extend(_wrap(option.detail, " " * 20))
    if not decision.options:  # pragma: no cover - a question always has answers
        return lines
    keys = "|".join(option.key for option in decision.options)
    # The cheapest answer sets the flag you need; anything dearer says so on its
    # own line above. Printed as one command so it can be pasted straight back.
    cheapest = min(option.risk.rank for option in decision.options)
    allow = "" if cheapest == 0 else f"--allow {_RISK_BY_RANK[cheapest]} "
    lines.append(f"       answer with: {allow}--choose {decision.rule_id}={keys}")
    return lines


def render_fix_text(
    report: "FixReport",
    strict: bool = False,
    color: bool = True,
    diff: bool = False,
) -> str:
    """What one fix run did, why it stopped where it did, and the new verdict.

    Skips are printed as prominently as fixes and in the same list, because for
    the person holding a file the shop rejected they are the more useful half:
    each one is either a decision only they can make or a package they can
    install.
    """
    lines: List[str] = []
    name = str(report.file) if report.file else "<stdin>"
    lines.append(f"📄 {name}   [profile: {report.profile}]")
    lines.append(LINE)

    if report.verification_error:
        lines.append(f"❌ verification FAILED: {report.verification_error}")
        lines.append("   Nothing was written. This is a bug in svgemb, not in your file.")
        lines.append(LINE)

    for fix in report.applied:
        lines.append(f"✅ fixed    {fix.rule_id}  [{fix.risk.value}]")
        for change in fix.changes:
            location = f"  ({change.location})" if change.location else ""
            lines.append(f"       {change.description}{location}")
    for skip in report.skipped:
        risk = f"  [{skip.risk.value}]" if skip.risk else ""
        lines.append(f"⏭ skipped  {skip.rule_id}{risk}")
        lines.append(f"       {skip.reason}")
    for finding in report.unmeasured:
        lines.append(f"{UNMEASURED_ICON} not measured  {finding.rule_id}")
        lines.append(f"       {finding.message}")
    for decision in report.pending:
        lines.extend(_render_decision(decision))
    if not (report.applied or report.skipped or report.unmeasured or report.pending):
        lines.append("Nothing to fix.")

    lines.append(LINE)
    before, after = report.before, report.after
    if after is None:  # pragma: no cover - fix_source always re-checks
        return "\n".join(lines)
    lines.append(
        f"{_verdict(before, strict)} → {_verdict(after, strict)}   "
        f"{len(before.errors)} → {len(after.errors)} error(s), "
        f"{len(before.warnings)} → {len(after.warnings)} warning(s)"
    )
    if report.visual is not None:
        lines.append(f"   visual: {report.visual}")
    if report.remaining_rules:
        lines.append(f"   still failing: {', '.join(report.remaining_rules)}")

    if diff and report.changed:
        lines.append(LINE)
        lines.append(report.diff().rstrip("\n"))

    text = "\n".join(lines)
    return text if color else _strip_icons(text)


def render_fix_json(
    reports: Iterable["FixReport"], strict: bool = False, diff: bool = False
) -> str:
    payload = [report.to_dict(strict=strict, include_diff=diff) for report in reports]
    return json.dumps(payload, indent=2, ensure_ascii=False)


def render_fix_summary(
    reports: List["FixReport"], strict: bool = False, color: bool = True
) -> str:
    """One line per file, for a run over a directory."""
    lines = []
    for report in reports:
        after = report.after or report.before
        ok = after.passed(strict=strict)
        lines.append(
            f"{'✅' if ok else '❌'} {'PASS' if ok else 'FAIL':4}  {report.file}  "
            f"({len(report.applied)} fix(es), {len(report.skipped)} skipped)"
        )
    lines.append(LINE)
    passing = sum(
        1 for report in reports if (report.after or report.before).passed(strict=strict)
    )
    lines.append(f"{passing}/{len(reports)} file(s) pass after fixing.")
    text = "\n".join(lines)
    return text if color else _strip_icons(text)


def render_summary(reports: List[Report], strict: bool = False, color: bool = True) -> str:
    """One-line-per-file summary, used when checking several files."""
    lines = []
    failed = 0
    for report in reports:
        ok = report.passed(strict=strict)
        failed += 0 if ok else 1
        counts = report.counts()
        status = "PASS" if ok else "FAIL"
        lines.append(
            f"{'✅' if ok else '❌'} {status:4}  {report.file}  "
            f"({counts['error']} error(s), {counts['warning']} warning(s))"
        )
    lines.append(LINE)
    lines.append(f"{len(reports) - failed}/{len(reports)} file(s) passed.")
    text = "\n".join(lines)
    return text if color else _strip_icons(text)
