"""What this machine can do, and how to give it more.

The project is one codebase with optional capabilities, not a mobile edition and
a desktop edition. Checking and fixing SVGs needs nothing but the standard
library, so it runs anywhere Python does. Heavier work — rendering, raster
conversion, path geometry — is detected at runtime and degrades explicitly:
a capability you don't have is a measurement you don't get, never a crash.

``svgemb doctor`` prints this, so the answer to "can my phone do X?" is a
command rather than a guess.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence


def is_termux() -> bool:
    """True when running under Termux on Android."""
    return "com.termux" in os.environ.get("PREFIX", "") or "ANDROID_ROOT" in os.environ


def platform_key() -> str:
    if is_termux():
        return "termux"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    return "linux"


@dataclass(frozen=True)
class Requirement:
    """One executable or importable module a capability needs."""

    label: str
    kind: str  # "command" or "module"
    target: str
    #: Distribution name, when it differs from the import name (PIL -> Pillow).
    distribution: str = ""

    def locate(self) -> Optional[str]:
        """Version string when present, ``None`` when missing."""
        if self.kind == "command":
            path = shutil.which(self.target)
            if path is None:
                return None
            return self._command_version(path)
        try:
            __import__(self.target)
        except ImportError:
            return None
        return self._module_version()

    def _command_version(self, path: str) -> str:
        for flag in ("--version", "-version", "-V"):
            try:
                result = subprocess.run(
                    [path, flag], capture_output=True, timeout=10, check=False
                )
            except (OSError, subprocess.SubprocessError):
                break
            output = (result.stdout or b"").decode("utf-8", "replace").strip()
            if not output:
                output = (result.stderr or b"").decode("utf-8", "replace").strip()
            if output:
                return output.splitlines()[0][:60]
        return "installed"

    def _module_version(self) -> str:
        try:
            from importlib.metadata import version

            return version(self.distribution or self.target)
        except Exception:  # noqa: BLE001 - version metadata is best effort
            return "installed"


@dataclass
class Capability:
    """A group of features that share a prerequisite."""

    key: str
    title: str
    enables: str
    requirements: Sequence[Requirement] = ()
    #: "any" — one requirement is enough; "all" — every one is needed.
    mode: str = "any"
    #: Per-platform install instructions.
    hints: Dict[str, str] = field(default_factory=dict)
    #: Roadmap step that introduces the need for it.
    step: str = ""
    #: Optional override returning the labels that are usable right now. Lets a
    #: capability defer to the module that actually uses it, so `doctor` can
    #: never disagree with the code.
    probe: Optional[Callable[[], Sequence[str]]] = None

    def status(self) -> "CapabilityStatus":
        found: Dict[str, str] = {}
        missing: List[str] = []
        usable = None if self.probe is None else set(self.probe())
        for requirement in self.requirements:
            if usable is not None and requirement.label not in usable:
                missing.append(requirement.label)
                continue
            version = requirement.locate()
            if version is None:
                missing.append(requirement.label)
            else:
                found[requirement.label] = version

        if not self.requirements:
            available = True
        elif self.mode == "all":
            available = not missing
        else:
            available = bool(found)
        return CapabilityStatus(capability=self, available=available, found=found, missing=missing)


@dataclass
class CapabilityStatus:
    capability: Capability
    available: bool
    found: Dict[str, str]
    missing: List[str]

    @property
    def hint(self) -> str:
        return self.capability.hints.get(platform_key(), self.capability.hints.get("linux", ""))

    def to_dict(self) -> Dict[str, object]:
        return {
            "key": self.capability.key,
            "title": self.capability.title,
            "enables": self.capability.enables,
            "available": self.available,
            "found": self.found,
            "missing": self.missing,
            "hint": None if self.available else self.hint,
            "step": self.capability.step,
        }


def _installed_renderers():
    """Ask the visual module directly, so the two can never drift apart."""
    from .visual import available_renderers

    return available_renderers()


def _installed_geometry():
    """Likewise for path geometry — one answer, from the code that uses it."""
    from .geometry import available_backends

    return [backend.name for backend in available_backends()]


CAPABILITIES: Sequence[Capability] = (
    Capability(
        key="core",
        title="Core",
        enables="check, round-trip, write, profiles, web UI",
        requirements=(),
        step="shipped",
    ),
    Capability(
        key="rendering",
        title="Rendering",
        enables="visual regression, before/after previews, fix verification",
        requirements=(
            Requirement("rsvg-convert", "command", "rsvg-convert"),
            Requirement("resvg", "command", "resvg"),
            Requirement("inkscape", "command", "inkscape"),
            Requirement("cairosvg", "module", "cairosvg"),
        ),
        mode="any",
        probe=lambda: [renderer.name for renderer in _installed_renderers()],
        hints={
            "termux": "pkg install librsvg",
            "linux": "apt install librsvg2-bin   (or: pip install cairosvg)",
            "macos": "brew install librsvg",
            "windows": "pip install cairosvg",
        },
        step="A1",
    ),
    Capability(
        key="geometry",
        title="Path geometry",
        enables="stroke → filled outline, minimum feature size, boolean ops",
        # shapely only. A5 picked one stack rather than abstracting over two:
        # the work is not just offsetting — the thinness test needs areas and
        # validity repair as well, and pyclipper offsets integer coordinates,
        # which would mean choosing a scale factor and living with it.
        requirements=(Requirement("shapely", "module", "shapely"),),
        mode="any",
        probe=_installed_geometry,
        hints={
            "termux": 'pkg install geos && pip install shapely   (may need to build; if it '
            "fails, run the heavy steps on a desktop and use svgemb serve)",
            "linux": 'pip install "svg-for-embroidery[geometry]"',
            "macos": 'pip install "svg-for-embroidery[geometry]"',
            "windows": 'pip install "svg-for-embroidery[geometry]"',
        },
        step="A5",
    ),
    Capability(
        key="raster",
        title="Raster conversion",
        enables="turning PNG/JPG images into embroidery-ready SVG",
        requirements=(
            Requirement("Pillow", "module", "PIL", distribution="Pillow"),
            Requirement("potrace", "command", "potrace"),
        ),
        mode="all",
        hints={
            "termux": "pkg install python-pillow potrace",
            "linux": 'apt install potrace && pip install "svg-for-embroidery[convert]"',
            "macos": 'brew install potrace && pip install "svg-for-embroidery[convert]"',
            "windows": 'pip install "svg-for-embroidery[convert]" and install potrace',
        },
        step="B0-B4",
    ),
)


def report() -> List[CapabilityStatus]:
    """Status of every capability on this machine."""
    return [capability.status() for capability in CAPABILITIES]


def describe_platform() -> str:
    label = platform_key()
    if label == "termux":
        return f"Termux on Android ({platform.machine()})"
    return f"{platform.system()} {platform.machine()}"
