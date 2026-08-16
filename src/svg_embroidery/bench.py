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

**What is measured today, and what is not.** There is no tracer yet (B0 picks
one, B4 wires it in), so the columns describing an SVG — path count, whether the
result passes its profile — are empty and print as ``·``. They are declared here
anyway, with their direction, so that when B4 lands the baseline diff grades
them from the first run instead of needing the harness changed. The columns that
*are* filled describe the image and the colour reduction, which is where the
roadmap says the quality actually comes from.

**A measurement you can't take is not a failure.** An image this machine cannot
decode — a JPEG with no Pillow — is reported as unmeasured with the reason,
the run continues, and the summary says how many rows are missing. Same rule as
everywhere else in the project.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
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
from .visual import compare_rasters

#: Where the corpus lives when nothing else is said. Kept out of ``tests/`` on
#: purpose: it is a measuring instrument that happens to be checked in, not a
#: fixture directory, and it is meant to grow with real images over time.
DEFAULT_CORPUS = Path(__file__).resolve().parents[2] / "bench" / "corpus"
DEFAULT_BASELINE = Path(__file__).resolve().parents[2] / "bench" / "baseline.json"

#: Ratio metrics move in the last decimal on any float change. Only call a cell
#: different when it moved by more than this, or every run is a "regression".
RATIO_EPSILON = 0.0005

BASELINE_VERSION = 1


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
    Metric("paths", "paths", 6, "int", "lower", "paths in the traced SVG (B4)"),
    Metric("passes", "passes", 6, "text", "", "does the converted SVG pass its profile (B6)"),
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
    alpha: Optional[bool] = None
    flat: Optional[float] = None
    quant: Optional[float] = None
    edges: Optional[float] = None
    thin: Optional[float] = None
    paths: Optional[int] = None
    passes: Optional[str] = None
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


def measure(entry: CorpusEntry, work_side: Optional[int] = None) -> Measurement:
    """Everything B1 can say about one image."""
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

    row.size = f"{source.width}x{source.height}"
    row.alpha = has_alpha(source)

    scale = scale_for(profile, max(source.width, source.height), work_side)
    work = downsample(source, scale.work_side)
    row.res = max(work.width, work.height)
    row.mm_px = round(scale.mm_per_pixel, 3)
    row.colors = unique_colors(work)
    row.k = color_budget(profile)
    row.flat = flat_ratio(work)

    reduced: Quantisation = quantise(work, row.k)
    row.quant = compare_rasters(work, reduced.raster()).ratio
    row.edges = edge_density(reduced)
    if scale.faithful:
        row.thin = thin_ratio(reduced, scale.radius)
    else:
        row.notes.append(
            f"thin: not measurable — at {scale.mm_per_pixel:.2f} mm/px the smallest "
            f"kernel asks {scale.kernel_mm:.1f} mm, not the profile's {scale.min_mm:.1f} mm"
        )
    return row


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
            "averages": self.averages(),
            "rows": {row.name: row.to_dict() for row in self.rows},
        }


def run(
    entries: Sequence[CorpusEntry], work_side: Optional[int] = None, corpus: str = ""
) -> BenchRun:
    return BenchRun(
        rows=[measure(entry, work_side=work_side) for entry in entries],
        work_side=work_side,
        corpus=corpus,
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
    lines.append(f"{summary}  ·  {resolution}")
    if bench.unmeasured:
        lines.append(
            "ℹ️  A format this machine cannot decode is a row you don't get, not a "
            "failed run."
        )
    text = "\n".join(lines)
    return text if color else text.replace("ℹ️", "[note]")


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
