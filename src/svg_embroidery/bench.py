"""B1: the benchmark — measuring the corpus, and measuring changes to it.

The roadmap's reason for building this before anything that converts an image:
*without objective measurement, every later tweak is guesswork.* A denoising
kernel that looks better on one logo and quietly ruins every scan is the normal
outcome of tuning by eye, and the only defence is a table that covers the whole
range and a baseline to diff against.

So ``svgemb bench`` does two things:

1. Measures every image in the corpus against the profile it is aimed at, and
   prints one row each.
2. Compares those numbers to a saved baseline and says, per cell, whether the
   change was an improvement or a regression — which requires knowing, for each
   metric, which direction is *better*. That is recorded in :data:`METRICS` and
   is the reason the table can grade itself.

**What is measured today, and what is not.** B0 wired a tracer in behind the
``paths`` and ``fit`` columns, so those fill in whenever this machine has one;
``passes`` waits for B6 and still prints ``·``. The columns describing the image
and the colour reduction are the ones the roadmap says quality actually comes
from, and they are filled everywhere.

**A run is only comparable to a baseline taken the same way.** Both the working
resolution and the tracer change every number in the table, so a diff across
either is refused with the reason rather than printed — see
:func:`incomparable`.

**A measurement you can't take is not a failure.** An image this machine cannot
decode — a JPEG with no Pillow — is reported as unmeasured with the reason,
the run continues, and the summary says how many rows are missing. Same rule as
everywhere else in the project.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .profiles import Profile, ProfileError, load_profile
from .raster import (
    Quantisation,
    RasterError,
    downsample,
    edge_density,
    flat_ratio,
    has_alpha,
    load_image,
    quantise,
    thin_ratio,
    unique_colors,
)
from .tracer import Backend as TracerBackend
from .tracer import DEFAULT_OVERLAP
from .tracer import INSTALL_HINT as TRACER_INSTALL_HINT
from .tracer import TracerError, default_backend, measure_svg
from .triage import Band, assess
from .visual import Raster, compare_rasters, render, show_through

#: Where the corpus lives when nothing else is said. Kept out of ``tests/`` on
#: purpose: it is a measuring instrument that happens to be checked in, not a
#: fixture directory, and it is meant to grow with real images over time.
DEFAULT_CORPUS = Path(__file__).resolve().parents[2] / "bench" / "corpus"
DEFAULT_BASELINE = Path(__file__).resolve().parents[2] / "bench" / "baseline.json"

#: Ratio metrics move in the last decimal on any float change. Only call a cell
#: different when it moved by more than this, or every run is a "regression".
RATIO_EPSILON = 0.0005

#: Bumped when a baseline recorded by an older build would be *silently*
#: misread — a new column it cannot supply, or a new condition it never
#: recorded. 2: B4's ``gaps`` column and the seam overlap the run was taken at.
#: 3: B5's ``passes`` column, which was declared empty from the start, and the
#: cleanup the run was taken with. 4: B6's ``tries`` column, the conversion
#: loop, and the profile a run was aimed at. An old baseline is refused with
#: the command to re-record it, rather than diffed against and reported as
#: twenty regressions.
BASELINE_VERSION = 4


class BenchError(Exception):
    """Raised when the corpus itself cannot be read."""


# ------------------------------------------------------------------ corpus


@dataclass(frozen=True)
class CorpusEntry:
    """One image and what it is supposed to become."""

    name: str
    file: str
    profile: str
    category: str
    #: What a human would say before measuring: good / marginal / hopeless.
    #: B2's triage is graded against this; B1 only carries it.
    expect: str
    note: str = ""
    directory: Path = field(default=DEFAULT_CORPUS, compare=False)

    @property
    def path(self) -> Path:
        return self.directory / self.file


def load_corpus(directory: Optional[Path] = None) -> List[CorpusEntry]:
    """Read ``manifest.yaml`` from a corpus directory."""
    directory = Path(directory or DEFAULT_CORPUS)
    manifest = directory / "manifest.yaml"
    if not manifest.is_file():
        raise BenchError(
            f"no corpus manifest at {manifest} — run 'python bench/make_corpus.py' to build it"
        )
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - PyYAML is a core dependency
        raise BenchError("reading the manifest needs PyYAML") from exc

    try:
        data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise BenchError(f"{manifest}: {exc}") from exc
    if not isinstance(data, dict):
        raise BenchError(f"{manifest}: expected a mapping with an 'images' list")
    images = data.get("images")
    if not isinstance(images, list) or not images:
        raise BenchError(f"{manifest}: 'images' must be a non-empty list")

    entries = []
    for index, item in enumerate(images):
        if not isinstance(item, dict) or "name" not in item or "file" not in item:
            raise BenchError(f"{manifest}: image #{index + 1} needs at least 'name' and 'file'")
        entries.append(
            CorpusEntry(
                name=str(item["name"]),
                file=str(item["file"]),
                profile=str(item.get("profile") or "embroidery-basic"),
                category=str(item.get("category") or "other"),
                expect=str(item.get("expect") or "unknown"),
                note=str(item.get("note") or ""),
                directory=directory,
            )
        )
    return entries


# ----------------------------------------------------------------- metrics


@dataclass(frozen=True)
class Metric:
    """One column, and which way is up."""

    key: str
    header: str
    width: int
    kind: str  # int | ratio | text
    #: "lower", "higher", or "" when a change is neither good nor bad.
    better: str
    about: str


METRICS: Sequence[Metric] = (
    # First, so it sits next to the ``expect`` column it is graded against —
    # B2's gate is "does triage separate the corpus", and this is where you read
    # the answer off. No direction: a verdict that moves is news either way.
    Metric("verdict", "verdict", 8, "text", "", "triage's answer: good / marginal / hopeless (B2)"),
    Metric("size", "size", 9, "text", "", "pixel dimensions of the source"),
    Metric("res", "res", 4, "int", "",
           "resolution the metrics ran at, chosen so the profile's minimum feature "
           "spans a whole kernel"),
    Metric("mm_px", "mm/px", 6, "text", "",
           "millimetres one working pixel covers, at the profile's smallest canvas"),
    Metric("colors", "colours", 8, "int", "", "distinct RGB values in the source"),
    Metric("k", "k", 3, "int", "", "colour budget, from the profile's color.max_count"),
    Metric("flat", "flat", 6, "ratio", "higher",
           "share of pixels identical to all four neighbours — how vector-like it is"),
    Metric("quant", "quant", 6, "ratio", "lower",
           "share of pixels changed by reducing to k colours"),
    Metric("edges", "edges", 6, "ratio", "lower",
           "share of pixels on a colour boundary — a stand-in for path count"),
    Metric("thin", "thin", 6, "ratio", "lower",
           "share of the image in features too fine for this profile's needle"),
    Metric("paths", "paths", 6, "int", "lower",
           "subpaths in the traced SVG — shapes the machine has to fill"),
    Metric("nodes", "nodes", 6, "int", "lower",
           "drawing commands in the traced SVG — where stitch count comes from"),
    Metric("fit", "fit", 6, "ratio", "lower",
           "share of pixels the traced SVG gets wrong against the quantised source"),
    Metric("gaps", "gaps", 6, "ratio", "lower",
           "share of the traced SVG left unpainted — the seams between colour "
           "layers, which show as hairlines of bare fabric (B4)"),
    Metric("passes", "passes", 6, "text", "",
           "does the traced SVG pass its profile — 'yes', or how many errors are "
           "left (B5; B6 adds the retry that argues with the answer)"),
    Metric("tries", "tries", 5, "int", "lower",
           "conversions the loop needed before the document passed, or before it "
           "ran out of settings to change (B6)"),
)

METRIC_BY_KEY = {metric.key: metric for metric in METRICS}


@dataclass
class Measurement:
    """One row of the table."""

    name: str
    category: str
    expect: str
    profile: str
    size: str = ""
    res: Optional[int] = None
    mm_px: Optional[float] = None
    colors: Optional[int] = None
    k: Optional[int] = None
    #: The profile's thinnest stitchable line, in millimetres. Recorded so the
    #: row is self-describing: ``thin`` means nothing without the width it was
    #: measured against, and B2 quotes it back to the user.
    min_mm: Optional[float] = None
    alpha: Optional[bool] = None
    #: B2's word for this row, filled by :mod:`svg_embroidery.triage` from the
    #: numbers beside it. Not measured separately — see that module.
    verdict: Optional[str] = None
    flat: Optional[float] = None
    quant: Optional[float] = None
    edges: Optional[float] = None
    thin: Optional[float] = None
    paths: Optional[int] = None
    nodes: Optional[int] = None
    fit: Optional[float] = None
    gaps: Optional[float] = None
    passes: Optional[str] = None
    #: How many times B6's loop had to trace this image. One means the first
    #: settings worked; empty means the row was not converted, only traced.
    tries: Optional[int] = None
    #: Wall-clock seconds the tracer took on this image. Deliberately *not* a
    #: metric: it is a property of the machine as much as of the code, and
    #: putting it in the baseline would make every run a regression. It is
    #: recorded because choosing between tracers needs it.
    trace_seconds: Optional[float] = None
    #: Non-empty when the row could not be measured here, saying why.
    unmeasured: str = ""
    #: Reasons individual cells are empty, when the row as a whole is fine.
    notes: List[str] = field(default_factory=list)

    def value(self, key: str) -> Any:
        return getattr(self, key, None)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


#: Working pixels the profile's minimum feature should span. Three is the
#: smallest odd kernel, so a radius-1 opening asks exactly the profile's
#: question. Larger would be more faithful and quadratically slower.
KERNEL_PIXELS = 3

#: Never measure above this, whatever the profile asks for — pure-Python
#: morphology is O(pixels) per colour and the corpus has twenty images.
MAX_WORK_SIDE = 256
MIN_WORK_SIDE = 24


@dataclass(frozen=True)
class Scale:
    """How the working image relates to the physical design."""

    work_side: int
    radius: int
    mm_per_pixel: float
    min_mm: float

    @property
    def kernel_mm(self) -> float:
        """What the thinness test actually asks, after integer rounding."""
        return (2 * self.radius + 1) * self.mm_per_pixel

    @property
    def faithful(self) -> bool:
        """True when the kernel is close enough to the profile's real minimum.

        A low-resolution source cannot answer the question at all: at three
        millimetres per pixel the smallest possible kernel already means "is
        this thinner than 9mm", which is not what the shop asked. Reporting a
        number there would be worse than reporting none, so the thinness cell
        goes empty and says why — the same rule the rest of the project follows.
        """
        return self.kernel_mm <= self.min_mm * 1.5


def canvas_and_stroke(profile: Profile) -> Tuple[float, float]:
    """The profile's smallest canvas (mm) and thinnest stitchable line (mm).

    The *smallest* canvas is the pessimistic choice on purpose — the same
    artwork printed smaller has physically finer detail, so sizing the kernel
    there answers "is this too fine anywhere in the allowed range" rather than
    at the flattering end of it.
    """
    min_cm, min_mm = 10.0, 1.5  # the built-in defaults, if the profile is silent
    for spec in profile.rules:
        if spec.id == "geometry.canvas_size":
            min_cm = float(spec.params.get("min_cm", min_cm))
        elif spec.id == "stroke.min_width":
            min_mm = float(spec.params.get("min_mm", min_mm))
    return min_cm * 10.0, min_mm


def scale_for(profile: Profile, source_side: int, work_side: Optional[int] = None) -> Scale:
    """Pick the resolution to measure at, and the kernel that goes with it.

    This is not a performance knob, it is a correctness one. Measure a design
    at too few pixels and the minimum feature rounds to one pixel, so the
    thinness test silently asks a different — much harsher — question than the
    profile does, and condemns strokes that stitch perfectly well. So the
    resolution is derived from the profile: enough pixels that the minimum
    feature spans :data:`KERNEL_PIXELS`, capped for speed and never above the
    source, since upsampling would invent detail that isn't there.
    """
    canvas_mm, min_mm = canvas_and_stroke(profile)
    wanted = int(-(-KERNEL_PIXELS * canvas_mm // min_mm))  # ceil
    side = work_side or min(MAX_WORK_SIDE, max(MIN_WORK_SIDE, min(source_side, wanted)))
    mm_per_pixel = canvas_mm / max(1, side)
    # A feature `width` pixels across survives an opening of radius
    # (width - 1) / 2, so that is the radius which asks the profile's question.
    # Halving the width instead — the obvious-looking formula — builds a kernel
    # nearly twice too wide and condemns strokes that stitch perfectly well.
    width_pixels = min_mm / mm_per_pixel
    radius = max(1, int(round((width_pixels - 1.0) / 2.0)))
    return Scale(work_side=side, radius=radius, mm_per_pixel=mm_per_pixel, min_mm=min_mm)


def color_budget(profile: Profile) -> int:
    """The profile's ``color.max_count``, or a sane default."""
    for spec in profile.rules:
        if spec.id == "color.max_count":
            return int(spec.params.get("max_colors", 3))
    return 3


