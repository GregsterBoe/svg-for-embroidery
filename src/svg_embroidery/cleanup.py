"""B5: cleaning up what the tracer drew.

There is no new fixing machinery here. Phase A already built one — a protocol,
an engine that verifies its own output, and repairs that report every change —
and B5's instruction is to *reuse* it on the tracer's output. What this module
adds is the one thing the engine cannot decide for itself: **how much a run is
allowed to do to a document nobody drew.**

That is a genuinely different question from repairing a designer's file, and
the answer is not the same. ``svgemb fix`` is careful because there is intent
in the file: a hairline may be a mistake or may be the whole point, and only
the person who drew it knows. A traced document has no such intent in it. It
was generated milliseconds ago from an image, by us, and the artwork of record
is still the image — so deleting a seven-pixel fleck the quantiser invented is
not overriding anybody. It is finishing the job.

So the conversion path runs at the top risk level by default, and three things
keep that honest rather than reckless:

- **the engine still checks its own work.** Every repair is re-checked, held to
  the visual budget it declared, and rolled back on its own if it would
  introduce an error somewhere else. Nothing about running destructively skips
  that;
- **every removal is reported**, which is what makes the difference between a
  cleanup and a tracer quietly dropping speckle behind everyone's back — the
  behaviour B4 found and B5 exists to replace;
- **only one kind of question is answered in advance.** A repair that asks
  (A7) is answered here when *the profile already contains the answer* and the
  question exists to get consent — ``geometry.min_area`` asks "may I delete
  these 29 flecks", and the profile has already said in millimetres that a
  fleck that size is not stitchable. Where the answer is a judgement the
  profile cannot make, it is left open and comes back in the report:
  ``path.max_count`` asks which artwork to sacrifice to a shape count, and
  nothing in a profile ranks one shape above another. That is the one place
  this module could do real damage, so it doesn't.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from .checker import Checker
from .findings import Report
from .fixes import FixEngine, FixReport, Risk, answer_from_mapping
from .fixes.cleanup import DropSpecks
from .profiles import Profile

#: What cleanup may do without being asked. The whole ladder, for the reason
#: given above: there is no design intent in a generated file to protect.
DEFAULT_ALLOW: Sequence[Risk] = (Risk.SAFE, Risk.LOSSY, Risk.DESTRUCTIVE)

#: The questions the conversion path answers for itself, and there is exactly
#: one. Written out as a mapping rather than a rule ("answer anything with a
#: single option") because the reason is specific to the rule and not to the
#: shape of its question: the profile has *already* said, in millimetres, that
#: a shape this small cannot be sewn, so the question is asking for consent
#: rather than for information. Anything else stays open. Adding an entry here
#: is a decision about what the tool may do to someone's artwork unasked, which
#: is why it is a short list in one place.
DEFAULT_ANSWERS: Dict[str, str] = {DropSpecks.rule_id: DropSpecks.OPTION}


@dataclass
class Cleanup:
    """A traced document before and after B5's pass over it."""

    svg: str
    before: Report
    after: Report
    fix: FixReport

    @property
    def passes(self) -> bool:
        return not self.after.errors

    @property
    def changed(self) -> bool:
        return self.fix.changed

    @property
    def ok(self) -> bool:
        """False when the engine caught its own output misbehaving."""
        return self.fix.ok

    def notes(self) -> List[str]:
        """One line per thing worth knowing, in the words the report prints."""
        lines = [f"cleanup: {fix}" for fix in self.fix.applied]
        lines.extend(
            f"cleanup left {decision.rule_id} open: {decision.question}"
            for decision in self.fix.pending
        )
        if not self.ok:
            lines.append(f"cleanup verification failed: {self.fix.verification_error}")
        return lines


def clean(
    source: str,
    profile: Profile,
    allow: Sequence[Risk] = DEFAULT_ALLOW,
    answers: Optional[Dict[str, str]] = None,
) -> Cleanup:
    """Run the profile's repairs over a traced document.

    ``answers`` replies to the repairs that ask (A7), and defaults to
    :data:`DEFAULT_ANSWERS`. Pass ``{}`` to answer nothing at all, which is the
    same run with every question left open and reported.
    """
    replies = DEFAULT_ANSWERS if answers is None else answers
    engine = FixEngine(
        profile,
        allow=allow,
        decide=answer_from_mapping(replies) if replies else None,
    )
    report = engine.fix_source(source)
    # Not `report.after`: if verification failed, the output is not offered for
    # use, so what this document *is* remains what came in.
    svg = report.source_after if report.ok else source
    after = report.after if report.ok and report.after is not None else report.before
    return Cleanup(svg=svg, before=report.before, after=after, fix=report)


def check(source: str, profile: Profile) -> Report:
    """What the profile makes of a document, without repairing anything.

    Here rather than in the caller so that the "does it pass" question is asked
    exactly one way, whether it is asked of a raw trace or a cleaned one.
    """
    return Checker(profile).check_source(source)
