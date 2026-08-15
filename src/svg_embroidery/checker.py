"""Running a profile against a document."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Optional, Union

from .document import SvgDocument, SvgParseError, load_svg, parse_svg
from .findings import Finding, Report, Severity
from .profiles import Profile, load_profile


class Checker:
    """Runs the rules of one profile against SVG documents."""

    def __init__(self, profile: Profile) -> None:
        self.profile = profile
        self.rules = profile.build_rules()

    @classmethod
    def from_profile_name(cls, name: str) -> "Checker":
        return cls(load_profile(name))

    def check_document(self, doc: SvgDocument) -> Report:
        report = Report(file=doc.path, profile=self.profile.name)
        for rule in self.rules:
            findings: Iterable[Finding] = rule.check(doc) or []
            report.extend(findings)
        report.findings.sort(key=lambda f: f.severity.rank)
        return report

    def check_file(self, path: Union[str, Path]) -> Report:
        file_path = Path(path)
        try:
            doc = load_svg(file_path)
        except SvgParseError as exc:
            report = Report(file=file_path, profile=self.profile.name)
            report.add(
                Finding(
                    rule_id="file.parse",
                    severity=Severity.ERROR,
                    message=f"Could not parse SVG: {exc}",
                    hint="Re-export the file as plain SVG.",
                )
            )
            return report
        return self.check_document(doc)

    def check_source(self, source: str, path: Optional[Path] = None) -> Report:
        return self.check_document(parse_svg(source, path=path))


def check_file(path: Union[str, Path], profile: str = "embroidery-basic") -> Report:
    """Convenience helper: check one file against a named profile."""
    return Checker.from_profile_name(profile).check_file(path)


def check_files(paths: Iterable[Union[str, Path]], profile: str = "embroidery-basic") -> List[Report]:
    checker = Checker.from_profile_name(profile)
    return [checker.check_file(path) for path in paths]