def trace_row(
    row: Measurement,
    reduced: Quantisation,
    canvas_mm: float,
    backend: TracerBackend,
    overlap: int = DEFAULT_OVERLAP,
    profile: Optional[Profile] = None,
    cleanup: bool = False,
) -> None:
    """Fill in the columns that describe the traced SVG, or say why they are empty.

    Two separate capabilities are needed here and they fail independently: the
    tracer produces ``paths``/``nodes``, and only a renderer can say whether the
    result looks like the source. Missing either costs those cells and nothing
    else.

    With ``cleanup``, B5 runs over the traced document first and every column
    describes the cleaned result — which is the point of the flag: ``paths``
    and ``nodes`` are what the machine is asked to sew, and cleanup is where
    the shapes it cannot sew come out.
    """
    try:
        traced = backend.trace(reduced, canvas_mm=canvas_mm, overlap=overlap)
    except TracerError as exc:
        row.notes.append(f"paths: not traced — {exc}")
        return
    svg = traced.svg
    row.trace_seconds = round(traced.seconds, 4)
    for note in traced.notes:
        row.notes.append(f"{backend.name}: {note}")

    if profile is not None:
        from . import cleanup as b5

        if cleanup:
            cleaned = b5.clean(svg, profile)
            svg = cleaned.svg
            row.notes.extend(cleaned.notes())
            verdict = cleaned.after
        else:
            verdict = b5.check(svg, profile)
        # The B5 gate, per image: does what came out pass the profile it was
        # aimed at? Filled whether or not cleanup ran, because the interesting
        # number is the difference between the two.
        row.passes = "yes" if not verdict.errors else f"{len(verdict.errors)} err"

    # Counted from the document that will actually be stitched, so the columns
    # and the verdict describe the same file.
    row.paths, row.nodes = measure_svg(svg)

    shot = render(svg, width=reduced.width)
    if shot is None:
        row.notes.append(
            "fit: not measurable — tracing worked, but no renderer is installed "
            "to compare the result against the source"
        )
        return
    row.fit = compare_rasters(reduced.raster(), shot).ratio
    # Free, off the same render: what the layered document failed to paint at
    # all. ``fit`` cannot separate that from a colour in the wrong place, and a
    # seam is the failure a shop notices first — it is bare fabric.
    row.gaps = show_through(shot).area


