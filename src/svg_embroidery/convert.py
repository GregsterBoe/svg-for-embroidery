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
from .findings import Finding, Report
from .preprocess import Preprocessed, Recipe
from .preprocess import run as preprocess_run
from .preprocess import upscale
from .profiles import Profile
from .raster import Raster, RasterError, downsample, load_image
from .tracer import DEFAULT_OVERLAP
from .tracer import INSTALL_HINT as TRACER_INSTALL_HINT
from .tracer import Backend, TracerError, default_backend, measure_svg

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
    #: Colour budget handed to the quantiser — the profile's ``color.max_count``
    #: until a retry lowers it.
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

    @property
    def canvas_cm(self) -> float:
        return self.canvas_mm / 10.0

    @property
    def mm_per_pixel(self) -> float:
        return self.canvas_mm / max(1, self.work_side)

    def describe(self) -> str:
        return f"{self.canvas_cm:.1f} cm, {self.colors} colour(s)"


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

    @property
    def passes(self) -> bool:
        return not self.report.errors

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
            for said_line in [
                f"{stage.name}: {stage.says}" for stage in attempt.prepared.stages
            ] + attempt.cleanup.notes():
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
        Recipe(colors=settings.colors, radius=radius, speck_area=settings.speck_area),
    )
    traced = backend.trace(
        prepared.quantisation, canvas_mm=settings.canvas_mm, overlap=settings.overlap
    )
    cleaned = b5.clean(traced.svg, profile)
    return Attempt(
        settings=settings,
        svg=cleaned.svg,
        report=cleaned.after,
        cleanup=cleaned,
        prepared=prepared,
        seconds=time.monotonic() - started,
    )


def convert(
    source: Raster,
    profile: Profile,
    backend: Optional[Backend] = None,
    tries: int = DEFAULT_TRIES,
    name: str = "",
    settings: Optional[Settings] = None,
) -> Conversion:
    """Turn an image into an SVG this profile accepts, or explain why not.

    ``backend=None`` picks whatever tracer this machine has. Unlike every other
    optional capability in the project, a missing one is fatal here rather than
    a measurement you don't get: there is no degraded conversion, so it raises
    with the install hint instead of returning half an answer.
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
    current = settings or start
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
        # What the loop wants answered. A failing document asks with its
        # errors; one that passed only by cutting artwork asks with the repair
        # it needed, which is the same complaint arriving as a receipt rather
        # than as a verdict.
        complaints = attempt.failing_rules
        if attempt.passes:
            if not attempt.cut_detail:
                break
            complaints = [FINE_DETAIL_RULE]
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
        decided = adjust(current, complaints, profile)
        if decided is None:
            break
        proposed, adjustment = decided
        if attempt.passes:
            adjustment = replace(
                adjustment,
                says="passes only after cutting detail finer than the needle out "
                "of the artwork, so it is " + adjustment.says,
            )
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
) -> Conversion:
    """:func:`convert`, from a file on disk."""
    path = Path(path)
    try:
        source = load_image(path)
    except RasterError as exc:
        raise ConvertError(f"{path}: {exc}") from exc
    return convert(source, profile, backend=backend, tries=tries, name=str(path))


# ------------------------------------------------------------- rendering


LINE = "─" * 62


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
            f"{best.settings.colors} colour(s), {best.shapes} shape(s), "
            f"{best.nodes} node(s), {conversion.tries} attempt(s)"
        )
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
    for finding in conversion.unmeasured:
        lines.append(f"ℹ️  not measured  {finding.rule_id}: {finding.message}")

    text = "\n".join(lines)
    if color:
        return text
    # The shared stripper knows the verdict icons; these three are this
    # command's own, and a --no-color run has to be readable on a terminal that
    # cannot draw them.
    plain = _strip_icons(text).replace("🖼", "Image:")
    return plain.replace("↻", "[retry]").replace("⏹", "[stop]")


def render_json(conversions: Sequence[Conversion]) -> str:
    import json

    return json.dumps([c.to_dict() for c in conversions], indent=2, ensure_ascii=False)
