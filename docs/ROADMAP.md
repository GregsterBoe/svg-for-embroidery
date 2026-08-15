# Roadmap: from checker → fixer → image converter

Where we are today (v0.1): a checker. It parses an SVG, resolves geometry and
styles, runs a profile's rules and reports findings. It never modifies anything.

Where we are going:

1. **Phase A — Auto-fix.** Opt in to having the tool repair what it flags.
2. **Phase B — Image → SVG.** Feed it a PNG/JPG, get an embroidery-ready SVG.

Phase B is the hard one, so it is split into steps that each produce something
measurable. The order matters: Phase A builds the two things Phase B depends on
(a safe way to write SVGs, and a way to measure whether output is any good).

---

## Ground rules

These hold for every step below. They exist so we don't paint ourselves into a
corner.

| Rule | Why |
| --- | --- |
| **The core stays dependency-free** (stdlib + PyYAML) | It runs in Termux with no toolchain. Fixing and converting get *optional* extras: `pip install svg-for-embroidery[fix]`, `[convert]`. Installing the converter must never be required to run the checker. |
| **The profile is the spec** | Rules already encode what a shop wants. Fixers and the converter read their targets from the *same* profile — max colours becomes the quantiser's `k`, min stroke width becomes a morphology kernel, canvas size becomes output dimensions. No second source of truth. |
| **Never destroy input** | Fixes write to a new file by default. `--in-place` requires an explicit flag and keeps a `.bak`. |
| **Every step ends at a gate** | Each step below has a *done when* that can be checked by running something, not by opinion. If a gate fails, we stop and fix it rather than stacking the next step on top. |

---

## Phase A — Automatic fixes

### A0. Round-trip fidelity — **the go/no-go gate** · ✅ CLEARED

**Outcome: ElementTree is good enough. The text-patching fallback is not needed** —
but only with a verbatim-span layer on top, which turned out to be the real
deliverable.

Straight ElementTree serialisation cleared the semantic bar (identical document
model, identical checker verdict, stable output) but rewrote every start tag:
Inkscape's multi-line root element collapsed to one line, namespace declarations
hoisted, quote styles changed. Semantically null, but it would have made every
fixer's diff span the whole file — the churn this gate exists to prevent.

So `document.scan_start_tags` records the source span of every start tag and
pairs it with the parsed tree in document order. An element nobody edited is
copied byte for byte; only edited elements get rebuilt. If the pairing ever
fails to line up, the index is discarded and the writer reformats instead of
guessing.

Results on the corpus (`svgemb roundtrip tests/corpus`): 6/6 model-identical,
verdict-identical and stable; 5/6 byte-identical. The sixth differs only in that
`&#233;` in text content comes back as `é` — the parser resolves character
references before we see them.

Two hazards found and closed along the way, both of which would have corrupted
real files:

- Inkscape declares `xmlns` **and** `xmlns:svg` for the same URI, so generated
  end tags came out as `</svg:metadata>` against a `<metadata>` start tag —
  invalid XML. End tags are now taken from the source start tag itself.
- A namespace declared on an element that a fixer edits disappears with the
  rebuilt tag. The writer now detects this and hoists declarations to the root.

Files with an internal DTD subset are **refused**, not written: the parser
expands those entities on read, so writing back would silently inline them.

Still open: the rendered-image comparison. No renderer is installed in the
development container, so `render_identical` is `None` everywhere. The hook is
written and skips cleanly; **A1 closes this**, and the gate should be re-run
with a renderer present. The semantic checks above are what it passes on today.

<details>
<summary>Original plan for this step</summary>


Before writing a single fix, prove we can read an SVG and write it back without
damaging it. A fixer that silently corrupts files is worse than no fixer.

Known hazards with `xml.etree.ElementTree`:

- namespaces get rewritten to `ns0:` → fix with `ET.register_namespace("", SVG_NS)`
- comments and processing instructions are dropped → fix with
  `XMLParser(target=TreeBuilder(insert_comments=True, insert_pis=True))`
- the XML declaration and any DOCTYPE are lost → re-emit manually
- attribute quoting/whitespace changes cosmetically → acceptable, but must be
  *proven* cosmetic

**Do:** build a `writer.py`, and a corpus of real-world files — exports from
Inkscape, Illustrator, Figma, Affinity, plus a hand-written one.

**Done when:** for every corpus file, `parse → write → parse` produces an
identical document model, and a rendered PNG of input and output are
pixel-identical. If ElementTree can't clear this bar, fall back to *surgical
text edits* on the original source (track source offsets, patch attributes in
place) — decide here, not later.

</details>