def convert_row(
    row: Measurement,
    source,
    reduced: Quantisation,
    profile: Profile,
    backend: TracerBackend,
    tries: Optional[int] = None,
) -> None:
    """Fill in the trace columns from B6's loop rather than from a single trace.

    The difference from :func:`trace_row` is the *whole* of B6: the same three
    stages run, and then the result is checked and the settings adjusted until
    it passes or the loop runs out of arguments. So ``paths``, ``nodes`` and
    ``passes`` describe the document a shop would actually be sent, and the new
    ``tries`` column says what it cost to get there.

    ``fit`` is still measured against the row's own quantisation — the image as
    this profile's colour budget allows it — so a retry that dropped a colour
    is visible as the concession it is, rather than being graded against its
    own reduced palette and scoring well for it.
    """
    from .convert import ConvertError, convert

    try:
        conversion = convert(
            source, profile, backend=backend, name=row.name,
            **({} if tries is None else {"tries": tries}),
        )
    except ConvertError as exc:
        row.notes.append(f"paths: not converted — {exc}")
        return

    svg = conversion.svg
    row.tries = conversion.tries
    row.passes = "yes" if conversion.passes else f"{len(conversion.best.errors)} err"
    row.paths, row.nodes = measure_svg(svg)
    row.trace_seconds = round(sum(attempt.seconds for attempt in conversion.attempts), 4)
    row.notes.extend(conversion.notes())

    shot = render(svg, width=reduced.width)
    if shot is None:
        row.notes.append(
            "fit: not measurable — converting worked, but no renderer is installed "
            "to compare the result against the source"
        )
        return
    row.fit = compare_rasters(reduced.raster(), shot).ratio
    row.gaps = show_through(shot).area


