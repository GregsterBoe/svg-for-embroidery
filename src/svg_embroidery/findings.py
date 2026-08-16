"""Findings produced by rules and the report that collects them."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"

    @property
    def rank(self) -> int:
        return {"error": 0, "warning": 1, "info": 2}[self.value]

    @property
    def icon(self) -> str:
        return {"error": "❌", "warning": "⚠️", "info": "✅"}[self.value]


@dataclass
class Finding:
    """One observation about a document, good or bad."""

    rule_id: str
    severity: Severity
    message: str
    hint: Optional[str] = None
    location: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)

    def with_severity(self, severity: Severity) -> "Finding":
        return Finding(self.rule_id, severity, self.message, self.hint, self.location, self.data)

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "rule": self.rule_id,
            "severity": self.severity.value,
            "message": self.message,
        }
        if self.hint:
            payload["hint"] = self.hint
        if self.location:
            payload["location"] = self.location
        if self.data:
            payload["data"] = self.data
        return payload


@dataclass
class Report:
    """All findings for a single file checked against a single profile."""

    file: Optional[Path]
    profile: str
    findings: List[Finding] = field(default_factory=list)

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    def extend(self, findings) -> None:
        self.findings.extend(findings)

    def of_severity(self, severity: Severity) -> List[Finding]:
        return [f for f in self.findings if f.severity is severity]

    @property
    def errors(self) -> List[Finding]:
        return self.of_severity(Severity.ERROR)

    @property
    def warnings(self) -> List[Finding]:
        return self.of_severity(Severity.WARNING)

    @property
    def infos(self) -> List[Finding]:
        return self.of_severity(Severity.INFO)

    @property
    def unmeasured(self) -> List[Finding]:
        """Checks that could not run on this machine.

        A rule with no capability behind it reports ``measured=False`` and stays
        INFO, because a missing library is not a defect in the file — but the
        difference between "passed" and "never ran" matters to whoever reads the
        verdict, so these stay visible even when the other INFOs are folded away.
        """
        return [f for f in self.findings if f.data.get("measured") is False]

    def passed(self, strict: bool = False) -> bool:
        """True when the file may be uploaded. ``strict`` also fails warnings."""
        if self.errors:
            return False
        return not (strict and self.warnings)

    def counts(self) -> Dict[str, int]:
        return {
            "error": len(self.errors),
            "warning": len(self.warnings),
            "info": len(self.infos),
        }

    def to_dict(self, strict: bool = False) -> Dict[str, Any]:
        return {
            "file": str(self.file) if self.file else None,
            "profile": self.profile,
            "passed": self.passed(strict=strict),
            "counts": self.counts(),
            "unmeasured": [f.rule_id for f in self.unmeasured],
            "findings": [f.to_dict() for f in self.findings],
        }
