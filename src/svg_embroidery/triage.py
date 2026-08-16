"""B2: suitability triage — is this image worth converting at all?

Not every image can become embroidery, and saying so *before* anyone waits on a
conversion is a feature rather than a failure. B1 built the instrument; this is
the step that turns four ratios into one word and a sentence explaining it.

**Triage does not measure anything.** It reads a :class:`~svg_embroidery.bench.Measurement`
— the same numbers ``svgemb bench`` prints, taken at the same resolution, against
the same profile — and grades them. One source of truth: ``svgemb assess`` and
``svgemb bench`` cannot disagree about an image, because there is only one place
the numbers come from.

The verdict is **the worst thing about the image**. Each reading below votes in
its own band and the lowest one wins, which is why the report names the reading
that decided rather than printing a score: "hopeless" is only useful next to
*why*.

What the readings are, and the evidence behind each threshold — the numbers are
this corpus, measured at each image's own profile resolution:

*Is it speckle?* ``edges >= 0.40``. A colour boundary on nearly half the pixels
is not artwork, it is grain: the three scans measure 0.62–0.67 and the crosshatch
0.86, while **everything else in the corpus is below 0.19**. The widest margin in
the whole model, and the most important one, because a speckled image has to be
read differently from a clean one:

    **On a speckled image, colour loss and flatness are measuring the grain, not
    the artwork.** Reduce a grainy scan of a two-colour drawing to three colours
    and every grain pixel moves — ``quant`` reports 0.87 about an image whose
    artwork is two flat colours. So those two readings abstain rather than vote,
    for the same reason B1 leaves a cell empty: a metric that cannot answer the
    question must not print a number.

    What *can* still be said under grain is whether anything survives the needle
    at all. At ``thin >= 0.95`` nothing in the image is wider than the minimum
    stitchable width, anywhere — and denoising cannot create width, so that is
    hopeless whatever preprocessing arrives later. The crosshatch scores 0.993;
    the scans 0.78–0.84, so they stay marginal.

*Is it tone?* ``quant >= 0.60`` on a clean image. Reducing to the profile's
colour budget repaints most of the picture and the loss is smooth shading rather
than detail — there are no flat regions to become stitches. The photographs and
gradients score 0.92–1.00; the worst clean image that is *not* hopeless is
``alpha-soft`` at 0.29.

*Otherwise*, three ordinary thresholds, any of which costs a band:

* ``quant > 0.15`` — one pixel in six repainted by the colour budget
* ``thin > 0.02`` — more than a fiftieth of the artwork finer than the needle
* ``flat < 0.85`` — less than that much of the image is flat colour

**What this gets right, and the one thing it does not.** Against the corpus's
``expect`` column — what a human says before measuring anything — triage agrees
on 19 of 20 images. The miss is ``scan-clean``, a line drawing on paper that a
human calls *good* and triage calls *marginal*, and it is the same gap B1
recorded and B3 exists to close: 84% of that image is finer than the needle
because grain is, and until something denoises it there is no way from here to
tell the drawing from the paper. The error is one band, in the cautious
direction, and the report says the word "denoising" out loud rather than
implying the artwork is at fault.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from .bench import Measurement


class Band(str, Enum):
    """How much of this image survives becoming stitches."""

    GOOD = "good"
    MARGINAL = "marginal"
    HOPELESS = "hopeless"

    @property
    def rank(self) -> int:
        return {"good": 0, "marginal": 1, "hopeless": 2}[self.value]

    @property
    def icon(self) -> str:
        return {"good": "✅", "marginal": "⚠️", "hopeless": "❌"}[self.value]


#: A colour boundary on this share of pixels means grain, not shapes.
SPECKLE_EDGES = 0.40

#: Nothing in the image survived the needle. Denoising cannot create width, so
#: this is hopeless whatever preprocessing arrives later.
ALL_TOO_FINE = 0.95

#: Colour reduction repaints this much of a *clean* image: it is tone, not art.
TONE_LOSS = 0.60

#: Ordinary thresholds — any one of these costs a band.
SOME_COLOUR_LOSS = 0.15
SOME_TOO_FINE = 0.02
FLAT_ENOUGH = 0.85


@dataclass(frozen=True)
class Reading:
    """One thing triage noticed, and the band it puts the image in."""

    key: str
    band: Band
    says: str

    def to_dict(self) -> Dict[str, Any]:
        return {"reading": self.key, "band": self.band.value, "says": self.says}


@dataclass(frozen=True)
class Abstention:
    """A reading that declined to vote, and why.

    Printed rather than dropped: a verdict resting on fewer readings than it
    looks like is the one report nobody can act on. Same judgement A6 made about
    checks that never ran.
    """

    key: str
    why: str

    def to_dict(self) -> Dict[str, Any]:
        return {"reading": self.key, "abstained": self.why}


def _percent(value: float) -> str:
    return f"{value * 100:.0f}%"


def _note_about(row: "Measurement", key: str) -> str:
    """B1 already wrote the reason a cell is empty — reuse it rather than guess."""
    for note in row.notes:
        if note.startswith(f"{key}:"):
            return note.split(":", 1)[1].strip()
    return f"{key} was not measured"


@dataclass
class Assessment:
    """What triage makes of one image."""

    name: str
    profile: str
    row: "Measurement"
    readings: List[Reading] = field(default_factory=list)
    abstained: List[Abstention] = field(default_factory=list)

    @property
    def verdict(self) -> Optional[Band]:
        """The worst band any reading voted for, or ``None`` if unmeasured."""
        if self.row.unmeasured:
            return None
        if not self.readings:
            return Band.GOOD
        return max((reading.band for reading in self.readings), key=lambda band: band.rank)

    @property
    def deciding(self) -> List[Reading]:
        """The readings that produced the verdict — what the report leads with."""
        return [reading for reading in self.readings if reading.band is self.verdict]

    def abstained_on(self, key: str) -> bool:
        """True when ``key`` declined to vote — one abstention can cover several."""
        return any(key in item.key for item in self.abstained)

    def headline(self) -> str:
        """What is in the image, before any judgement about it.

        The colour-loss figure is dropped when that reading abstained. Quoting a
        number in the first line that the verdict below then says cannot answer
        the question is exactly the thing this project keeps refusing to do.
        """
        row = self.row
        if row.unmeasured:
            return f"not assessed — {row.unmeasured}"
        bits = [row.size, f"{row.colors:,} colours"]
        if row.quant is not None and row.k and not self.abstained_on("colour loss"):
            bits.append(f"at {row.k} colours {_percent(row.quant)} of the image is repainted")
        return "; ".join(bits)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "image": self.name,
            "profile": self.profile,
            "verdict": self.verdict.value if self.verdict else None,
            "headline": self.headline(),
            "readings": [reading.to_dict() for reading in self.readings],
            "abstained": [item.to_dict() for item in self.abstained],
            "unmeasured": self.row.unmeasured,
            "measurement": self.row.to_dict(),
        }


def assess(row: "Measurement", name: str = "") -> Assessment:
    """Grade one measurement. Pure: it reads numbers and returns a verdict."""
    result = Assessment(name=name or row.name, profile=row.profile, row=row)
    if row.unmeasured:
        return result

    min_mm = row.min_mm or 1.5
    budget = row.k or 3

    # -- is it speckle? ------------------------------------------------------
    if row.edges is None:
        result.abstained.append(Abstention("speckle", _note_about(row, "edges")))
        speckled = False
    else:
        speckled = row.edges >= SPECKLE_EDGES

    if speckled:
        share = _percent(row.edges)
        if row.thin is not None and row.thin >= ALL_TOO_FINE:
            result.readings.append(
                Reading(
                    "speckle",
                    Band.HOPELESS,
                    f"{share} of the pixels sit on a colour boundary and {_percent(row.thin)} "
                    f"of the image is finer than {min_mm:g} mm — there is nothing in here "
                    "wider than the needle, so no amount of cleaning up recovers artwork "
                    "from it",
                )
            )
        else:
            result.readings.append(
                Reading(
                    "speckle",
                    Band.MARGINAL,
                    f"{share} of the pixels sit on a colour boundary: this is grain rather "
                    "than shapes. What is under it may well be drawable, but reading the "
                    "artwork through speckle needs denoising svgemb cannot do yet",
                )
            )
        # Both readings below would be describing the grain, not the design.
        result.abstained.append(
            Abstention(
                "colour loss and flatness",
                "on a speckled image these measure the grain rather than the artwork, so "
                "they cannot answer the questions they are named after",
            )
        )
        return result

    # -- is it tone? ---------------------------------------------------------
    if row.quant is None:
        result.abstained.append(Abstention("colour loss", _note_about(row, "quant")))
    elif row.quant >= TONE_LOSS:
        result.readings.append(
            Reading(
                "colour loss",
                Band.HOPELESS,
                f"reducing to {budget} colours repaints {_percent(row.quant)} of the image, "
                "and what is lost is smooth shading rather than detail — there are no flat "
                "regions here to become stitches",
            )
        )
    elif row.quant > SOME_COLOUR_LOSS:
        result.readings.append(
            Reading(
                "colour loss",
                Band.MARGINAL,
                f"reducing to {budget} colours repaints {_percent(row.quant)} of the image, "
                "so expect visible colour banding",
            )
        )

    # -- how fine is it? -----------------------------------------------------
    if row.thin is None:
        result.abstained.append(Abstention("fineness", _note_about(row, "thin")))
    elif row.thin > SOME_TOO_FINE:
        result.readings.append(
            Reading(
                "fineness",
                Band.MARGINAL,
                f"{_percent(row.thin)} of the artwork is finer than the {min_mm:g} mm this "
                "profile can stitch, and that much will thicken or disappear",
            )
        )

    # -- how much of it is flat colour? --------------------------------------
    if row.flat is None:  # pragma: no cover - flat is measurable whenever the row is
        result.abstained.append(Abstention("flatness", _note_about(row, "flat")))
    elif row.flat < FLAT_ENOUGH:
        result.readings.append(
            Reading(
                "flatness",
                Band.MARGINAL,
                f"only {_percent(row.flat)} of the image is flat colour; the rest is "
                "texture or shading a tracer has to guess at",
            )
        )

    return result


# --------------------------------------------------------------- rendering


LINE = "─" * 62

#: These sentences are the product, so they are wrapped rather than left to the
#: terminal — a reason that runs off the right edge does not get read.
WIDTH = 76


def _wrap(text: str, initial: str = "   ", subsequent: str = "      ") -> List[str]:
    import textwrap

    return textwrap.wrap(
        text, width=WIDTH, initial_indent=initial, subsequent_indent=subsequent
    ) or [initial + text]


def render_assessment(assessment: Assessment, verbose: bool = False, color: bool = True) -> str:
    """One image, for a human."""
    lines = [f"🖼  {assessment.name}   [profile: {assessment.profile}]", LINE]

    verdict = assessment.verdict
    if verdict is None:
        lines.extend(_wrap(assessment.headline(), initial="ℹ️  ", subsequent="    "))
        text = "\n".join(lines)
        return text if color else _plain(text)

    lines.append(assessment.headline())
    lines.append("")
    lines.append(f"{verdict.icon} {verdict.value.upper()}")
    shown = assessment.readings if verbose else assessment.deciding
    for reading in shown:
        lines.extend(_wrap(f"{reading.key}: {reading.says}"))
    if verdict is Band.GOOD and not shown:
        lines.append("   flat colour, crisp edges, nothing finer than the needle")

    for item in assessment.abstained:
        lines.extend(
            _wrap(f"{item.key} did not vote: {item.why}", initial="ℹ️  ", subsequent="    ")
        )

    text = "\n".join(lines)
    return text if color else _plain(text)


def render_summary(assessments: Sequence[Assessment], color: bool = True) -> str:
    """Several images, one line each."""
    width = max([len(a.name) for a in assessments] + [len("image")])
    lines = []
    for assessment in assessments:
        verdict = assessment.verdict
        if verdict is None:
            lines.append(f"ℹ️  {assessment.name:<{width}}  not assessed")
            continue
        deciding = assessment.deciding
        because = f"  ({deciding[0].key})" if deciding else ""
        lines.append(
            f"{verdict.icon} {assessment.name:<{width}}  {verdict.value}{because}"
        )
    counts = {band: 0 for band in Band}
    for assessment in assessments:
        if assessment.verdict is not None:
            counts[assessment.verdict] += 1
    lines.append(
        f"{counts[Band.GOOD]} good, {counts[Band.MARGINAL]} marginal, "
        f"{counts[Band.HOPELESS]} hopeless."
    )
    text = "\n".join(lines)
    return text if color else _plain(text)


def render_json(assessments: Sequence[Assessment]) -> str:
    import json

    return json.dumps([a.to_dict() for a in assessments], indent=2, ensure_ascii=False)


def _plain(text: str) -> str:
    for icon, word in (("✅", "[ok]"), ("⚠️", "[warn]"), ("❌", "[bad]"), ("ℹ️", "[note]"), ("🖼", "")):
        text = text.replace(icon, word)
    return text.strip("\n")


def render_thresholds() -> str:
    """What the bands mean — printed by ``--explain``."""
    return "\n".join(
        [
            "How triage reads an image. The verdict is the worst thing about it.",
            "",
            f"  speckle       edges >= {SPECKLE_EDGES:.2f}   a boundary on this many pixels is grain,",
            "                                not shapes; colour loss and flatness then",
            "                                describe the grain, so they do not vote",
            f"  ...and        thin  >= {ALL_TOO_FINE:.2f}   nothing survives the needle anywhere,",
            "                                which denoising cannot change: hopeless",
            f"  colour loss   quant >= {TONE_LOSS:.2f}   the loss is tone, not detail: hopeless",
            f"                quant >  {SOME_COLOUR_LOSS:.2f}   visible banding: marginal",
            f"  fineness      thin  >  {SOME_TOO_FINE:.2f}   this much is finer than the needle: marginal",
            f"  flatness      flat  <  {FLAT_ENOUGH:.2f}   less flat colour than this: marginal",
            "",
            "Every number is measured at the resolution the profile asks for, so",
            "'too fine' means too fine for that shop. See 'svgemb bench --explain'.",
        ]
    )