def measure(
    entry: CorpusEntry,
    work_side: Optional[int] = None,
    backend: Optional[TracerBackend] = None,
    preprocess: bool = False,
    overlap: int = DEFAULT_OVERLAP,
    cleanup: bool = False,
    convert: bool = False,
    tries: Optional[int] = None,
) -> Measurement:
    """Everything B1 can say about one image, plus what B0's tracer makes of it.

    With ``preprocess``, the image goes through B3's pipeline first and every
    number describes the result: the metrics run on the cleaned image, and the
    tracer traces the Lab quantisation rather than the RGB one. That is a
    different question from the raw run, not a better answer to the same one, so
    :func:`incomparable` refuses to diff the two.

    ``convert`` and ``preprocess`` are separate flags here and tied together by
    the command, deliberately: the loop preprocesses at its own settings, so a
    row whose image columns describe one pipeline and whose document came from
    another is a row nobody can read.
    """
    row = Measurement(
        name=entry.name, category=entry.category, expect=entry.expect, profile=entry.profile
    )
    try:
        profile = load_profile(entry.profile)
    except ProfileError as exc:
        row.unmeasured = str(exc)
        return row

    try:
        source = load_image(entry.path)
    except RasterError as exc:
        row.unmeasured = str(exc)
        return row

    return measure_raster(
        source,
        row,
        profile,
        work_side=work_side,
        backend=backend,
        preprocess=preprocess,
        overlap=overlap,
        cleanup=cleanup,
        convert=convert,
        tries=tries,
    )


def measure_raster(
    source: Raster,
    row: Measurement,
    profile: Profile,
    work_side: Optional[int] = None,
    backend: Optional[TracerBackend] = None,
    preprocess: bool = False,
    overlap: int = DEFAULT_OVERLAP,
    cleanup: bool = False,
    convert: bool = False,
    tries: Optional[int] = None,
) -> Measurement:
    """:func:`measure`, from pixels already in memory rather than from a file.

    Split out for B7: an image uploaded to the web UI never becomes a file, and
    the page has to be able to say what ``svgemb assess`` would say about it.
    Sharing the body rather than the intent is the point — one implementation
    means the browser and the terminal cannot grade the same image differently.
    """
    row.size = f"{source.width}x{source.height}"
    row.alpha = has_alpha(source)

    scale = scale_for(profile, max(source.width, source.height), work_side)
    work = downsample(source, scale.work_side)
    row.k = color_budget(profile)

    reduced: Optional[Quantisation] = None
    if preprocess:
        from .preprocess import Recipe
        from .preprocess import run as preprocess_run

        # Upscaling is deliberately left off here: it invents no detail, but it
        # would let the thinness test claim an answer at a resolution the source
        # does not have. It belongs to the conversion path (B6), not to
        # measuring. See preprocess.upscale.
        result = preprocess_run(work, Recipe(colors=row.k, radius=scale.radius))
        work = result.cleaned
        reduced = result.quantisation
        row.notes.extend(f"preprocess/{stage.name}: {stage.says}" for stage in result.stages)

    row.res = max(work.width, work.height)
    row.mm_px = round(scale.mm_per_pixel, 3)
    row.min_mm = scale.min_mm
    row.colors = unique_colors(work)
    row.flat = flat_ratio(work)

    if reduced is None:
        reduced = quantise(work, row.k)
    row.quant = compare_rasters(work, reduced.raster()).ratio
    row.edges = edge_density(reduced)
    if scale.faithful:
        row.thin = thin_ratio(reduced, scale.radius)
    else:
        row.notes.append(
            f"thin: not measurable — at {scale.mm_per_pixel:.2f} mm/px the smallest "
            f"kernel asks {scale.kernel_mm:.1f} mm, not the profile's {scale.min_mm:.1f} mm"
        )

    if backend is not None and convert:
        # From the source rather than from `work`: the loop does its own
        # preprocessing, at its own settings, and may enlarge a small image
        # first — which is the one thing measuring must not do.
        convert_row(row, source, reduced, profile, backend, tries=tries)
    elif backend is not None:
        trace_row(
            row,
            reduced,
            canvas_and_stroke(profile)[0],
            backend,
            overlap=overlap,
            profile=profile,
            cleanup=cleanup,
        )

    # B2 grades the row it is handed rather than measuring again, so ``svgemb
    # assess`` and ``svgemb bench`` cannot disagree about an image. Last, so the
    # verdict sees every cell — including the ones the tracer filled in.
    verdict = assess(row).verdict
    row.verdict = verdict.value if verdict else None
    return row


