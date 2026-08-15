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

    def __init__(self, rule: Rule) -> None:
        if rule.id != self.rule_id:  # pragma: no cover - engine pairs these up
            raise FixerError(f"{type(self).__name__} cannot fix '{rule.id}'")
        self.rule = rule
        #: The profile's parameters for this rule — repair to the same target.
        self.config: Dict[str, Any] = rule.config

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


def build_fixers(rule: Rule) -> List[Fixer]:
    """The configured fixers for a rule, safest first."""
    return [cls(rule) for cls in fixer_classes_for(rule.id)]


def available_fixers() -> List[Type[Fixer]]:
    return [cls for key in sorted(_FIXERS) for cls in _FIXERS[key]]


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
