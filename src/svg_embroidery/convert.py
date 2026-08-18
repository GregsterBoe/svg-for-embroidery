"""B6: closing the loop — an image in, an SVG the shop will accept out.

Everything B0–B5 built is a stage; this is the thing that runs them and then
*argues with the answer*. Convert, check, clean up, re-check — and when the
result still fails, change one setting and go round again rather than handing
back a document with the reason it was rejected attached.

**The loop is the step, not the single pass.** B5 already produced a passing
SVG for most of the corpus: 11 of the 14 images a human called good or
marginal, at ``embroidery-strict``. What it could not do is anything about the
other three, because a single pass has no way to say *"that was the wrong size
to stitch this at"*. The whole of B6 is the sentence after the failure.

**Which setting to change is the entire design, and the ordering is a claim
about what a conversion may cost the artwork.** Three knobs, tried in order of
what they take away:

1. **Stitch it bigger.** Detail below the needle is a *size* problem before it
   is a shape problem. A 0.4 mm stroke at 8 cm is a 1.5 mm stroke at 30 cm, and
   the design is not touched at all — the same document, presented at a size
   the same profile also calls acceptable. Nothing is invented and nothing is
   deleted, which is why it is tried first. (It is also, measured, the knob
   that closes the gate: see the roadmap.)
2. **Absorb the specks.** Regions the profile has *already* said in
   millimetres are too small to sew, removed before the tracer draws them
   rather than after. Capped at exactly that number: past it, despeckling
   stops removing specks and starts removing artwork.
3. **Drop a colour.** The palette itself shrinks, which is a real loss of the
   design, so it is last and it never goes below two — one colour is a
   silhouette, not a conversion.

**A pass is not always the end of it.** B5 runs the conversion path
destructively, so a document can satisfy the profile by having the too-fine
parts of the drawing cut off — measured on straight bars, 42% of the ink at
the smallest allowed canvas. The loop reads that repair as evidence about the
*size*: it tries the same image larger and keeps whichever result kept more of
the artwork. That costs one trace and is the difference between converting a
drawing and quietly deleting half of it.

Each attempt changes **one** knob, so the report can say which change bought
which improvement. A knob with no room left says so and the loop stops, which
is a different piece of news from "it failed": *the canvas is already at the
profile's maximum* tells you to talk to the shop, not to redraw the artwork.

**What this step does not do is invent a way to pass.** The document still goes
through B5's cleanup, which still runs the engine that verifies its own output,
and the verdict is still ``svgemb check``'s. A conversion that never passes
comes back saying what it tried, at what settings, and what is still wrong —
the same shape of answer A6 chose for ``svgemb fix``, for the same reason: the
useful half of a failed run is the account of it.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import cleanup as b5
from .bench import MAX_WORK_SIDE, canvas_and_stroke, color_budget, scale_for
from .colors import hex_to_rgb, normalize_color
from .document import parse_svg
from .findings import Finding, Report
from .preprocess import Preprocessed, Recipe, entry_for_color
from .preprocess import run as preprocess_run
from .preprocess import upscale
from .profiles import Profile
from .raster import Raster, RasterError, downsample, load_image
from .tracer import DEFAULT_OVERLAP
from .tracer import INSTALL_HINT as TRACER_INSTALL_HINT
from .tracer import (
    Backend,
    TracerError,
    default_backend,
    hex_color,
    layer_order,
    measure_svg,
)

#: How many times the loop may trace one image. Four is two more than the
#: corpus ever needs and few enough that a phone is not left grinding: the
#: expensive half is the tracer, and every attempt runs it again.
DEFAULT_TRIES = 4

#: How much bigger each retry asks to be stitched. Half again per attempt
#: converges on the *smallest* size that works rather than jumping to the
#: profile's maximum — a design delivered at 12 cm when 12 cm is enough costs
#: less thread and less hooping than one delivered at 30 cm.
CANVAS_STEP = 1.5

#: A retry never reduces the palette below this. Two colours is the least a
#: design can be and still be a design; one is a silhouette, and a conversion
#: that "passes" by flattening the artwork to a single block has answered a
#: different question from the one it was asked.
MIN_COLORS = 2

#: Enough of a change in canvas size to be worth another trace, in mm. Below
#: this the retry would re-trace the same document at a rounding error.
CANVAS_EPSILON = 1.0

#: The rule whose repair reshapes artwork rather than dropping what was never
#: viable. Named here because the loop reads it twice: as a complaint to
#: answer, and as a sign that an attempt which *passed* did so at a price.
FINE_DETAIL_RULE = "geometry.min_feature_size"


class ConvertError(Exception):
    """Raised when the conversion cannot start at all."""


# ------------------------------------------------------------- what to try


@dataclass(frozen=True)
class Settings:
    """One set of knobs, and everything an attempt needs to be repeatable."""

    #: Longest side of the finished design, in millimetres.
    canvas_mm: float
    #: Colour budget for the **artwork** — the profile's ``color.max_count``
    #: until a retry lowers it. B8 made this threads rather than palette
    #: entries: with :attr:`drop_background` set the quantiser is asked for one
    #: more than this and the extra one is the paper, so a design on white gets
    #: the same number of inks as the same design with an alpha channel.
    colors: int
    #: Resolution the image is traced at, in pixels on its longest side.
    work_side: int
    #: Connected regions smaller than this many working pixels are absorbed
    #: before tracing. One means absorb nothing, which is B3's default.
    speck_area: int = 1
    #: Working pixels each colour is grown under the ones stitched after it
    #: (B4). Not a retry knob: it is what stops the layers showing seams, and
    #: no failure this loop can see is answered by turning it off.
    overlap: int = DEFAULT_OVERLAP
    #: B8: leave the paper unstitched, so the garment is the background. Not a
    #: retry knob either — no failure the loop can see is answered by stitching
    #: the background back in, and turning it off costs a thread.
    drop_background: bool = True
    #: B9, experimental: sew the paper the artwork closes around rather than
    #: dropping it with the rest of the background. Not a retry knob either, and
    #: this one has a price the others do not — the paper becomes a thread, so a
    #: document that used it comes back with one colour more than the budget
    #: expected and the loop lowers the ink count to pay for it. That is the
    #: honest accounting: if the design needs white in the middle of it, white is
    #: a thread somebody has to load.
    sew_background_holes: bool = False
    #: C1: colours a **person** asked not to stitch, as ``#rrggbb``. The same
    #: mechanism as :attr:`drop_background` with the index picked by hand rather
    #: than by the corner fill, and not a retry knob for the third time: the loop
    #: has no business deleting a colour nobody complained about.
    #:
    #: Carried as colours rather than as palette indices because an index is
    #: only meaningful in the quantisation it came from, and every knob above
    #: re-quantises. :func:`plan_removals` resolves them per attempt and reports
    #: any it could not.
    remove: Tuple[str, ...] = ()

    @property
    def canvas_cm(self) -> float:
        return self.canvas_mm / 10.0

    @property
    def mm_per_pixel(self) -> float:
        return self.canvas_mm / max(1, self.work_side)

    def describe(self) -> str:
        said = f"{self.canvas_cm:.1f} cm, {self.colors} colour(s)"
        if self.sew_background_holes:
            said += ", enclosed ground sewn"
        if self.remove:
            said += f", {len(self.remove)} removed"
        return said


@dataclass(frozen=True)
class LayerInfo:
    """One palette entry of a conversion, and whether it is being sewn.

    What the layer panel lists (C1) and what ``svgemb convert`` prints as the
    thread list — one computation behind both, so a colour you can see in the
    terminal is a colour you can name to ``--remove`` and a colour you can tap.
    """

    #: ``#rrggbb``, which is also the fill the document paints it with.
    color: str
    #: Share of the working image this entry covers — its own pixels, before
    #: B4's trap grew it under the layers stitched after it.
    share: float
    stitched: bool = True
    #: Why not, when it is not: "background" (B8 found the paper) or "removed"
    #: (C1, a person picked it). Empty for a layer that is being sewn.
    reason: str = ""
    #: B9: this layer is ground the artwork closes around, being sewn in the
    #: same colour as the ground that is not. Without the flag the panel would
    #: show two rows in one colour, one bare and one sewn, and no reason for it.
    enclosed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "color": self.color,
            "share": round(self.share, 4),
            "stitched": self.stitched,
            "reason": self.reason,
            "enclosed": self.enclosed,
        }


def normalise_removals(values: Sequence[str]) -> Tuple[str, ...]:
    """Colour names, ``#rgb`` and ``#rrggbb`` down to one spelling, in order.

    Through the checker's own :func:`~svg_embroidery.colors.normalize_color`, so
    ``white`` names the same layer as ``#FFF`` here and in every rule — A3
    dropped a notation-normalising fixer precisely because that is already true
    everywhere else. Raises :class:`ValueError` on a colour it cannot read.
    """
    seen: List[str] = []
    for value in values:
        normalised = normalize_color(str(value).strip())
        if normalised is None:
            raise ValueError(f"{value!r} is not a colour")
        if normalised not in seen:
            seen.append(normalised)
    return tuple(seen)


@dataclass(frozen=True)
class Removal:
    """One colour somebody asked to leave unstitched, and what became of it.

    One record per *pick* rather than per entry removed, because a pick that did
    nothing is the case a panel has to be able to show: it is the only way to
    offer putting back a colour that is not in the document to be pointed at.
    """

    color: str
    #: The palette entry this pick names in this attempt, or ``None`` when it
    #: names none — the colour is not in the palette the settings produced.
    index: Optional[int] = None
    #: Why the pick changed nothing; empty when it did.
    says: str = ""

    @property
    def applied(self) -> bool:
        return self.index is not None and not self.says

    def to_dict(self) -> Dict[str, Any]:
        return {"color": self.color, "applied": self.applied, "says": self.says}


def plan_removals(prepared: Preprocessed, wanted: Sequence[str]) -> List[Removal]:
    """Which palette entries a person's picks name here, and what could not be done.

    C1's whole judgement, and it is three guards rather than a lookup — this is
    the one place where a colour goes missing because somebody tapped a button,
    so every way it can go wrong ends in a sentence rather than in a surprise:

    - **the colour is not in this palette.** A pick is resolved per attempt, and
      a retry that dropped the colour budget re-quantised the image underneath
      it. :func:`~svg_embroidery.preprocess.entry_for_color` refuses anything
      further off than a colour anyone would call the same one, so a removal
      that no longer applies is reported instead of landing on its neighbour.
    - **it is already unstitched**, because it is the paper B8 found. Removing
      it changes nothing, and saying nothing would look like the button failed.
    - **it is the last thing left.** A document with nothing in it is not a
      conversion, so the removal that would empty it is declined and named.

    One :class:`Removal` per pick, in the order they were picked.
    """
    quantisation = prepared.quantisation
    painted = {
        index for index in range(quantisation.colors) if quantisation.area(index) > 0
    }
    already = set() if prepared.background is None else {prepared.background}
    removed: List[int] = []
    picks: List[Removal] = []

    for color in wanted:
        # B9 can put the paper's colour in the palette twice — once as ground and
        # once as the part of it the artwork encloses. A pick then sits exactly
        # as far from both, and the one it can mean is the one being sewn.
        index = entry_for_color(quantisation, hex_to_rgb(color), avoid=tuple(already))
        if index is None or index not in painted:
            picks.append(Removal(color, None, (
                f"{color} is not a colour of this conversion — the palette was "
                "worked out again at these settings and nothing in it is that "
                "colour, so nothing was removed for it"
            )))
        elif index in already:
            picks.append(Removal(color, index, (
                f"{color} was already being left to the fabric as the background, "
                "so removing it changes nothing"
            )))
        elif index in removed:
            picks.append(Removal(color, index, (
                f"{color} is near enough to a colour already picked that the two "
                "name the same layer, so it removes nothing further"
            )))
        elif not painted - already - set(removed) - {index}:
            picks.append(Removal(color, index, (
                f"removing {color} would leave nothing to stitch at all, so it is "
                "kept — a document with no thread in it is not a conversion"
            )))
        else:
            removed.append(index)
            picks.append(Removal(color, index))
    return picks


def skip_set(prepared: Preprocessed, removed: Sequence[int]) -> Tuple[int, ...]:
    """The palette entries a trace leaves out: B8's paper, plus C1's picks.

    One set rather than two arguments, and that is the whole of why C1 is a
    small step. ``trapped_claims`` refuses to grow a layer under anything in it,
    so the rule that stops a fringe forming around a dropped background is the
    same rule that stops one forming around a colour somebody removed — it
    cannot be applied to one and forgotten for the other, because there is only
    one place it is applied.
    """
    background = prepared.background
    entries = () if background is None else (background,)
    return tuple(dict.fromkeys(tuple(entries) + tuple(removed)))


@dataclass(frozen=True)
class Adjustment:
    """One knob turned, and the failure that asked for it."""

    knob: str
    #: The rule whose complaint this answers, or "" when the knob had no room
    #: left and nothing was changed.
    rule_id: str
    says: str
    #: False when the knob is exhausted — the loop stops, and this is the
    #: reason it stopped rather than a change it made.
    turned: bool = True


@dataclass
class Attempt:
    """One trip through preprocess → trace → clean up → check."""

    settings: Settings
    svg: str
    #: The verdict on this document, after cleanup.
    report: Report
    cleanup: b5.Cleanup
    prepared: Preprocessed
    seconds: float = 0.0
    #: What the loop decided to do next, or ``None`` when it stopped here.
    adjustment: Optional[Adjustment] = None
    #: Anything the tracer thought worth saying — a colour left unstitched, or a
    #: colour tracer explaining that it could not leave one out.
    trace_notes: List[str] = field(default_factory=list)
    #: C1: what a person asked to leave unstitched, and what became of each
    #: pick. B8's background is *not* in here — it is ``prepared.background``,
    #: because a different thing decided it.
    picks: Tuple[Removal, ...] = ()

    @property
    def passes(self) -> bool:
        return not self.report.errors

    @property
    def removed(self) -> Tuple[int, ...]:
        """The palette entries the picks actually took out, in pick order."""
        return tuple(pick.index for pick in self.picks if pick.applied)

    @property
    def remove_notes(self) -> List[str]:
        """One line per pick that changed nothing, and why."""
        return [pick.says for pick in self.picks if pick.says]

    @property
    def skipped(self) -> Tuple[int, ...]:
        """Every palette entry this document does not paint, whoever decided."""
        return skip_set(self.prepared, self.removed)

    @property
    def layers(self) -> List[LayerInfo]:
        """Every colour this attempt found, in the order it would be stitched.

        Including the ones left unstitched, and that is the point: a panel that
        listed only what is being sewn could not offer to put a colour back, and
        a person looking at a hole wants to know what used to be in it.
        """
        quantisation = self.prepared.quantisation
        background = self.prepared.background
        out: List[LayerInfo] = []
        for index in layer_order(quantisation):
            share = quantisation.area(index)
            if share <= 0:
                continue
            reason = (
                "background"
                if index == background
                else "removed" if index in self.removed else ""
            )
            out.append(
                LayerInfo(
                    color=hex_color(quantisation.palette[index]),
                    share=share,
                    stitched=not reason,
                    reason=reason,
                    enclosed=index == self.prepared.enclosed,
                )
            )
        return out

    @property
    def errors(self) -> List[Finding]:
        return list(self.report.errors)

    @property
    def cut_detail(self) -> bool:
        """True when this document passes only because artwork was cut away.

        B5 runs the conversion path at the top risk level, which means a
        traced shape finer than the needle is *reshaped* — the thin runs cut
        off — rather than reported. That is the right policy for a document
        nobody drew, and it is still a loss: measured on a drawing of straight
        bars, one pass at the smallest allowed canvas passes the profile after
        removing 42% of the ink.

        So a repair here is read as evidence about the **size**, not about the
        artwork: detail this fine is what a design looks like when it is being
        sewn smaller than it can be. It costs one more trace to find out, and
        the loop keeps whichever result kept more of the drawing.

        Only the reshaping repair counts. ``geometry.min_area`` drops specks
        the profile has already said in millimetres cannot be sewn at all, and
        B5 answers that question in advance for exactly that reason.
        """
        return any(
            fix.rule_id == FINE_DETAIL_RULE for fix in self.cleanup.fix.applied
        )

    @property
    def failing_rules(self) -> List[str]:
        seen = []
        for finding in self.report.errors:
            if finding.rule_id not in seen:
                seen.append(finding.rule_id)
        return seen

    @property
    def inks(self) -> int:
        """Colours actually in the document, which is threads on the machine.

        Not ``settings.colors``: that is the *budget*, and since B8 a run can
        come in under it — a one-colour monogram on paper is one ink and bare
        fabric, and reporting "3 colour(s)" because the profile allows three
        reads as a claim about the file rather than about the permission.
        """
        return len(parse_svg(self.svg).colors())

    @property
    def shapes(self) -> int:
        return measure_svg(self.svg)[0]

    @property
    def nodes(self) -> int:
        return measure_svg(self.svg)[1]


@dataclass
class Conversion:
    """Every attempt made on one image, and the document that came out."""

    name: str
    profile: str
    tracer: str
    attempts: List[Attempt] = field(default_factory=list)
    #: Set when the source had too few pixels to trace and was enlarged first.
    upscaled: str = ""

    @property
    def svg(self) -> str:
        """The document to keep."""
        return self.best.svg

    @property
    def best(self) -> Attempt:
        """The attempt worth handing back, which is not always the last one.

        Four things in order, and each one is a claim: a document that passes
        beats one that does not; among those that do not, fewer complaints is
        closer; a document that kept the whole drawing beats one that passed
        by cutting part of it away; and among equals the **smallest** wins,
        because every retry here asked for something — a larger hoop, a colour
        dropped — and a change that bought no improvement is a change to hand
        back unmade. That last clause is why an image nothing works on comes
        back at the size it was asked for rather than at the size the last
        retry had grown it to.
        """
        return min(
            self.attempts,
            key=lambda a: (
                not a.passes,
                len(a.errors),
                a.cut_detail,
                a.settings.canvas_mm,
            ),
        )

    @property
    def best_index(self) -> int:
        """Which attempt :attr:`best` picked, for a UI that lists them all."""
        best = self.best
        return next(i for i, attempt in enumerate(self.attempts) if attempt is best)

    @property
    def passes(self) -> bool:
        return any(attempt.passes for attempt in self.attempts)

    @property
    def ok(self) -> bool:
        """False when the fix engine caught its own output misbehaving (A2)."""
        return self.best.cleanup.ok

    @property
    def tries(self) -> int:
        return len(self.attempts)

    @property
    def settings(self) -> Settings:
        return self.best.settings

    @property
    def unmeasured(self) -> List[Finding]:
        """Checks that could not run here — an install away from an answer."""
        return list(self.best.report.unmeasured)

    def adjustments(self) -> List[Adjustment]:
        return [a.adjustment for a in self.attempts if a.adjustment is not None]

    def notes(self) -> List[str]:
        """One line per thing worth knowing, in the words each step printed.

        A line already printed for an earlier attempt is dropped rather than
        repeated: most retries change the canvas alone, so every stage says
        exactly what it said last time, and four identical copies of the
        pipeline bury the one line that is new.
        """
        lines: List[str] = []
        said = set()
        if self.upscaled:
            lines.append(f"convert: {self.upscaled}")
        for index, attempt in enumerate(self.attempts, start=1):
            for said_line in (
                [f"{stage.name}: {stage.says}" for stage in attempt.prepared.stages]
                + [f"remove: {note}" for note in attempt.remove_notes]
                + [f"trace: {note}" for note in attempt.trace_notes]
                + attempt.cleanup.notes()
            ):
                if said_line in said:
                    continue
                said.add(said_line)
                lines.append(f"try {index}/{said_line}")
            if attempt.adjustment is not None:
                because = (
                    f" [{attempt.adjustment.rule_id}]" if attempt.adjustment.rule_id else ""
                )
                lines.append(f"try {index}/retry{because}: {attempt.adjustment.says}")
        lines.append(
            f"convert: {'passed' if self.passes else 'did not pass'} at "
            f"{self.settings.describe()} after {self.tries} attempt(s)"
        )
        return lines

    def to_dict(self) -> Dict[str, Any]:
        return {
            "image": self.name,
            "profile": self.profile,
            "tracer": self.tracer,
            "passes": self.passes,
            "verified": self.ok,
            "tries": self.tries,
            "canvas_cm": round(self.settings.canvas_cm, 2),
            "colors": self.settings.colors,
            "work_side": self.settings.work_side,
            "speck_area": self.settings.speck_area,
            "shapes": self.best.shapes,
            "nodes": self.best.nodes,
            "layers": [layer.to_dict() for layer in self.best.layers],
            "attempts": [
                {
                    "canvas_cm": round(attempt.settings.canvas_cm, 2),
                    "colors": attempt.settings.colors,
                    "speck_area": attempt.settings.speck_area,
                    "errors": [finding.to_dict() for finding in attempt.errors],
                    "adjustment": (
                        {
                            "knob": attempt.adjustment.knob,
                            "because": attempt.adjustment.rule_id,
                            "says": attempt.adjustment.says,
                            "turned": attempt.adjustment.turned,
                        }
                        if attempt.adjustment
                        else None
                    ),
                }
                for attempt in self.attempts
            ],
            "report": self.best.report.to_dict(),
        }


# ------------------------------------------------------ reading the profile


def canvas_limits(profile: Profile) -> Tuple[float, float]:
    """The smallest and largest design this profile will accept, in mm.

    The smallest is where every conversion starts, and it is the same reading
    B1 measures at — the pessimistic end of the range, because a design that
    stitches at the smallest size the shop allows stitches at every size it
    allows. The largest is the ceiling on the first retry knob.

    A profile that sets no maximum has no opinion, so the ceiling is the try
    budget rather than a number invented here. Where a profile bounds the axes
    separately, the tightest of the bounds wins: a traced document is as wide
    as it is tall in the same proportion as its image, so the canvas cannot
    exceed the strictest limit on either side.
    """
    smallest, _ = canvas_and_stroke(profile)
    ceilings: List[float] = []
    for spec in profile.rules:
        if spec.id != "geometry.canvas_size":
            continue
        for key in ("max_cm", "max_width_cm", "max_height_cm"):
            value = spec.params.get(key)
            if value is not None:
                ceilings.append(float(value) * 10.0)
    largest = min(ceilings) if ceilings else math.inf
    return smallest, max(smallest, largest)


def min_area_mm2(profile: Profile) -> float:
    """The smallest patch this profile calls stitchable, in mm².

    From ``geometry.min_area`` where the profile has it. Where it does not, the
    square of the minimum feature width, which is the same reasoning
    ``embroidery-strict`` writes out in its own comment: the smallest patch a
    stitch that wide can fill in both directions.
    """
    for spec in profile.rules:
        if spec.id == "geometry.min_area":
            value = spec.params.get("min_mm2")
            if value is not None:
                return float(value)
    minimum = canvas_and_stroke(profile)[1]
    return minimum * minimum


def working_side(profile: Profile, source_side: int) -> Tuple[int, str]:
    """The resolution to trace at, and a note when the source is enlarged first.

    The profile picks it, exactly as it picks the resolution B1 measures at:
    enough pixels that the minimum stitchable feature spans a whole kernel. The
    difference is what happens when the source has fewer than that.

    **Measuring refuses to upsample and converting has to.** A metric read off
    invented pixels answers a question the image cannot support — B1 leaves the
    cell empty and says why. But a 32×32 icon has enough *shape* to trace and
    not enough *pixels*, and the tracer is not measuring anything: it is
    following a boundary, which is easier to do well on a bigger grid. That is
    the one stage B3 built and could not use (:func:`preprocess.upscale`, whose
    docstring says it belongs to the conversion path); this is the conversion
    path.
    """
    # Ask what the profile wants with no source to cap it: scale_for takes the
    # smaller of the source and the profile's answer, so handing it the
    # instrument's own ceiling gives the profile's answer alone.
    wanted = scale_for(profile, MAX_WORK_SIDE).work_side
    if source_side >= wanted:
        return wanted, ""
    return wanted, (
        f"enlarged the source from {source_side}px to {wanted}px before tracing — "
        f"too few pixels to follow a boundary at {wanted}px, and repeating them "
        "invents no detail"
    )


def initial_settings(profile: Profile, source_side: int) -> Tuple[Settings, str]:
    """Where a conversion starts: everything read off the profile, nothing invented."""
    smallest, _ = canvas_limits(profile)
    side, note = working_side(profile, source_side)
    return (
        Settings(canvas_mm=smallest, colors=color_budget(profile), work_side=side),
        note,
    )


# -------------------------------------------------------------- the knobs


#: Complaints each knob can answer. A rule not named by any knob is one this
#: loop has no argument with, and the run stops rather than turning something
#: at random and hoping.
TOO_FINE = ("geometry.min_feature_size", "geometry.min_area", "stroke.min_width")
TOO_MANY_SHAPES = ("path.max_count",)
TOO_MANY_COLOURS = ("color.max_count",)


def _bigger_canvas(
    settings: Settings, profile: Profile
) -> Tuple[Optional[Settings], str]:
    """Stitch the same artwork larger, within what the profile allows."""
    _, largest = canvas_limits(profile)
    wanted = settings.canvas_mm * CANVAS_STEP
    room = min(wanted, largest)
    if room - settings.canvas_mm < CANVAS_EPSILON:
        return None, (
            f"the design is already {settings.canvas_cm:.1f} cm, which is the "
            f"largest this profile accepts — detail this fine needs the artwork "
            "redrawn, not the canvas resized"
        )
    return (
        replace(settings, canvas_mm=room),
        f"stitched larger: {settings.canvas_cm:.1f} → {room / 10:.1f} cm; the "
        "artwork is unchanged, only the size it is sewn at",
    )


def _more_despeckling(
    settings: Settings, profile: Profile
) -> Tuple[Optional[Settings], str]:
    """Absorb the regions the profile has already called too small to sew."""
    # The cap *is* the profile's number, converted to this attempt's pixels:
    # beyond it, despeckling stops removing specks and starts removing artwork.
    cap = int(min_area_mm2(profile) / (settings.mm_per_pixel ** 2))
    if cap <= settings.speck_area or cap < 2:
        return None, (
            "specks under the profile's minimum area are already being absorbed "
            "before tracing; the shapes that are left are ones it calls stitchable"
        )
    return (
        replace(settings, speck_area=cap),
        f"too many shapes to sew, so regions under {cap}px — the profile's "
        f"{min_area_mm2(profile):.2f} mm² at this size — are absorbed before "
        "tracing rather than traced and then deleted",
    )


def _fewer_colours(
    settings: Settings, profile: Profile
) -> Tuple[Optional[Settings], str]:
    """Give the quantiser a smaller budget. The one knob that loses design."""
    if settings.colors <= MIN_COLORS:
        return None, (
            f"the palette is already down to {settings.colors}, and below that "
            "there is no design left to convert"
        )
    return (
        replace(settings, colors=settings.colors - 1),
        f"still more colours than the profile allows, so the budget drops "
        f"{settings.colors} → {settings.colors - 1}. This one loses part of the "
        "design rather than presenting it differently",
    )


#: In the order they are tried, which is the order of what they cost the
#: artwork: a size change costs nothing, absorbing specks costs shapes the
#: profile already rejected, and dropping a colour costs the design itself.
KNOBS: Sequence[
    Tuple[str, Sequence[str], Callable[[Settings, Profile], Tuple[Optional[Settings], str]]]
] = (
    ("canvas", TOO_FINE, _bigger_canvas),
    ("speckle", TOO_MANY_SHAPES, _more_despeckling),
    ("colours", TOO_MANY_COLOURS, _fewer_colours),
)


@dataclass(frozen=True)
class Limits:
    """How far each knob may be turned, read off the profile.

    The loop turns these itself and never needs to know the range — it asks for
    one step and is told whether there was room. B7 needs the range as a number,
    because a slider has two ends, and they are the same numbers: the canvas
    bounds are :func:`canvas_limits`, the colour ceiling is the profile's own
    ``color.max_count``, and the speck cap is ``min_area`` in this attempt's
    pixels, which is exactly where :func:`_more_despeckling` stops.
    """

    canvas_min_mm: float
    canvas_max_mm: float
    #: True when the profile sets no maximum canvas at all. The loop treats that
    #: as "no ceiling" and stops when it runs out of attempts; a slider cannot,
    #: so the page invents an end and this flag is how it says so out loud.
    canvas_open_ended: bool
    #: Never above the profile's own ``color.max_count``: a budget over the
    #: limit only buys a document that fails the check it was converted for.
    #: The floor is the loop's two-colour rule, or the profile's ceiling where
    #: that is lower still — a shop that stocks one thread means one thread.
    colors_min: int
    colors_max: int
    speck_min: int
    speck_max: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "canvas_min_cm": round(self.canvas_min_mm / 10.0, 2),
            "canvas_max_cm": round(self.canvas_max_mm / 10.0, 2),
            "canvas_open_ended": self.canvas_open_ended,
            "colors_min": self.colors_min,
            "colors_max": self.colors_max,
            "speck_min": self.speck_min,
            "speck_max": self.speck_max,
        }


#: What a slider offers when the profile names no maximum canvas. The loop's own
#: ceiling in that case is the try budget — four attempts at half again each —
#: so this is that same reach, and it is the page's number rather than the
#: shop's, which is why :attr:`Limits.canvas_open_ended` exists to say so.
OPEN_ENDED_CANVAS = CANVAS_STEP ** DEFAULT_TRIES


def limits_for(profile: Profile, settings: Settings) -> Limits:
    """The range of every knob, at the size this attempt is being made."""
    smallest, largest = canvas_limits(profile)
    open_ended = not math.isfinite(largest)
    if open_ended:
        largest = smallest * OPEN_ENDED_CANVAS
    budget = color_budget(profile)
    return Limits(
        canvas_min_mm=smallest,
        canvas_max_mm=largest,
        canvas_open_ended=open_ended,
        colors_min=min(MIN_COLORS, budget),
        colors_max=budget,
        speck_min=1,
        speck_max=max(1, int(min_area_mm2(profile) / (settings.mm_per_pixel ** 2))),
    )


def clamp(settings: Settings, profile: Profile) -> Tuple[Settings, List[str]]:
    """Pull hand-set knobs back inside what the profile allows, and say so.

    B7 lets someone turn these by hand, which is the one thing the loop cannot
    do — but a slider dragged past the shop's own limit is asking for a document
    the shop will reject, and answering it silently would produce a conversion
    that fails a check it never had to fail. Clamping *and reporting* is the
    same choice A6 made about a repair it declined to make: the interesting half
    of the answer is the reason.
    """
    limits = limits_for(profile, settings)
    notes: List[str] = []
    canvas = min(max(settings.canvas_mm, limits.canvas_min_mm), limits.canvas_max_mm)
    if abs(canvas - settings.canvas_mm) > 0.05:
        notes.append(
            f"{settings.canvas_cm:.1f} cm is outside what this profile accepts, so "
            f"the design is sized at {canvas / 10:.1f} cm"
        )
    colors = min(max(settings.colors, limits.colors_min), limits.colors_max)
    if colors != settings.colors:
        notes.append(
            f"{settings.colors} colours is outside {limits.colors_min}–"
            f"{limits.colors_max}, so the budget is {colors}"
        )
    # Re-read the speck cap at the size that survived: it is a number of pixels,
    # and how much a pixel is worth is exactly what the canvas knob changes.
    at_size = replace(settings, canvas_mm=canvas, colors=colors)
    cap = limits_for(profile, at_size).speck_max
    speck = min(max(settings.speck_area, 1), cap)
    if speck != settings.speck_area:
        notes.append(
            f"absorbing regions under {settings.speck_area}px would remove artwork at "
            f"this size, so it stops at {speck}px — the profile's "
            f"{min_area_mm2(profile):.2f} mm²"
        )
    return replace(at_size, speck_area=speck), notes


def adjust(
    settings: Settings, failing: Sequence[str], profile: Profile
) -> Optional[Tuple[Optional[Settings], Adjustment]]:
    """What to change after a failed attempt, or ``None`` when nothing applies.

    Returns the next settings and the reason, or ``(None, reason)`` when the
    knob that would have answered is out of room — which is a result worth
    printing rather than a silent stop.
    """
    for knob, rules, turn in KNOBS:
        because = next((rule for rule in rules if rule in failing), "")
        if not because:
            continue
        proposed, says = turn(settings, profile)
        return proposed, Adjustment(
            knob=knob, rule_id=because, says=says, turned=proposed is not None
        )
    return None


# --------------------------------------------------------------- the loop


def prepare(source: Raster, settings: Settings) -> Raster:
    """The pixels an attempt works from, at the resolution the profile asked for.

    Both directions, and each is a no-op when it does not apply: a source with
    too few pixels is enlarged, one with too many is reduced.
    """
    return downsample(upscale(source, settings.work_side), settings.work_side)


def kernel_radius(profile: Profile, settings: Settings) -> int:
    """The profile's minimum feature, in this attempt's working pixels.

    B1's :func:`~svg_embroidery.bench.scale_for` computes the same thing for
    the resolution *it* chose; recomputed here because a retry can change the
    canvas, and the same physical minimum is then a different number of pixels.
    A design stitched half again as large has half again fewer pixels per
    millimetre, so a kernel carried over from the first attempt would be asking
    about a width nobody specified.
    """
    minimum = canvas_and_stroke(profile)[1]
    width_pixels = minimum / settings.mm_per_pixel
    return max(1, int(round((width_pixels - 1.0) / 2.0)))


def one_attempt(
    work: Raster, profile: Profile, settings: Settings, backend: Backend
) -> Attempt:
    """B3 → B4 → B5 → check, once, at these settings."""
    started = time.monotonic()
    radius = kernel_radius(profile, settings)
    prepared = preprocess_run(
        work,
        Recipe(
            colors=settings.colors,
            radius=radius,
            speck_area=settings.speck_area,
            drop_background=settings.drop_background,
            sew_background_holes=settings.sew_background_holes,
        ),
    )
    # C1: a colour a person removed and B8's paper are one skip set from here
    # on. Built once, so the trap that stops a fringe forming under a dropped
    # layer cannot be applied to one of them and forgotten for the other.
    picks = plan_removals(prepared, settings.remove)
    removed = tuple(pick.index for pick in picks if pick.applied)
    traced = backend.trace(
        prepared.quantisation,
        canvas_mm=settings.canvas_mm,
        overlap=settings.overlap,
        # Empty unless the pipeline found a background it was willing to drop
        # and nobody picked a colour, so this is the pre-B8 path unchanged.
        skip=skip_set(prepared, removed),
    )
    cleaned = b5.clean(traced.svg, profile)
    return Attempt(
        settings=settings,
        svg=cleaned.svg,
        report=cleaned.after,
        cleanup=cleaned,
        prepared=prepared,
        seconds=time.monotonic() - started,
        trace_notes=list(traced.notes),
        picks=tuple(picks),
    )


def plan_next(
    attempt: Attempt, profile: Profile
) -> Optional[Tuple[Optional[Settings], Adjustment]]:
    """What to try after this attempt, or ``None`` when there is nothing to argue with.

    The loop's whole judgement, in one function: which complaint to answer, what
    turning that knob would cost, and whether a document that *passed* did so at
    a price worth one more trace. :func:`convert` runs it in a ``for``; B7's page
    runs it one attempt per request, so a phone can show each try as it lands
    instead of staring at a spinner for four of them. Both get the same
    decisions because there is only one copy of them.
    """
    complaints = attempt.failing_rules
    if attempt.passes:
        if not attempt.cut_detail:
            return None
        complaints = [FINE_DETAIL_RULE]
    decided = adjust(attempt.settings, complaints, profile)
    if decided is None:
        return None
    proposed, adjustment = decided
    if attempt.passes:
        adjustment = replace(
            adjustment,
            says="passes only after cutting detail finer than the needle out "
            "of the artwork, so it is " + adjustment.says,
        )
    return proposed, adjustment


def convert(
    source: Raster,
    profile: Profile,
    backend: Optional[Backend] = None,
    tries: int = DEFAULT_TRIES,
    name: str = "",
    settings: Optional[Settings] = None,
    drop_background: bool = True,
    sew_background_holes: bool = False,
    remove: Sequence[str] = (),
) -> Conversion:
    """Turn an image into an SVG this profile accepts, or explain why not.

    ``backend=None`` picks whatever tracer this machine has. Unlike every other
    optional capability in the project, a missing one is fatal here rather than
    a measurement you don't get: there is no degraded conversion, so it raises
    with the install hint instead of returning half an answer.

    ``drop_background=False`` stitches the paper as a colour, which is what
    every conversion did before B8. It is an escape hatch rather than a knob:
    the loop never turns it, because no failure it can see is answered by
    spending a thread on fabric.

    ``sew_background_holes=True`` splits that decision in two (B9): the ground
    outside the artwork is still left to the fabric, and the ground the artwork
    *closes around* is sewn in its own colour. It is for the case B8's colour
    test cannot see — a white shirt drawn on white paper is paper by colour and
    design by intent — and it is experimental because it costs a thread, which
    the retry loop then takes out of the ink budget.

    ``remove`` names colours to leave unstitched by hand (C1) — the same
    mechanism with a person choosing the index. Removing a colour does **not**
    hand its thread back to the artwork the way B8's paper does: the budget
    stays where it was, so every other layer comes back exactly as it was drawn
    and the only difference is the hole. A colour spent on something you did not
    want is still a colour you chose to spend; lowering the budget is the knob
    for spending it elsewhere, and it is right there next to this one.
    """
    backend = backend or default_backend()
    if backend is None:
        raise ConvertError(
            f"converting an image needs a tracer, and none is installed here — "
            f"{TRACER_INSTALL_HINT}"
        )
    if tries < 1:
        raise ConvertError("a conversion needs at least one attempt")

    source_side = max(source.width, source.height)
    start, note = initial_settings(profile, source_side)
    current = replace(
        settings or start,
        drop_background=drop_background,
        sew_background_holes=sew_background_holes,
        remove=normalise_removals(remove) if remove else (settings or start).remove,
    )
    if source_side >= current.work_side:  # caller-supplied settings may not need it
        note = ""
    # Once, outside the loop: no knob changes the resolution, and re-deriving
    # the pixels every attempt would be re-answering a question nothing asked.
    work = prepare(source, current)

    conversion = Conversion(
        name=name, profile=profile.name, tracer=backend.name, upscaled=note
    )
    for number in range(1, tries + 1):
        try:
            attempt = one_attempt(work, profile, current, backend)
        except TracerError as exc:  # a tracer that fails mid-run is not a verdict
            raise ConvertError(str(exc)) from exc
        conversion.attempts.append(attempt)
        # A document that passed with the whole drawing intact is the end of it.
        # Everything else has something to answer: a failing one asks with its
        # errors, and one that passed only by cutting artwork asks with the
        # repair it needed — the same complaint arriving as a receipt rather
        # than as a verdict. :func:`plan_next` reads both.
        if attempt.passes and not attempt.cut_detail:
            break
        if number == tries:
            # Never print a change that was not made: the loop stops here, and
            # what would have been turned next is not what happened.
            attempt.adjustment = Adjustment(
                knob="tries",
                rule_id="",
                says=f"out of attempts after {tries}; --tries N allows more",
                turned=False,
            )
            break
        decided = plan_next(attempt, profile)
        if decided is None:
            break
        proposed, adjustment = decided
        attempt.adjustment = adjustment
        if proposed is None:
            break
        current = proposed
    return conversion


def convert_file(
    path: Path,
    profile: Profile,
    backend: Optional[Backend] = None,
    tries: int = DEFAULT_TRIES,
    drop_background: bool = True,
    sew_background_holes: bool = False,
    remove: Sequence[str] = (),
) -> Conversion:
    """:func:`convert`, from a file on disk."""
    path = Path(path)
    try:
        source = load_image(path)
    except RasterError as exc:
        raise ConvertError(f"{path}: {exc}") from exc
    return convert(
        source,
        profile,
        backend=backend,
        tries=tries,
        name=str(path),
        drop_background=drop_background,
        sew_background_holes=sew_background_holes,
        remove=remove,
    )


# ------------------------------------------------------------- rendering


LINE = "─" * 62


#: How the reason a layer is not stitched reads in a terminal.
WHY_BARE = {
    "background": "left unstitched: the ground the corners found",
    "removed": "left unstitched: you removed it",
}


def render_layers(layers: Sequence[LayerInfo]) -> List[str]:
    """The threads, largest first, and what happened to the ones that are not.

    Printed by every conversion because it is the list ``--remove`` names its
    colours from — and because "3 ink(s)" says how many without saying which.
    """
    if not layers:
        return []
    out = ["threads, in the order they are sewn:"]
    for layer in layers:
        mark = "🧵" if layer.stitched else "  "
        why = "" if layer.stitched else f"   {WHY_BARE.get(layer.reason, layer.reason)}"
        # Two rows in one colour otherwise read as a bug rather than as B9's
        # whole point: the ground outside the artwork and the ground inside it.
        if layer.enclosed:
            why = "   the ground the artwork closes around, so it is sewn"
        out.append(f"   {mark} {layer.color}  {layer.share:>6.1%}{why}")
    return out


def render_conversion(
    conversion: Conversion, verbose: bool = False, color: bool = True
) -> str:
    """What the loop did, attempt by attempt, and what came out.

    The attempts are the report. A conversion that passed on the second try
    tells you the first size was too small for the artwork, which is worth
    knowing before the next image from the same customer; one that never
    passed tells you which knob ran out.
    """
    from .report import _strip_icons

    lines = [
        f"🖼 {conversion.name or '<image>'}   [profile: {conversion.profile}]",
        LINE,
    ]
    if conversion.upscaled:
        lines.append(f"ℹ️  {conversion.upscaled}")

    for index, attempt in enumerate(conversion.attempts, start=1):
        verdict = (
            ("✅ passes, artwork cut" if attempt.cut_detail else "✅ passes")
            if attempt.passes
            else f"❌ {len(attempt.errors)} error(s)"
        )
        lines.append(
            f"try {index}  {attempt.settings.describe():<22} {verdict}"
            + (f"   [{', '.join(attempt.failing_rules)}]" if attempt.errors else "")
        )
        if verbose:
            for stage in attempt.prepared.stages:
                lines.append(f"       {stage.name}: {stage.says}")
            for note in attempt.cleanup.notes():
                lines.append(f"       {note}")
        elif not attempt.passes:
            for finding in attempt.errors[:3]:
                lines.append(f"       {finding.message}")
        if attempt.adjustment is not None:
            icon = "↻" if attempt.adjustment.turned else "⏹"
            lines.append(f"       {icon} {attempt.adjustment.says}")

    best = conversion.best
    lines.append(LINE)
    if not conversion.ok:
        lines.append(
            "❌ verification FAILED: svgemb caught its own output misbehaving, so "
            "the cleaned document was discarded."
        )
    if conversion.passes:
        lines.append(
            f"✅ converted at {best.settings.canvas_cm:.1f} cm: "
            f"{best.inks} ink(s), {best.shapes} shape(s), "
            f"{best.nodes} node(s), {conversion.tries} attempt(s)"
        )
        # B8: the paper is the loudest thing this run did to the artwork and the
        # one thing a preview on a white page cannot show, so it is said without
        # -v. Same reasoning as A6's skip list: a run accounts for what it left
        # out as plainly as for what it did.
        for note in best.trace_notes:
            lines.append(f"ℹ️  {note}")
        if best.cut_detail:
            lines.append(
                "ℹ️  detail finer than the needle was cut out of the artwork to get "
                "here, and no size this profile allows kept it — the drawing is "
                "finer than the thread, not the other way round."
            )
    else:
        rules = ", ".join(best.failing_rules) or "no rule"
        lines.append(
            f"❌ still failing after {conversion.tries} attempt(s): {rules}"
        )
        for finding in best.errors:
            lines.append(f"   {finding.message}")
    # C1: the colours are printed whether or not the run passed, because they
    # are what --remove is named against. A list you have to turn -v on to see
    # is a flag you cannot use without guessing.
    lines.extend(render_layers(best.layers))
    # A pick that did not happen is the same kind of news as A6's skip list, and
    # the same rule applies: the run accounts for what it did not do as plainly
    # as for what it did.
    for note in best.remove_notes:
        lines.append(f"⚠️  {note}")
    for finding in conversion.unmeasured:
        lines.append(f"ℹ️  not measured  {finding.rule_id}: {finding.message}")

    text = "\n".join(lines)
    if color:
        return text
    # The shared stripper knows the verdict icons; these three are this
    # command's own, and a --no-color run has to be readable on a terminal that
    # cannot draw them.
    plain = _strip_icons(text).replace("🖼", "Image:").replace("🧵", "* ")
    return plain.replace("↻", "[retry]").replace("⏹", "[stop]")


def render_json(conversions: Sequence[Conversion]) -> str:
    import json

    return json.dumps([c.to_dict() for c in conversions], indent=2, ensure_ascii=False)
