"""Rendering reports for humans and for machines."""

from __future__ import annotations

import json
from typing import Iterable, List

from .findings import Report, Severity

LINE = "─" * 62


def render_text(report: Report, strict: bool = False, verbose: bool = False, color: bool = True) -> str:
    """Human readable report. ``verbose`` also prints the passing checks."""
    lines: List[str] = []
    name = str(report.file) if report.file else "<stdin>"
    lines.append(f"📄 {name}   [profile: {report.profile}]")
    lines.append(LINE)

    shown = [
        finding
        for finding in report.findings
        if verbose or finding.severity is not Severity.INFO
    ]
    if not shown:
        lines.append("No issues found.")
    for finding in shown:
        lines.append(f"{finding.severity.icon} {finding.message}  [{finding.rule_id}]")
        if finding.location and finding.severity is not Severity.INFO:
            lines.append(f"    at {finding.location}")
        if finding.hint and finding.severity is not Severity.INFO:
            lines.append(f"    → {finding.hint}")

    counts = report.counts()
    lines.append(LINE)
    verdict = "PASS" if report.passed(strict=strict) else "FAIL"
    icon = "✅" if verdict == "PASS" else "❌"
    lines.append(
        f"{icon} {verdict}: {counts['error']} error(s), {counts['warning']} warning(s), "
        f"{counts['info']} check(s) passed."
    )
    text = "\n".join(lines)
    return text if color else _strip_icons(text)


def _strip_icons(text: str) -> str:
    for icon, replacement in (("✅", "[ok]"), ("❌", "[error]"), ("⚠️", "[warn]"), ("📄", "File:"), ("→", "->")):
        text = text.replace(icon, replacement)
    return text


def render_json(reports: Iterable[Report], strict: bool = False) -> str:
    payload = [report.to_dict(strict=strict) for report in reports]
    return json.dumps(payload, indent=2, ensure_ascii=False)


def render_summary(reports: List[Report], strict: bool = False) -> str:
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
    return "\n".join(lines)
