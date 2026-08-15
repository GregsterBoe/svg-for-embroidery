"""Rule base class and registry.

A rule is a small, self-contained check with declared parameters. Rulesets
(profiles) pick rules by id and give them parameters, so supporting a new shop
usually means writing a YAML profile — not new code.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional, Type

from ..document import SvgDocument
from ..findings import Finding, Severity


class RuleConfigError(Exception):
    """Raised for unknown rule ids or bad rule parameters."""


class Rule:
    """Base class for all checks.

    Subclasses set :attr:`id`, :attr:`summary` and :attr:`params` (the
    parameter defaults) and implement :meth:`check`.
    """

    id: str = ""
    summary: str = ""
    #: Parameter name -> default value. Anything else in a profile is an error.
    params: Dict[str, Any] = {}
    #: Severity used for failures unless the profile overrides it.
    default_severity: Severity = Severity.ERROR

    def __init__(self, severity: Optional[Severity] = None, **params: Any) -> None:
        unknown = sorted(set(params) - set(self.params))
        if unknown:
            allowed = ", ".join(sorted(self.params)) or "(none)"
            raise RuleConfigError(
                f"rule '{self.id}': unknown parameter(s) {', '.join(unknown)}; allowed: {allowed}"
            )
        self.config: Dict[str, Any] = {**self.params, **params}
        self.severity = severity or self.default_severity
        self.validate()

    def validate(self) -> None:
        """Hook for subclasses to sanity check :attr:`config`."""

    # -- helpers for subclasses -------------------------------------------
    def fail(
        self,
        message: str,
        hint: Optional[str] = None,
        location: Optional[str] = None,
        **data: Any,
    ) -> Finding:
        return Finding(self.id, self.severity, message, hint, location, data)

    def warn(self, message: str, hint: Optional[str] = None, location: Optional[str] = None, **data: Any) -> Finding:
        return Finding(self.id, Severity.WARNING, message, hint, location, data)

    def ok(self, message: str, **data: Any) -> Finding:
        return Finding(self.id, Severity.INFO, message, data=data)

    def check(self, doc: SvgDocument) -> Iterable[Finding]:  # pragma: no cover - abstract
        raise NotImplementedError

    def describe(self) -> str:
        if not self.config:
            return self.summary
        settings = ", ".join(f"{k}={v!r}" for k, v in sorted(self.config.items()))
        return f"{self.summary} ({settings})"


_REGISTRY: Dict[str, Type[Rule]] = {}


def register(cls: Type[Rule]) -> Type[Rule]:
    """Class decorator that adds a rule to the global registry."""
    if not cls.id:
        raise RuleConfigError(f"{cls.__name__} must define an id")
    if cls.id in _REGISTRY:
        raise RuleConfigError(f"duplicate rule id '{cls.id}'")
    _REGISTRY[cls.id] = cls
    return cls


def get_rule_class(rule_id: str) -> Type[Rule]:
    try:
        return _REGISTRY[rule_id]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY))
        raise RuleConfigError(f"unknown rule '{rule_id}'. Available rules: {known}") from None


def create_rule(rule_id: str, params: Optional[Dict[str, Any]] = None, severity: Optional[str] = None) -> Rule:
    """Instantiate a registered rule from profile data."""
    cls = get_rule_class(rule_id)
    resolved_severity = None
    if severity is not None:
        try:
            resolved_severity = Severity(severity)
        except ValueError:
            raise RuleConfigError(
                f"rule '{rule_id}': invalid severity '{severity}' (use error, warning or info)"
            ) from None
    return cls(severity=resolved_severity, **(params or {}))


def available_rules() -> List[Type[Rule]]:
    return [_REGISTRY[key] for key in sorted(_REGISTRY)]