def measure_file(
    path: Path,
    profile: str = "embroidery-basic",
    work_side: Optional[int] = None,
    backend: Optional[TracerBackend] = None,
) -> Measurement:
    """Measure one image off the corpus — B2's single-file entry point.

    Deliberately the same :func:`measure` underneath: an image assessed on its
    own has to score exactly what it would score as a corpus row, or the
    benchmark stops describing the tool.
    """
    path = Path(path)
    return measure(
        CorpusEntry(
            name=path.name,
            file=path.name,
            profile=profile,
            category="",
            expect="",
            directory=path.parent,
        ),
        work_side=work_side,
        backend=backend,
    )


@dataclass
class BenchRun:
    """A whole sweep of the corpus."""

    rows: List[Measurement]
    #: The ``--work-side`` override, or ``None`` when each row's resolution came
    #: from its own profile. Recorded because it changes every ratio in the
    #: table, so a baseline taken at one resolution must not be diffed against
    #: a run at another without saying so.
    work_side: Optional[int]
    corpus: str
    #: Which tracer filled in ``paths``/``nodes``/``fit``, or "" when this
    #: machine has none. Recorded for the same reason ``work_side`` is: two
    #: tracers do not produce comparable numbers, so a diff across them is a
    #: diff of the tracers, not of the code under test.
    tracer: str = ""
    #: And the version of it, for the same reason one step finer: potrace 1.16
    #: and potrace 1.10 do not have to agree on a curve.
    tracer_version: str = ""
    #: Whether B3's pipeline ran before measuring. Recorded for the same reason
    #: as the other two: it changes every number in the table, so a preprocessed
    #: run and a raw one answer different questions and must not be diffed.
    preprocess: bool = False
    #: How far each colour was grown under the ones stitched after it (B4). One
    #: more condition that moves ``paths``, ``nodes``, ``fit`` and ``gaps``
    #: together, so it is recorded and diffed like the rest of them.
    overlap: int = DEFAULT_OVERLAP
    #: Whether B5 cleaned each traced document before it was measured. Same
    #: reason again: it is meant to move ``paths`` and ``nodes``, so a run with
    #: it and a run without it are answers to different questions.
    cleanup: bool = False
    #: Whether B6's loop produced each document, retries and all. It implies
    #: the two above and can change the canvas and the palette on top, so it is
    #: the strongest condition of the lot and diffed like the rest of them.
    convert: bool = False
    #: Set when every entry was aimed at one profile instead of the one its
    #: manifest names. The profile decides the colour budget, the resolution
    #: and what counts as too fine, so a run at a different one is measuring a
    #: different question — the same reason the resolution is recorded.
    profile: str = ""

    @property
    def measured(self) -> List[Measurement]:
        return [row for row in self.rows if not row.unmeasured]

    @property
    def unmeasured(self) -> List[Measurement]:
        return [row for row in self.rows if row.unmeasured]

    def averages(self) -> Dict[str, float]:
        """Mean of every ratio metric over the rows that could be measured."""
        out = {}
        for metric in METRICS:
            if metric.kind != "ratio":
                continue
            values = [
                row.value(metric.key)
                for row in self.measured
                if row.value(metric.key) is not None
            ]
            if values:
                out[metric.key] = sum(values) / len(values)
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": BASELINE_VERSION,
            "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "corpus": self.corpus,
            "work_side": self.work_side,
            "tracer": self.tracer,
            "tracer_version": self.tracer_version,
            "preprocess": self.preprocess,
            "overlap": self.overlap,
            "cleanup": self.cleanup,
            "convert": self.convert,
            "profile": self.profile,
            "averages": self.averages(),
            "rows": {row.name: row.to_dict() for row in self.rows},
        }


#: Passed as ``backend`` to ask for no tracing at all, as opposed to ``None``,
#: which means "whichever tracer this machine has".
NO_TRACER = "none"


def resolve_backend(name: Optional[str]) -> Optional[TracerBackend]:
    """Turn a ``--tracer`` argument into a backend, or ``None`` for no tracing."""
    if name == NO_TRACER:
        return None
    if name:
        from .tracer import backend_named

        try:
            backend = backend_named(name)
        except TracerError as exc:  # a typo is a usage error, not a crash
            raise BenchError(str(exc)) from exc
        if not backend.available():
            raise BenchError(f"tracer {name} is not available here — {backend.install}")
        return backend
    return default_backend()


def with_profile(entries: Sequence[CorpusEntry], name: str) -> List[CorpusEntry]:
    """Aim the whole corpus at one profile, whatever each entry names.

    The manifest's own profile is the right default — a metric like "too thin
    to stitch" means too thin *for that shop* — but a gate stated at one
    profile has to be runnable at that profile, and B6's is: 80% of the good
    and marginal images at ``embroidery-strict``.
    """
    return [replace(entry, profile=name) for entry in entries]


