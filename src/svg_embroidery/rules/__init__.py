"""Rule registry.

Importing this package registers every built-in rule. Third-party rules can be
added by importing this module and using the :func:`register` decorator on a
:class:`Rule` subclass.
"""

from .base import (  # noqa: F401
    Rule,
    RuleConfigError,
    available_rules,
    create_rule,
    get_rule_class,
    register,
)

# Importing the modules populates the registry.
from . import color_rules, geometry, path_rules, stroke_rules, structure  # noqa: F401,E402

__all__ = [
    "Rule",
    "RuleConfigError",
    "available_rules",
    "create_rule",
    "get_rule_class",
    "register",
]
