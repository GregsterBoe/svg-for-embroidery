"""Just enough path parsing to find where a subpath starts and ends.

Closing a contour needs one number: how far the pen finished from where it
started. That means walking the commands and tracking the current point — not
flattening curves, not computing areas. Full geometry arrives with the geometry
layer in step A5.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterator, List, Sequence, Tuple

Point = Tuple[float, float]

#: How many numbers each command takes.
ARGUMENT_COUNT = {
    "M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7, "Z": 0,
}

_TOKEN_RE = re.compile(
    r"([MmZzLlHhVvCcSsQqTtAa])|([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)"
)


def iter_commands(d: str) -> Iterator[Tuple[str, List[float]]]:
    """Yield ``(command, args)`` pairs, expanding implicit repeats.

    ``M 0 0 1 1 2 2`` means a move followed by two line-tos, which is why the
    repeat of ``M`` becomes ``L`` (and ``m`` becomes ``l``).
    """
    tokens = _TOKEN_RE.findall(d or "")
    index = 0
    command = ""
    while index < len(tokens):
        letter, number = tokens[index]
        if letter:
            command = letter
            index += 1
        elif not command:
            index += 1  # numbers before any command: malformed, skip
            continue

        count = ARGUMENT_COUNT[command.upper()]
        args: List[float] = []
        while len(args) < count:
            if index >= len(tokens) or tokens[index][0]:
                return  # ran out of numbers: stop rather than guess
            args.append(float(tokens[index][1]))
            index += 1
        yield command, args

        if command == "M":
            command = "L"
        elif command == "m":
            command = "l"


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