def run(
    entries: Sequence[CorpusEntry],
    work_side: Optional[int] = None,
    corpus: str = "",
    backend: Optional[TracerBackend] = None,
    preprocess: bool = False,
    overlap: int = DEFAULT_OVERLAP,
    cleanup: bool = False,
    convert: bool = False,
    tries: Optional[int] = None,
    profile: str = "",
) -> BenchRun:
    """Measure every entry. ``backend=None`` means *do not trace*, not *pick one*.

    Choosing a default is policy and lives in the command (see
    :func:`resolve_backend`), so that calling this directly — from a test, or
    from another tool — gives the same numbers on every machine.
    """
    if profile:
        entries = with_profile(entries, profile)
    return BenchRun(
        rows=[
            measure(
                entry,
                work_side=work_side,
                backend=backend,
                preprocess=preprocess,
                overlap=overlap,
                cleanup=cleanup,
                convert=convert,
                tries=tries,
            )
            for entry in entries
        ],
        work_side=work_side,
        corpus=corpus,
        tracer=backend.name if backend else "",
        tracer_version=backend.version() if backend else "",
        preprocess=preprocess,
        overlap=overlap,
        cleanup=cleanup,
        convert=convert,
        profile=profile,
    )


# ---------------------------------------------------------------- baseline


@dataclass(frozen=True)
class Change:
    """One cell that moved between the baseline and this run."""

    name: str
    metric: str
    before: Any
    after: Any
    #: "better", "worse", or "changed" when the metric has no direction.
    verdict: str

    def __str__(self) -> str:
        return f"{self.name}  {self.metric}: {_cell(self.metric, self.before)} → {_cell(self.metric, self.after)}"


