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
| **The core stays dependency-free** (stdlib + PyYAML) | It runs in Termux with no toolchain. Heavier work goes in optional extras (`[render]`, `[geometry]`, `[convert]`) and is detected at runtime — see the decision below. Installing the converter must never be required to run the checker. |
| **The profile is the spec** | Rules already encode what a shop wants. Fixers and the converter read their targets from the *same* profile — max colours becomes the quantiser's `k`, min stroke width becomes a morphology kernel, canvas size becomes output dimensions. No second source of truth. |
| **Never destroy input** | Fixes write to a new file by default. `--in-place` requires an explicit flag and keeps a `.bak`. |
| **Every step ends at a gate** | Each step below has a *done when* that can be checked by running something, not by opinion. If a gate fails, we stop and fix it rather than stacking the next step on top. |

---

## Decision: one codebase, not a mobile and a desktop edition

**Asked early, on purpose.** The question was whether keeping this runnable on a
phone would hold back the desktop features. Answer: **it doesn't, so there is no
fork.** One codebase, capabilities detected at runtime, and heavy work offloaded
over the network when a device can't do it locally.

The evidence, measured rather than assumed (`svgemb doctor` prints it per
machine):

| Tier | Needs | On a phone (Termux)? | If missing |
| --- | --- | --- | --- |
| **Core** — check, write, round-trip, fix, web UI | stdlib + PyYAML | Yes, always | n/a |
| **Rendering** (A1) — visual regression, previews | any of `rsvg-convert`, `resvg`, Inkscape, `cairosvg` | Yes — `pkg install librsvg` | comparisons are skipped, nothing fails |
| **Raster** (B3+) — image → SVG | Pillow + potrace | Yes — `pkg install python-pillow potrace` | conversion unavailable |
| **Path geometry** (A5) — offsetting, booleans | shapely or pyclipper | **Maybe** — needs GEOS and a compiler; the one fragile tier | stroke→outline and min-feature-size unavailable |

So only *one* tier is genuinely at risk on mobile, and only for two features.
That is nowhere near enough to justify maintaining two versions of a checker
that has no mobile problem at all.

**Why not fork.** A fork splits along *dependencies*, but the features are the
same on both sides. We would duplicate the entire checker — the part that runs
happily anywhere — to solve a problem confined to two later steps. Two codebases
means divergent bugs and a mobile edition that quietly rots.

**What we do instead.**

1. Every heavy capability is optional and detected at runtime. A capability you
   don't have is a *measurement you don't get*, never a crash — the pattern A1
   established, where a missing renderer returns `None` and callers skip.
2. `svgemb doctor` reports what this machine can do and exactly what to install
   for the rest, with per-platform hints (Termux included). It defers to the
   modules themselves, so it cannot claim a capability the code won't use.
3. **The mobile/desktop split is a runtime split, not a code split.** When a
   phone can't do the heavy step, run `svgemb serve --host 0.0.0.0` on a desktop
   and use it from the phone's browser. That already works — the phone becomes a
   client, and no second codebase exists to maintain.

`SVGEMB_NO_RENDERER=1` forces the degraded path on any machine, so CI proves the
bare-install experience instead of assuming it.

**Revisit if:** Phase B needs something with no Android build at all (an ML
background remover, say). Even then the answer is not a fork — that feature is
server-only and the UI reports it as unavailable.

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

~~Still open: the rendered-image comparison.~~ **Closed by A1**: with
`rsvg-convert` installed, all six corpus files are now render-verified as well
as model-verified.

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

### A1. Visual regression harness · ✅ CLEARED

Lives in `visual.py`. Rendering is delegated to whatever is installed
(`rsvg-convert` preferred, then `resvg`, Inkscape, `cairosvg`); **decoding and
comparison are pure Python** — PNG is zlib plus five filter types, which is
little enough code to keep the harness free of compiled dependencies and
working on a phone. The decoder is verified byte-for-byte against Pillow where
Pillow happens to be installed.

Two details that matter more than they look:

- **Compositing before comparing is not cosmetic.** Renderers leave arbitrary
  colour values in fully transparent pixels, so comparing raw RGBA reports
  differences in parts of the image nobody can see. Both sides are flattened
  onto white first.
- **Both sides always go through the same renderer.** Mixing two would compare
  their antialiasing rather than the documents.

Verified: identical input scores zero; a colour change scores 11.7% of pixels;
**reformatting scores zero** — the property every fixer depends on. All three
hold across every installed renderer.

