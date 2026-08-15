"""Profiles: one ruleset per shop.

A profile is a YAML file listing rule ids with parameters. Profiles can extend
another profile, override single rules and disable inherited ones, so a new
shop is usually a ten-line file — no Python required.

Lookup order for a profile name:

1. built-in profiles shipped in ``profiles/builtin``
2. ``*.yaml`` in every directory on ``$SVG_EMBROIDERY_PROFILE_PATH``
3. ``*.yaml`` in ``./profiles`` relative to the working directory

A path to a YAML file can also be passed directly instead of a name.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from ..rules import Rule, RuleConfigError, create_rule

BUILTIN_DIR = Path(__file__).parent / "builtin"
ENV_PATH_VAR = "SVG_EMBROIDERY_PROFILE_PATH"


class ProfileError(Exception):
    """Raised when a profile cannot be found, parsed or resolved."""


@dataclass
class RuleSpec:
    """One entry in a profile's ``rules`` list."""

    id: str
    params: Dict[str, Any] = field(default_factory=dict)
    severity: Optional[str] = None

    def build(self) -> Rule:
        try:
            return create_rule(self.id, self.params, self.severity)
        except RuleConfigError as exc:
            raise ProfileError(str(exc)) from exc


@dataclass
class Profile:
    """A named ruleset."""

    name: str
    title: str = ""
    description: str = ""
    url: str = ""
    source: Optional[Path] = None
    rules: List[RuleSpec] = field(default_factory=list)

    def build_rules(self) -> List[Rule]:
        return [spec.build() for spec in self.rules]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "url": self.url,
            "source": str(self.source) if self.source else None,
            "rules": [
                {"id": spec.id, "params": spec.params, "severity": spec.severity}
                for spec in self.rules
            ],
        }


def search_directories() -> List[Path]:
    directories = [BUILTIN_DIR]
    for entry in os.environ.get(ENV_PATH_VAR, "").split(os.pathsep):
        if entry.strip():
            directories.append(Path(entry.strip()))
    directories.append(Path.cwd() / "profiles")
    return [d for d in directories if d.is_dir()]


def _read_yaml(path: Path) -> Dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ProfileError(f"cannot read profile '{path}': {exc}") from exc
    except yaml.YAMLError as exc:
        raise ProfileError(f"invalid YAML in '{path}': {exc}") from exc
    if not isinstance(data, dict):
        raise ProfileError(f"profile '{path}' must contain a YAML mapping")
    return data


def _find_file(name: str) -> Path:
    candidate = Path(name)
    if candidate.suffix in (".yaml", ".yml") and candidate.is_file():
        return candidate
    for directory in search_directories():
        for suffix in (".yaml", ".yml"):
            path = directory / f"{name}{suffix}"
            if path.is_file():
                return path
    known = ", ".join(sorted(p.name for p in list_profiles())) or "(none)"
    raise ProfileError(f"unknown profile '{name}'. Available profiles: {known}")


def _parse_rule_entries(data: Dict[str, Any], source: Path) -> List[RuleSpec]:
    entries = data.get("rules", [])
    if not isinstance(entries, list):
        raise ProfileError(f"profile '{source}': 'rules' must be a list")
    specs: List[RuleSpec] = []
    for entry in entries:
        if isinstance(entry, str):
            specs.append(RuleSpec(id=entry))
            continue
        if not isinstance(entry, dict) or "id" not in entry:
            raise ProfileError(
                f"profile '{source}': every rule entry needs an 'id' (got {entry!r})"
            )
        params = entry.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ProfileError(f"profile '{source}': params of '{entry['id']}' must be a mapping")
        specs.append(
            RuleSpec(id=str(entry["id"]), params=dict(params), severity=entry.get("severity"))
        )
    return specs


def load_profile(name: str, _seen: Optional[List[str]] = None) -> Profile:
    """Load a profile by name or path, resolving ``extends`` chains."""
    seen = list(_seen or [])
    path = _find_file(name)
    key = str(path.resolve())
    if key in seen:
        chain = " -> ".join(seen + [key])
        raise ProfileError(f"circular profile inheritance: {chain}")
    seen.append(key)

    data = _read_yaml(path)
    specs: List[RuleSpec] = []

    parent_name = data.get("extends")
    if parent_name:
        parent = load_profile(str(parent_name), seen)
        specs = [RuleSpec(s.id, dict(s.params), s.severity) for s in parent.rules]

    disabled = {str(item) for item in data.get("disable", []) or []}
    if disabled:
        specs = [spec for spec in specs if spec.id not in disabled]

    for spec in _parse_rule_entries(data, path):
        for index, existing in enumerate(specs):
            if existing.id == spec.id:  # override the inherited configuration
                specs[index] = spec
                break
        else:
            specs.append(spec)

    return Profile(
        name=str(data.get("name") or path.stem),
        title=str(data.get("title", "")),
        description=str(data.get("description", "")),
        url=str(data.get("url", "")),
        source=path,
        rules=specs,
    )


def list_profiles() -> List[Profile]:
    """All discoverable profiles, nearest match per name."""
    found: Dict[str, Profile] = {}
    for directory in search_directories():
        for path in sorted(directory.glob("*.y*ml")):
            try:
                profile = load_profile(str(path))
            except ProfileError:
                continue
            found.setdefault(profile.name, profile)
    return sorted(found.values(), key=lambda p: p.name)