def load_baseline(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BenchError(f"no baseline at {path} — run 'svgemb bench --save' to record one") from exc
    except json.JSONDecodeError as exc:
        raise BenchError(f"{path}: {exc}") from exc
    if data.get("version") != BASELINE_VERSION:
        raise BenchError(
            f"{path}: baseline version {data.get('version')}, this build writes "
            f"{BASELINE_VERSION} — re-record it with 'svgemb bench --save'"
        )
    return data


def incomparable(baseline: Dict[str, Any], current: BenchRun) -> List[str]:
    """Reasons this run must not be diffed against this baseline.

    The project's own rule — *a metric that can't answer the question must not
    print a number* — applies to the diff as much as to the table. Change the
    working resolution or the tracer and every cell moves, so a diff across
    either measures the conditions rather than the code, and is worse than no
    diff at all because it looks like an answer.
    """
    reasons = []
    was, now = baseline.get("work_side"), current.work_side
    if was != now:
        reasons.append(
            f"resolution: baseline at {was or 'per profile'}, this run at "
            f"{now or 'per profile'}"
        )
    was, now = baseline.get("tracer", ""), current.tracer
    if was != now:
        reasons.append(
            f"tracer: baseline used {was or 'none'}, this run used {now or 'none'}"
        )
    elif baseline.get("tracer_version", "") != current.tracer_version:
        reasons.append(
            f"tracer version: baseline on {now} "
            f"{baseline.get('tracer_version') or 'unknown'}, this run on "
            f"{current.tracer_version or 'unknown'}"
        )
    was, now = bool(baseline.get("preprocess", False)), current.preprocess
    if was != now:
        reasons.append(
            f"preprocessing: baseline measured the image "
            f"{'after B3 cleaned it' if was else 'as handed in'}, this run "
            f"{'after B3 cleaned it' if now else 'as handed in'}"
        )
    # Only worth saying when something was traced: with no tracer there are no
    # layers to overlap and nothing to clean, so neither setting can have moved
    # a number.
    if current.tracer:
        was, now = baseline.get("overlap", DEFAULT_OVERLAP), current.overlap
        if was != now:
            reasons.append(
                f"seam overlap: baseline grew each layer {was}px under the next, "
                f"this run {now}px"
            )
        was, now = bool(baseline.get("cleanup", False)), current.cleanup
        if was != now:
            reasons.append(
                f"cleanup: baseline measured the trace "
                f"{'after B5 cleaned it' if was else 'as the tracer left it'}, this run "
                f"{'after B5 cleaned it' if now else 'as the tracer left it'}"
            )
        was, now = bool(baseline.get("convert", False)), current.convert
        if was != now:
            reasons.append(
                f"conversion: baseline traced each image "
                f"{'through B6’s retry loop' if was else 'once'}, this run "
                f"{'through B6’s retry loop' if now else 'once'}"
            )
    was, now = baseline.get("profile", ""), current.profile
    if was != now:
        reasons.append(
            f"profile: baseline aimed the corpus at {was or 'each image’s own profile'}, "
            f"this run at {now or 'each image’s own profile'}"
        )
    return reasons


def _moved(metric: Metric, before: Any, after: Any) -> bool:
    if before is None and after is None:
        return False
    if before is None or after is None:
        return True
    if metric.kind == "ratio":
        return abs(float(before) - float(after)) > RATIO_EPSILON
    return before != after


def compare(baseline: Dict[str, Any], current: BenchRun) -> List[Change]:
    """Every cell that moved, graded better or worse where that has meaning."""
    old_rows = baseline.get("rows", {})
    changes: List[Change] = []

    for row in current.rows:
        old = old_rows.get(row.name)
        if old is None:
            changes.append(Change(row.name, "(row)", None, "new", "changed"))
            continue
        for metric in METRICS:
            before, after = old.get(metric.key), row.value(metric.key)
            if not _moved(metric, before, after):
                continue
            verdict = "changed"
            if metric.better and before is not None and after is not None:
                try:
                    rose = float(after) > float(before)
                except (TypeError, ValueError):
                    rose = None
                if rose is not None:
                    improved = rose if metric.better == "higher" else not rose
                    verdict = "better" if improved else "worse"
            changes.append(Change(row.name, metric.key, before, after, verdict))

    for name in old_rows:
        if not any(row.name == name for row in current.rows):
            changes.append(Change(name, "(row)", "present", None, "changed"))
    return changes


# --------------------------------------------------------------- rendering


def _cell(key: str, value: Any) -> str:
    if value is None:
        return "·"
    metric = METRIC_BY_KEY.get(key)
    if metric is None:
        return str(value)
    if metric.kind == "ratio":
        return f"{float(value):.3f}"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def graded(bench: BenchRun) -> List[Measurement]:
    """Rows whose ``expect`` is a real band, so triage can be graded on them."""
    bands = {band.value for band in Band}
    return [
        row for row in bench.measured if row.expect in bands and row.verdict is not None
    ]


def disagreements(bench: BenchRun) -> List[Measurement]:
    """Rows where triage and the human ``expect`` column do not match.

    B2's gate — *it correctly separates the corpus* — is this list being short
    and every entry on it having a reason. Printed with the run rather than
    hidden in a test, because it is the number that says whether the thresholds
    still hold as the corpus grows.
    """
    return [row for row in graded(bench) if row.verdict != row.expect]


#: What share of the images a human called convertible must come out of B6's
#: loop passing their profile, unattended. The roadmap's gate for the step.
CONVERSION_BAR = 0.80


def convertible(bench: BenchRun) -> List[Measurement]:
    """Rows a human said were worth converting — B6 is graded on these.

    Not the whole corpus: half of it is photographs and gradients that the
    roadmap says should *not* become embroidery, and a loop scored on those
    would be rewarded for converting them.
    """
    return [row for row in bench.measured if row.expect in ("good", "marginal")]


def _conversion_grading(bench: BenchRun) -> List[str]:
    """B6's gate, printed with the run rather than left to a test."""
    rows = [row for row in convertible(bench) if row.passes]
    if not bench.convert or not rows:
        return []
    passing = [row for row in rows if row.passes == "yes"]
    share = len(passing) / len(rows)
    icon = "✅" if share >= CONVERSION_BAR else "❌"
    lines = [
        f"{icon} converted {len(passing)}/{len(rows)} of the images a human called "
        f"good or marginal ({share:.0%}, bar is {CONVERSION_BAR:.0%})"
    ]
    for row in rows:
        if row.passes != "yes":
            lines.append(f"ℹ️  {row.name}  {row.expect}, but still {row.passes}")
    retried = [row for row in rows if (row.tries or 1) > 1]
    if retried:
        lines.append(
            "ℹ️  retried: "
            + ", ".join(f"{row.name} ({row.tries})" for row in retried)
        )
    return lines


def _grading(bench: BenchRun) -> List[str]:
    rows = graded(bench)
    if not rows:
        return []
    wrong = disagreements(bench)
    lines = [
        f"triage agrees with 'expect' on {len(rows) - len(wrong)}/{len(rows)} image(s)"
    ]
    for row in wrong:
        lines.append(f"ℹ️  {row.name}  expected {row.expect}, triage says {row.verdict}")
    return lines


def render_table(bench: BenchRun, color: bool = True) -> str:
    """The corpus, one row per image."""
    name_width = max([len(row.name) for row in bench.rows] + [len("image")])
    category_width = max([len(row.category) for row in bench.rows] + [len("kind")])
    expect_width = max([len(row.expect) for row in bench.rows] + [len("expect")])

    header = (
        f"{'image':<{name_width}}  {'kind':<{category_width}}  {'expect':<{expect_width}}  "
        + "  ".join(f"{metric.header:>{metric.width}}" for metric in METRICS)
    )
    lines = [header, "─" * len(header)]

    for row in bench.rows:
        start = (
            f"{row.name:<{name_width}}  {row.category:<{category_width}}  "
            f"{row.expect:<{expect_width}}  "
        )
        if row.unmeasured:
            lines.append(start + f"{'not measured — ' + row.unmeasured}")
            continue
        lines.append(
            start
            + "  ".join(
                f"{_cell(metric.key, row.value(metric.key)):>{metric.width}}"
                for metric in METRICS
            )
        )

    lines.append("─" * len(header))
    averages = bench.averages()
    if averages:
        pad = name_width + category_width + expect_width + 6
        lines.append(
            " " * 0
            + f"{'mean':<{pad}}"
            + "  ".join(
                f"{(f'{averages[m.key]:.3f}' if m.key in averages else ''):>{m.width}}"
                for m in METRICS
            )
        )

    for row in bench.rows:
        for note in row.notes:
            lines.append(f"ℹ️  {row.name}  {note}")

    summary = f"{len(bench.measured)} image(s) measured"
    if bench.unmeasured:
        summary += f", {len(bench.unmeasured)} not measurable here"
    resolution = (
        f"forced to {bench.work_side}px"
        if bench.work_side
        else "resolution per profile (see res)"
    )
    traced = (
        f"traced with {bench.tracer} {bench.tracer_version}".rstrip()
        + f", layers grown {bench.overlap}px"
        + (
            ", converted with retries (B6)"
            if bench.convert
            else ", cleaned up (B5)" if bench.cleanup else ", as the tracer left it"
        )
        if bench.tracer
        else "no tracer here"
    )
    prepared = "preprocessed (B3)" if bench.preprocess else "as handed in"
    aimed = f"  ·  aimed at {bench.profile}" if bench.profile else ""
    lines.append(f"{summary}  ·  {resolution}  ·  {prepared}  ·  {traced}{aimed}")
    lines.extend(_grading(bench))
    lines.extend(_conversion_grading(bench))
    if bench.unmeasured:
        lines.append(
            "ℹ️  A format this machine cannot decode is a row you don't get, not a "
            "failed run."
        )
    text = "\n".join(lines)
    if color:
        return text
    return (
        text.replace("ℹ️", "[note]").replace("✅", "[ok]").replace("❌", "[fail]")
    )


def render_changes(changes: Sequence[Change], color: bool = True) -> str:
    """What moved since the baseline, worst first."""
    if not changes:
        return "✅ no change against the baseline."

    order = {"worse": 0, "changed": 1, "better": 2}
    icons = {"worse": "❌", "better": "✅", "changed": "•"}
    counts = {"worse": 0, "better": 0, "changed": 0}
    for change in changes:
        counts[change.verdict] += 1

    lines = [
        f"{counts['better']} better, {counts['worse']} worse, "
        f"{counts['changed']} changed with no direction:",
    ]
    for change in sorted(changes, key=lambda c: (order[c.verdict], c.name, c.metric)):
        lines.append(f"  {icons[change.verdict]} {change}")
    text = "\n".join(lines)
    if color:
        return text
    return text.replace("✅", "[ok]").replace("❌", "[worse]").replace("•", "[note]")


def compare_tracers(
    entries: Sequence[CorpusEntry],
    work_side: Optional[int] = None,
    corpus: str = "",
    backends: Optional[Sequence[TracerBackend]] = None,
    preprocess: bool = False,
    overlap: int = DEFAULT_OVERLAP,
    cleanup: bool = False,
) -> List[BenchRun]:
    """One full sweep per installed tracer — the instrument B0's decision rests on."""
    from .tracer import available_backends

    chosen = list(backends if backends is not None else available_backends())
    return [
        run(
            entries,
            work_side=work_side,
            corpus=corpus,
            backend=backend,
            preprocess=preprocess,
            overlap=overlap,
            cleanup=cleanup,
        )
        for backend in chosen
    ]


def _totals(bench: BenchRun, key: str) -> Optional[int]:
    values = [row.value(key) for row in bench.measured if row.value(key) is not None]
    return sum(values) if values else None


def render_tracer_comparison(runs: Sequence[BenchRun], color: bool = True) -> str:
    """Every tracer over the same corpus, and what each one costs.

    Deliberately four numbers and not one: a tracer that draws fewer paths has
    either simplified the artwork or thrown it away, and only ``fit`` tells you
    which. Reading ``paths`` without ``fit`` is how you end up choosing the
    tracer that erases the design.
    """
    if not runs:
        return f"no tracer installed here, so there is nothing to compare — {TRACER_INSTALL_HINT}"

    lines = [
        f"{'tracer':<16}{'images':>7}{'paths':>8}{'nodes':>8}{'fit':>8}{'gaps':>8}"
        f"{'seconds':>9}",
        "─" * 64,
    ]
    for bench in runs:
        fits = [row.fit for row in bench.measured if row.fit is not None]
        mean_fit = f"{sum(fits) / len(fits):.3f}" if fits else "·"
        seams = [row.gaps for row in bench.measured if row.gaps is not None]
        mean_gaps = f"{sum(seams) / len(seams):.3f}" if seams else "·"
        spent = sum(
            row.trace_seconds for row in bench.measured if row.trace_seconds is not None
        )
        label = f"{bench.tracer} {bench.tracer_version}".rstrip()
        lines.append(
            f"{label:<16}{len(bench.measured):>7}"
            f"{_totals(bench, 'paths') or 0:>8}{_totals(bench, 'nodes') or 0:>8}"
            f"{mean_fit:>8}{mean_gaps:>8}{spent:>9.2f}"
        )

    names = [bench.tracer for bench in runs]
    width = max([len(row.name) for row in runs[0].rows] + [len("fit by image")])
    lines += ["", f"{'fit by image':<{width}}" + "".join(f"{n:>10}" for n in names)]
    for index, row in enumerate(runs[0].rows):
        cells = []
        for bench in runs:
            value = bench.rows[index].fit
            cells.append(f"{value:>10.3f}" if value is not None else f"{'·':>10}")
        lines.append(f"{row.name:<{width}}" + "".join(cells))

    text = "\n".join(lines)
    return text if color else text


def render_metric_help() -> str:
    """What each column means — printed by ``--explain``."""
    lines = ["What the columns measure:"]
    for metric in METRICS:
        direction = {
            "lower": " (lower is better)",
            "higher": " (higher is better)",
            "": "",
        }[metric.better]
        lines.append(f"  {metric.header:<8} {metric.about}{direction}")
    return "\n".join(lines)


def render_json(bench: BenchRun, changes: Optional[Sequence[Change]] = None) -> str:
    payload: Dict[str, Any] = bench.to_dict()
    if changes is not None:
        payload["changes"] = [asdict(change) for change in changes]
    return json.dumps(payload, indent=2, ensure_ascii=False)