This also closed A0's open half: the corpus now round-trips **6/6
render-verified**, not merely model-verified.

`svgemb doctor` reports renderer availability, and `SVGEMB_NO_RENDERER=1` forces
the degraded path — the suite passes either way (168 with a renderer; 162 plus 6
skips without), and the core was separately proved to run with every optional
dependency blocked.

<details>
<summary>Original plan for this step</summary>

**Do:** render SVG → PNG via `resvg` (single static binary, no Python build) or
`rsvg-convert`, with `cairosvg` as fallback. Compare images, return a difference
score. Skip cleanly (don't fail) when no renderer is installed.

**Done when:** `compare(a, a) == 0`, a known-different pair scores above
threshold, and the suite still passes on a machine with no renderer at all.

</details>

### A2. The fix protocol · ✅ CLEARED

Rules stayed pure. A `Fixer` is a separate object registered against a rule id,
built from the **configured rule instance** — so a fixer repairs to exactly the
numbers the profile checks, rather than to a second copy of them. The ground
rule "the profile is the spec", made structural.

| Risk | Meaning | Default |
| --- | --- | --- |
| `SAFE` | Cannot change the rendered image | applied |
| `LOSSY` | Changes the image on purpose | needs `--allow lossy` |
| `DESTRUCTIVE` | May change what the designer meant | asked for explicitly |

**The engine checks these claims instead of trusting them.** After a run it
re-checks the document and renders before/after: a `SAFE` fix that moves a
pixel, or any fix that introduces a new error, is reported as a failed run and
the output is not offered for writing. That is what A1 was built for, and it is
covered by a test with a deliberately lying fixer.

`verify_fixer()` holds a fixer to the four invariants — fixes the target, no
regressions, idempotent, within its visual budget — and is **public rather than
test-only**, so fixers added later (including shop-specific ones) can be held to
the same bar. `verify_no_op()` is the floor beneath it all: a run that fixes
nothing returns the file byte for byte, verified across the whole corpus.

Two things worth recording:

- **Fixers re-parse between runs.** The document model caches resolved styles
  and geometry, so without re-parsing the second fixer in a run would read a
  stale view of what the first one changed. Verified by a test that watches one
  fixer observe another's output.
- **Idempotence is "running the engine again changes nothing further".** The
  case it really catches is a fixer that never satisfies its own rule and so
  gets re-applied forever, churning the file each pass.

One reference fixer ships with it — `geometry.require_viewbox`, which adds the
viewBox the renderer already assumed. End to end it produces a **one-line diff**
(A0's verbatim spans) and **zero pixels changed** (A1's harness). The batch of
safe fixes is A3; the `svgemb fix` command is A6. Until then `svgemb rules`
marks which rules are fixable and at what risk.

<details>
<summary>Original plan for this step</summary>

Keep rules pure. A `Fixer` is a separate object registered against a rule id,
so a rule that can't be fixed simply has none.

**Done when:** a no-op fixer round-trips a file unchanged, and risk gating is
covered by tests.

</details>

### A3. Safe fixes · ✅ CLEARED

Five fixers, every one appearance-preserving and every one held to
`verify_fixer()`:

| Rule | Fix |
| --- | --- |
| `geometry.canvas_size` | scale width/height into the allowed range |
| `geometry.require_viewbox` | add the viewBox implied by width/height |
| `color.no_gradients` | delete gradient/pattern definitions nothing references |
| `element.forbidden` | remove forbidden elements that draw nothing |
| `document.no_editor_metadata` | strip editor state, keeping layer annotations |

**The recurring trick: only remove what was never drawing.** An unreferenced
`<linearGradient>` in `<defs>`, a `<filter>` nothing points at, a hidden
`<image>` — all invisible, so deleting them cannot change the picture, and it is
frequently the *only* reason the file tripped the rule. Anything actually on the
canvas is left alone with a reason: *"`<text>` are part of the artwork; removing
them would delete visible content, so that is a manual decision."* Replacing a
live gradient with flat colour is a real change to the design — that is A4.

**Canvas scaling is safe because the viewBox stays put.** Only width and height
move, so every coordinate inside is untouched and the design is simply presented
at a different physical size — zero pixels differ. A file with no viewBox gets
the implied one first; without that, changing width/height would enlarge the
canvas *around* the artwork instead of scaling it, which is a visible change and
not what anyone means by "make it bigger".

Two things the batch forced, both worth having:

- **Per-fix rollback in the engine.** Scaling a design down makes its strokes
  physically thinner, which can push them under the minimum width. Each fix is
  now re-checked on its own and reverted if it would introduce errors, instead
  of one bad fix failing the whole run. The report says
  `rolled back: it would introduce errors in stroke.min_width`.
- **Surgical attribute removal in the writer.** Stripping two `inkscape:*`
  attributes counted as "the root was edited", so a fourteen-line Inkscape
  `<svg>` tag collapsed onto one line — a wall of diff for a two-line change.
  When the only change to an element is attributes being *removed*, the writer
  now cuts them out of the original tag text and keeps the author's layout.

**New rule: `document.no_editor_metadata`** (warning, in `embroidery-basic`).
Editor state does not render, bloats the upload, and `sodipodi:docname` carries
the file name from the machine it was drawn on. `inkscape:groupmode` and
`inkscape:label` are deliberately kept — `structure.color_layers` reads them to
find layers, so stripping them would change a verdict.

**Not done, with reasons.** *Colour notation normalisation* was on the list but
has no home: colours are already normalised internally for counting, so `white`
and `#FFF` are one colour to every rule, and no rule can fail on notation. A
fixer needs a failing rule to attach to. It would be pure cosmetics, so it is
dropped rather than given a rule invented to justify it.

<details>
<summary>Original plan for this step</summary>

The first real batch — all of them appearance-preserving:

- `geometry.require_viewbox` — derive `viewBox` from width/height
- `geometry.canvas_size` — scale the canvas uniformly into the allowed range
- `color.*` — normalise colour notation
- `element.forbidden: filter` — drop filters and unreferenced `<defs>`
- strip editor metadata (Inkscape/Illustrator private namespaces)

**Done when:** for each fixer, the target rule fails before and passes after;
no other rule's error count increases; `fix(fix(x)) == fix(x)`; visual
difference is zero. ~~Make these four assertions a shared test helper~~ — A2
shipped it as `fixes.verify_fixer()`; every fixer here just calls it.

</details>

### A4. Lossy fixes · ✅ CLEARED

Four repairs that change the picture on purpose. None of them run without
`--allow lossy`.

| Rule | Fix | Budget |
| --- | --- | --- |
| `color.max_count` | merge near-duplicate colours down to the limit | unbounded |
| `color.allowed_palette` | snap colours to the nearest stocked thread | unbounded |
| `color.no_transparency` | composite semi-transparent paint onto the page | 10% |
| `path.closed` | close stroked contours whose ends nearly meet | 2% |

**Every fix declares a visual budget**, so "lossy" never means "anything goes".
The engine holds each one to the budget it declared rather than to a single
global number — gap-closing that repaints 30% of the image is a bug, while
recolouring that repaints 30% is the job. Safe fixes declare `0.0`, which is the
same check as before, so the special case for `SAFE` disappeared.

**Colours are merged in CIE Lab, and survivors are always real colours from the
design.** Averaging a cluster would invent shades no thread matches. Merging is
agglomerative rather than k-means: no random seeding, so the same file always
produces the same palette — which is what makes the fix idempotent. Every
substitution is reported: `#141414 -> #111111 (1 place(s))`.

**Transparency flattening is exact where it matters.** 50% red on a white page
*is* `#ff8080`, so the flattened file renders identically — verified. The
approximation is bounded and stated: it composites against the page, not against
whatever shape sits underneath, so overlaps drift, which is what the 10% budget
is for. Group opacity is declined outright, because flattening it correctly
means pushing it into every descendant.

**One rule turned out to need two fixes**, which the registry did not allow:

- an *unstroked* open path is already rendered closed — SVG fills treat every
  subpath as closed — so writing the `Z` moves **zero pixels**. That is a safe
  fix, and it now lands without `--allow lossy`.
- a *stroked* open path would gain a visible closing segment, so that one is
  lossy and only closes gaps under 1 mm. A wider gap means the shape was never
  meant to close, and inventing an edge across it would be making up artwork.

So the registry now holds a list per rule, safest first, and the engine tries
the repair that cannot hurt before the one that can. `verify_fixer(..., risk=)`
picks which to hold to the contract.

<details>
<summary>Original plan for this step</summary>

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

</details>

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

Do not start Phase B until: ~~A0 round-trips cleanly~~ ✅, ~~A1 can measure
visual difference~~ ✅, A3 fixers hold their four invariants, and A5 has a
working geometry layer. Phase B leans on all four — starting early means debugging a tracer and a
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
