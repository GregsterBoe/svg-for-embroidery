"""A2: the fix protocol.

Rules stay pure — they look at a document and report. A *fixer* is a separate
object registered against a rule id, so a rule that cannot be repaired simply
has none, and adding a fix never risks changing what a check means.

A fixer is built from the **configured rule instance**, not from the rule class.
That is deliberate: the profile decides that this shop wants 2 colours and 2 mm
strokes, and the fixer repairs to those same numbers. One source of truth.

Every fix declares a risk level, because "just fix it" means different things:

``SAFE``
    Cannot change the rendered image. Adding a ``viewBox``, normalising colour
    notation, dropping unused defs. Applied by default.
``LOSSY``
    Changes the image on purpose — quantising five colours to three, flattening
    transparency. The user has to ask for it.
``DESTRUCTIVE``
    May change what the designer meant: regrouping layers alters z-order,
    closing a wide gap invents geometry. Asked for explicitly, per rule.

The engine checks these claims rather than trusting them: a ``SAFE`` fix that
moves a pixel is reported as a bug, not written out.

A fixer may also *ask*. Some rules fail for reasons no tool can settle on its
own — which flat colour replaces a gradient, whether the overlap order in a
drawing matters — and reporting "no automatic fix available" turns a question
into a dead end. Such a fixer returns a :class:`Decision` from
:meth:`Fixer.decision`; the engine offers only the answers the risk budget
allows, and applies the one it is given. Unanswered, the question is reported
with the exact flag that answers it, so nothing ever blocks on a prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Type

from ..document import SvgDocument
from ..rules import Rule


class Risk(str, Enum):
    SAFE = "safe"
    LOSSY = "lossy"
    DESTRUCTIVE = "destructive"

    @property
    def rank(self) -> int:
        return {"safe": 0, "lossy": 1, "destructive": 2}[self.value]


#: What a plain ``svgemb fix`` is allowed to do.
DEFAULT_ALLOWED = frozenset({Risk.SAFE})


class FixerError(Exception):
    """Raised for a broken fixer registration."""


@dataclass(frozen=True)
class Change:
    """One edit, described the way it should read in a report."""

    description: str
    location: Optional[str] = None

    def __str__(self) -> str:
        return f"{self.description} ({self.location})" if self.location else self.description


@dataclass(frozen=True)
class Option:
    """One answer to a fixer's question.

    The **option** carries the risk, not the fixer: "one layer per run of
    colour" cannot move a pixel while "one layer per colour" reorders the
    artwork, and they are two answers to the same question. A fixer whose
    options differ in risk is normal, and the engine gates on the one chosen.
    """

    key: str
    label: str
    #: What it does and what it costs, in the words the report will print.
    detail: str = ""
    risk: Risk = Risk.SAFE
    #: Shown first and taken as the answer when someone just presses Enter.
    recommended: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "detail": self.detail,
            "risk": self.risk.value,
            "recommended": self.recommended,
        }


@dataclass(frozen=True)
class Decision:
    """A question a repair cannot answer on its own.

    Some rules fail for reasons no tool may settle: the shop wants one colour
    per layer and the artwork has none, so *something* has to be regrouped, and
    only the person who drew it knows whether the overlap order matters. Rather
    than reporting "no automatic fix available" and stopping, the fixer states
    the question and the answers it can carry out.
    """

    rule_id: str
    question: str
    options: List[Option] = field(default_factory=list)
    #: Why the question exists at all — the finding, in one line.
    context: str = ""

    def option(self, key: str) -> Optional[Option]:
        for option in self.options:
            if option.key == key:
                return option
        return None

    @property
    def default(self) -> Optional[Option]:
        for option in self.options:
            if option.recommended:
                return option
        return self.options[0] if len(self.options) == 1 else None

    def within(self, allowed) -> "Decision":
        """The same question with only the answers ``allowed`` permits."""
        return Decision(
            rule_id=self.rule_id,
            question=self.question,
            options=[option for option in self.options if option.risk in allowed],
            context=self.context,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule": self.rule_id,
            "question": self.question,
            "context": self.context,
            "options": [option.to_dict() for option in self.options],
        }


@dataclass
class FixOutcome:
    """What a fixer did. No changes means it declined — never an error."""

    changes: List[Change] = field(default_factory=list)
    #: Why nothing was changed, when the fixer had to decline.
    declined: str = ""

    @property
    def applied(self) -> bool:
        return bool(self.changes)

    def add(self, description: str, location: Optional[str] = None) -> None:
        self.changes.append(Change(description, location))

    def decline(self, reason: str) -> "FixOutcome":
        self.declined = reason
        return self


class Fixer:
    """Base class for repairs.

    Subclasses set :attr:`rule_id` and :attr:`risk`, then implement
    :meth:`apply`, mutating the document's XML tree in place.
    """

    #: The rule this repairs. A rule may have several fixers at different
    #: risk levels — deleting a dead gradient is safe, replacing a live one
    #: with flat colour is not.
    rule_id: str = ""
    risk: Risk = Risk.SAFE
    summary: str = ""
    #: Fraction of the image this fix may change (0 = must be pixel-identical).
    #: A lossy fix declares how much it is allowed to move; the engine holds it
    #: to that, so "lossy" never means "anything goes".
    visual_budget: float = 0.0

    def __init__(self, rule: Rule, choice: Optional[str] = None) -> None:
        if rule.id != self.rule_id:  # pragma: no cover - engine pairs these up
            raise FixerError(f"{type(self).__name__} cannot fix '{rule.id}'")
        self.rule = rule
        #: The profile's parameters for this rule — repair to the same target.
        self.config: Dict[str, Any] = rule.config
        #: The answer to :meth:`decision`, when this repair asked one.
        self.choice = choice

    def decision(self, doc: SvgDocument) -> Optional[Decision]:
        """The question this repair needs answered, or ``None`` if it has none.

        Asked against the document, so a fixer can price its own options
        honestly — regrouping is free on artwork that is already sorted by
        colour and destructive on artwork that interleaves it.
        """
        return None

    def apply(self, doc: SvgDocument) -> FixOutcome:  # pragma: no cover - abstract
        raise NotImplementedError

    def describe(self) -> str:
        return f"{self.rule_id} [{self.risk.value}] {self.summary}"


_FIXERS: Dict[str, List[Type[Fixer]]] = {}


def register_fixer(cls: Type[Fixer]) -> Type[Fixer]:
    """Class decorator adding a fixer to the registry."""
    if not cls.rule_id:
        raise FixerError(f"{cls.__name__} must set rule_id")
    existing = _FIXERS.setdefault(cls.rule_id, [])
    if any(other.risk is cls.risk for other in existing):
        raise FixerError(
            f"a {cls.risk.value} fixer for '{cls.rule_id}' is already registered"
        )
    existing.append(cls)
    # Safest first: try the repair that cannot hurt before the one that can.
    existing.sort(key=lambda fixer: fixer.risk.rank)
    return cls


def fixer_classes_for(rule_id: str) -> List[Type[Fixer]]:
    """Fixers for a rule, safest first."""
    return list(_FIXERS.get(rule_id, ()))


def build_fixers(rule: Rule, choice: Optional[str] = None) -> List[Fixer]:
    """The configured fixers for a rule, safest first."""
    return [cls(rule, choice) for cls in fixer_classes_for(rule.id)]


def available_fixers() -> List[Type[Fixer]]:
    return [cls for key in sorted(_FIXERS) for cls in _FIXERS[key]]


def answer_from_mapping(answers: Dict[str, str]):
    """A decider that looks answers up by rule id.

    What ``--choose rule.id=key`` and the web UI's buttons both reduce to: the
    question was already answered somewhere else, so the run stays
    non-interactive and repeatable.
    """

    def decide(decision: Decision) -> Optional[str]:
        key = answers.get(decision.rule_id)
        if key is None:
            return None
        if key in ("", "default", "yes"):
            # "just do the sensible thing" — only honoured when there is one.
            return decision.default.key if decision.default else None
        return key

    return decide


def risks_up_to(level) -> "frozenset[Risk]":
    """Every risk at or below ``level`` — what ``--allow lossy`` means.

    Allowing a level implies the safer ones: nobody asking for lossy repairs
    wants the safe ones withheld. Picking one level rather than a set is also
    what makes the flag readable as a *ceiling*, which is how people reason
    about it.
    """
    if not isinstance(level, Risk):
        try:
            level = Risk(str(level).strip().lower())
        except ValueError:
            allowed = ", ".join(risk.value for risk in Risk)
            raise FixerError(f"unknown risk level '{level}' (use: {allowed})") from None
    return frozenset(risk for risk in Risk if risk.rank <= level.rank)


def parse_risks(values) -> "frozenset[Risk]":
    """Turn ``["safe", "lossy"]`` into a risk set, raising on nonsense."""
    risks = set()
    for value in values:
        if isinstance(value, Risk):
            risks.add(value)
            continue
        try:
            risks.add(Risk(str(value).strip().lower()))
        except ValueError:
            allowed = ", ".join(risk.value for risk in Risk)
            raise FixerError(f"unknown risk level '{value}' (use: {allowed})") from None
    return frozenset(risks)
