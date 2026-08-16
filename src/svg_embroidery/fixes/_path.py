"""Where a subpath starts and ends, for the fixers that edit ``d`` as text.

Closing a contour needs one number: how far the pen finished from where it
started. Tracking the current point is enough for that — no curve is ever
flattened here, because the edit is a ``Z`` appended to the original text and
every byte the designer wrote stays where it was.

Commands are tokenised by :mod:`svg_embroidery.geometry`, which needs the same
walk to flatten curves properly. ``ARGUMENT_COUNT`` and :func:`iter_commands`
are re-exported so this module still reads as one piece.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Sequence, Tuple

from ..geometry import ARGUMENT_COUNT, iter_commands  # noqa: F401

Point = Tuple[float, float]


@dataclass
class Subpath:
    """Where one subpath begins and ends."""

    index: int
    start: Point
    end: Point
    closed: bool

    @property
    def gap(self) -> float:
        """Distance between the last point and the first, in user units."""
        return math.dist(self.start, self.end)


def subpaths(d: str) -> List[Subpath]:
    """Describe every subpath in a ``d`` attribute, in order."""
    found: List[Subpath] = []
    current: Point = (0.0, 0.0)
    start: Point = (0.0, 0.0)
    index = -1
    open_run = False

    def close_current(closed: bool) -> None:
        if open_run:
            found.append(Subpath(index=index, start=start, end=current, closed=closed))

    for command, args in iter_commands(d):
        upper = command.upper()
        relative = command.islower()

        if upper == "M":
            close_current(False)
            index += 1
            open_run = True
            current = (
                (current[0] + args[0], current[1] + args[1]) if relative else (args[0], args[1])
            )
            start = current
        elif upper == "Z":
            close_current(True)
            open_run = False
            current = start
        elif upper == "H":
            current = (current[0] + args[0] if relative else args[0], current[1])
        elif upper == "V":
            current = (current[0], current[1] + args[0] if relative else args[0])
        else:
            x, y = args[-2], args[-1]
            current = (current[0] + x, current[1] + y) if relative else (x, y)

    close_current(False)
    return found


def close_subpath_text(text: str) -> str:
    """Append a close command, matching the case the subpath already uses."""
    stripped = text.rstrip()
    lowercase = sum(1 for char in stripped if char.islower())
    uppercase = sum(1 for char in stripped if char.isupper())
    return f"{stripped} {'z' if lowercase > uppercase else 'Z'}"


def rebuild(chunks: Sequence[str]) -> str:
    return " ".join(chunk.strip() for chunk in chunks if chunk.strip())
