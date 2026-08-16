"""Automatic fixes: the protocol, the engine, and the fixers themselves.

    from svg_embroidery.fixes import FixEngine, Risk

    engine = FixEngine.from_profile_name("embroidery-basic")
    report = engine.fix_source(open("design.svg").read())
    print(report.summary())
    print(report.diff())

Nothing is written to disk here — the engine returns the repaired text and the
caller decides. Only ``SAFE`` fixes run unless ``allow`` says otherwise.
"""

from .base import (  # noqa: F401
    DEFAULT_ALLOWED,
    Change,
    FixOutcome,
    Fixer,
    FixerError,
    Risk,
    available_fixers,
    build_fixers,
    fixer_classes_for,
    parse_risks,
    register_fixer,
    risks_up_to,
)
from .engine import AppliedFix, FixEngine, FixReport, SkippedFix  # noqa: F401
from .verify import FixVerification, verify_fixer, verify_no_op  # noqa: F401

# Importing the modules registers the fixers.
from . import geometry, lossy, safe  # noqa: F401,E402

__all__ = [
    "AppliedFix",
    "Change",
    "DEFAULT_ALLOWED",
    "FixEngine",
    "FixOutcome",
    "FixReport",
    "FixVerification",
    "Fixer",
    "FixerError",
    "Risk",
    "SkippedFix",
    "available_fixers",
    "build_fixers",
    "fixer_classes_for",
    "parse_risks",
    "register_fixer",
    "risks_up_to",
    "verify_fixer",
    "verify_no_op",
]