> **Note on the corpus.** The committed fixtures are modelled on real exporter
> output (Inkscape's `sodipodi:namedview` and RDF metadata, Illustrator's
> DOCTYPE and `.st0` classes, Figma's flat `clipPath` defs), not captured from
> those applications. Dropping genuine exports into `tests/corpus/` picks them
> up automatically, and `svgemb roundtrip <file>` runs the same gate over any
> file — worth doing with real designs before A3 lands.

### A1. Visual regression harness · size M

The tool that answers "did this change how the design looks?" — needed by every
fixer, and again by the whole of Phase B.

**Do:** render SVG → PNG via `resvg` (single static binary, no Python build) or
`rsvg-convert`, with `cairosvg` as fallback. Compare images, return a difference
score. Skip cleanly (don't fail) when no renderer is installed.

**Done when:** `compare(a, a) == 0`, a known-different pair scores above
threshold, and the suite still passes on a machine with no renderer at all.

### A2. The fix protocol · size M

Keep rules pure. A `Fixer` is a separate object registered against a rule id,
so a rule that can't be fixed simply has none.

```python
@register_fixer("geometry.require_viewbox")
class AddViewBox(Fixer):
    risk = Risk.SAFE
    def apply(self, doc) -> FixOutcome: ...
```

Three risk levels, because "fix it" means different things:

| Risk | Meaning | Example |
| --- | --- | --- |
| `SAFE` | Cannot change appearance | add `viewBox`, normalise `#FFF`→`#ffffff`, drop unused `<defs>` |
| `LOSSY` | Changes appearance predictably, on purpose | quantise 5 colours down to 3, flatten opacity |
| `DESTRUCTIVE` | May change design intent | regroup layers (z-order), close a path with a large gap |

Default is `SAFE` only; the rest are opt-in per run or per profile.

**Done when:** a no-op fixer round-trips a file unchanged, and risk gating is
covered by tests.

### A3. Safe fixes · size M

The first real batch — all of them appearance-preserving:

- `geometry.require_viewbox` — derive `viewBox` from width/height
- `geometry.canvas_size` — scale the canvas uniformly into the allowed range
  (change `width`/`height`, keep `viewBox`; the artwork is untouched)
- `color.*` — normalise colour notation
- `element.forbidden: filter` — drop filters and unreferenced `<defs>`
- strip editor metadata (Inkscape/Illustrator private namespaces)

**Done when:** for each fixer, the target rule fails before and passes after;
no other rule's error count increases; `fix(fix(x)) == fix(x)`; visual
difference is zero. Make these four assertions a shared test helper — every
later fixer reuses it.

### A4. Lossy fixes · size L

Now appearance changes, deliberately.

- `color.max_count` / `color.allowed_palette` — cluster colours in **Lab** space
  (not RGB — RGB distance doesn't match human perception) and remap to the
  nearest allowed colour. Report the mapping so the user can see what happened.
- `color.no_transparency` — composite semi-transparent fills against what is
  behind them, or flatten to opaque.
- `path.closed` — append `Z` to open subpaths, but **only** when the gap is
  below a threshold; a large gap means the shape was never meant to be closed,
  so report instead of guessing.

**Done when:** the visual difference stays under an agreed budget, and every
colour remap is reported.

### A5. Geometry-dependent fixes · size L — **shared with Phase B**

These need real path maths, which is also what Phase B needs. Build the geometry
layer once, here.

- `stroke.min_width` — widen thin strokes; better, convert stroke→filled outline
  (needs path offsetting)
- **minimum feature size** — the check the README currently lists as *not
  implemented*. Once we can offset paths, we can finally detect details too thin
  to stitch, as a check *and* as a fix.

**Do:** pick the geometry stack once — `svgpathtools` (pure Python) for parsing
and flattening, `pyclipper` or `shapely` for offsetting and booleans. Both
compiled ones go in the optional extra, keeping the core Termux-safe.

**Done when:** offsetting a known shape matches a hand-computed result, and the
new min-feature check finds the defect in a purpose-built test file.

### A6. Surfacing it · size S

- CLI: `svgemb fix design.svg -p myshop -o fixed.svg`, `--dry-run` (show a diff
  and the risk of each change), `--allow lossy`, `--only <rule.id>`, `--in-place`
- Always re-check after fixing and print the before/after verdict
- Web UI: a **Fix what can be fixed** button, before/after preview, download

**Done when:** `svgemb fix examples/bad-design.svg` turns a failing file into a
passing one, or explains precisely why it can't.

### Explicitly *not* in Phase A

**Text → paths.** Correct conversion means loading the font, extracting glyph
outlines, applying kerning and text layout. That's a font-engineering project
(`fonttools` at minimum) and it will be subtly wrong for years. Instead: detect
Inkscape on `PATH` and shell out to `inkscape --export-text-to-path`, otherwise
report it as a manual step. Revisit only if it becomes the top complaint.

---

## Gate between the phases

Do not start Phase B until: A0 round-trips cleanly, A1 can measure visual
difference, A3 fixers hold their four invariants, and A5 has a working geometry
layer. Phase B leans on all four — starting early means debugging a tracer and a
broken writer at the same time.

---

## Phase B — Images → embroidery-ready SVG

The insight that shapes this phase: **quality comes from preprocessing, not from
the tracer.** A great tracer on a bad mask gives a bad SVG. So most of the work
below happens *before* anything is vectorised.

### B0. Buy, don't build · size S

Do not write a tracer — that's a research project. Evaluate existing ones on our
own corpus:

| Candidate | Notes |
| --- | --- |
| **potrace** | Best-in-class for single-colour bitmaps. Binary, `pkg install potrace` in Termux. Traces one mask at a time — which is exactly the shape of our problem. |
| **VTracer** | Colour tracing out of the box, fast. Rust; check for an Android wheel before committing. |
| **autotrace** | Older, more artefacts. Fallback. |

**Done when:** a written spike comparing output on the corpus, with a decision
and its reasoning recorded. Recommendation to beat: potrace, per colour layer.

### B1. Corpus and metrics — **do this first** · size M

Without objective measurement, every later tweak is guesswork.

**Do:** assemble ~20 images spanning the real range — flat logos, line art,
scanned drawings, photos with backgrounds, transparent PNGs, gradients,
low-resolution junk. For each, define the target profile and record: rendered
difference vs. the quantised source, path count, colour count, and whether the
result passes the profile.

**Done when:** `make bench` prints a table of all metrics for the whole corpus,
and re-running it after a change shows what got better and what got worse.

### B2. Suitability triage · size S

Not every image can become embroidery, and saying so early is a feature.

**Do:** `svgemb assess photo.jpg -p embroidery-basic` — measure colour count,
gradient smoothness, edge density, finest stroke; report *"this photo has 38,000
colours and soft shading; at 3 colours expect heavy detail loss."*

**Done when:** it correctly separates the corpus into good/marginal/hopeless.

### B3. Preprocessing pipeline · size L

The heart of the phase. Each stage independently testable:

1. load, honour EXIF rotation, upscale small inputs
2. background handling — alpha channel, or flood-fill from the corners
3. denoise (bilateral filter: smooths noise, keeps edges)
4. contrast normalisation
5. **colour quantisation to `k` colours in Lab space** — where `k` comes from
   the profile's `color.max_count`
6. **morphological open/close** — removes specks and bridges thinner than the
   profile's `stroke.min_width`, i.e. exactly the detail a needle can't render

**Done when:** each stage has a before/after test on a fixture, and B1's metrics
improve measurably versus tracing the raw image.

### B4. Layered tracing · size M

Split the quantised image into one binary mask per colour and trace each
separately. This is the elegant part: **the "one colour per layer" requirement
falls straight out of the method** — each traced mask becomes its own `<g>`, so
`structure.color_layers` passes by construction rather than by repair.

Handle overlaps deliberately: trace darkest-last so light backgrounds sit
underneath, and grow each mask by a hair to avoid hairline gaps between
neighbouring colours.

**Done when:** a three-colour logo produces three layers, each single-coloured,
with no visible seams.

### B5. Vector cleanup · size M

Reuse Phase A's machinery on the tracer's raw output:

- simplify curves until `path.max_count` is satisfied
- drop shapes below minimum stitchable area
- close all subpaths, set canvas size in cm, assemble the layer structure

**Done when:** the tracer's output passes the target profile after cleanup.

### B6. Close the loop · size M

```bash
svgemb convert photo.jpg -p embroidery-basic -o design.svg
```

convert → check → fix → re-check, and when it still fails, **adjust and retry
automatically**: too many colours → lower `k`; too many paths → simplify harder;
details too fine → larger morphology kernel. Bounded retries, and a report of
what it changed and why.

**Done when:** ≥80% of the corpus's "good" and "marginal" images convert to a
passing SVG unattended, with the metrics to prove it.

### B7. In the UI · size M

Upload an image on the phone → side-by-side preview → sliders for colours and
detail → live re-check → download the SVG.

**Done when:** the full journey works on a phone, offline.

---

## Sequencing

```
A0 round-trip ──┬── A2 protocol ── A3 safe ── A4 lossy ──┐
A1 visual diff ─┘                                        ├── A6 CLI/UI
                    A5 geometry ─────────────────────────┘
                         │
                    ═══ GATE ═══
                         │
B1 corpus+metrics ──┬── B3 preprocessing ── B4 tracing ── B5 cleanup ── B6 loop ── B7 UI
B0 tracer spike ────┤
B2 triage ──────────┘
```

A0/A1 and B0/B1/B2 are independent and can run in parallel. Everything else is
a chain.

## Risks, named up front

| Risk | Mitigation |
| --- | --- |
| ElementTree can't round-trip real files | A0 is a gate, and the text-patching fallback is chosen there — not discovered in A4 |
| Compiled dependencies break the Termux story | Everything heavy goes in optional extras; core stays stdlib+PyYAML; CI installs the bare core and runs the checker suite |
| "Fixed" files look wrong | Risk levels, `SAFE` by default, visual diff budget enforced in tests |
| Photos make bad embroidery, users blame the tool | B2 triage sets expectations *before* conversion |
| Tuning turns into endless guesswork | B1's metric table lands before any tuning work |
| Auto-fix quietly changes design intent | Every fix is reported; `--dry-run` shows the diff; nothing overwrites without a flag |
