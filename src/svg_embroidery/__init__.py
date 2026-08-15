"""svg-for-embroidery — modular rule checking for embroidery/print shop SVGs.

Typical use::

    from svg_embroidery import check_file, render_text

    report = check_file("design.svg", profile="embroidery-basic")
    print(render_text(report, verbose=True))
    print(report.passed())
"""

from .checker import Checker, check_file, check_files
from .document import SvgDocument, SvgParseError, load_svg, parse_svg
from .findings import Finding, Report, Severity
from .profiles import Profile, ProfileError, list_profiles, load_profile
from .report import render_json, render_summary, render_text
from .rules import Rule, RuleConfigError, available_rules, register

__version__ = "0.1.0"

__all__ = [
    "Checker",
    "Finding",
    "Profile",
    "ProfileError",
    "Report",
    "Rule",
    "RuleConfigError",
    "Severity",
    "SvgDocument",
    "SvgParseError",
    "available_rules",
    "check_file",
    "check_files",
    "list_profiles",
    "load_profile",
    "load_svg",
    "parse_svg",
    "register",
    "render_json",
    "render_summary",
    "render_text",
    "__version__",
]
