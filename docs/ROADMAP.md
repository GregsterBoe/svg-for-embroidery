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
| **Never destroy input** | Fixes write to a new file by default — in fact `svgemb fix` writes nothing at all without `-o`, `--in-place` or `--stdout`. `--in-place` requires an explicit flag and keeps a `.bak`. |
| **Every step ends at a gate** | Each step below has a *done when* that can be checked by running something, not by opinion. If a gate fails, we stop and fix it rather than stacking the next step on top. |
| **A measurement you can't take is not a failure** (A1, A5, A7, B1) | A missing renderer, geometry backend or image codec costs you a number, never the run. Added as a ground rule at B1, where it applies to a whole row of the benchmark rather than one check. |
| **A metric that can't answer the question must not print a number** (B1) | Stricter than the rule above and learned the hard way: a thinness test at the wrong resolution silently asks a *different* question and quietly grades good artwork as junk. If the instrument can't answer, the cell stays empty and says why. |

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
| **Reading images** (B1) — measuring a source | nothing for PNG; Pillow for JPEG and the rest | Yes — `pkg install python-pillow` | that format is a row you don't get, with the reason |
| **Tracing** (B0) — image → paths | any of `potrace`, `potracer`, `vtracer` | Yes — `pkg install potrace`, or pip-install potracer with no binary at all | `paths`/`fit` are empty, the rest of the table still fills |
| **Path geometry** (A5) — offsetting, booleans | shapely (A5 picked one stack, not two) | **Maybe** — needs GEOS and a compiler; the one fragile tier | stroke→outline and min-feature-size unavailable |

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
safe fixes is A3; `svgemb fix` drives all of them from A6. `svgemb rules` marks
which rules are fixable and at what risk.

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

### A5. Geometry-dependent fixes · ✅ CLEARED — **shared with Phase B**

Lives in `geometry.py`, and it is split where the dependency is, not where the
topic is:

- **Flattening is pure Python and always available.** Turning `d="M0 0 C…"`
  into points is de Casteljau plus the endpoint-arc formula — the same
  judgement A1 made about decoding PNG. So *measuring* a design never needs a
  compiled dependency, and the one check people trip over most (`stroke.min_width`)
  is repaired by arithmetic that runs on a phone.
- **Offsetting and booleans need a backend**, which is the fragile tier. A1's
  pattern exactly: `default_backend()` returns `None`, callers skip, and the
  file's verdict does not change. A test asserts that last part directly,
  because a checker whose answer depends on what is installed is worse than one
  that measures less.

**Decision: shapely, not pyclipper — and not both.** The work here is not only
offsetting: the thinness test needs areas and validity repair too, and shapely
has all three with floating-point coordinates. pyclipper offsets integers,
which means choosing a scale factor and living with it. Abstracting over two
backends would have doubled the code to hedge a bet nobody asked us to make.
`svgemb doctor` now *probes* the geometry module instead of listing modules, so
it can no longer advertise a library the code will not use — it had been
offering pyclipper.

**No `svgpathtools` either.** The plan named it for parsing and flattening, but
the fix protocol already had a path scanner (A4 wrote one to find where
subpaths end) and flattening is ~120 lines on top of it. Adding a dependency to
avoid that would have put the *pure Python* half of this step behind an install.

**New check: `geometry.min_feature_size`** — the one the README listed as *not
implemented*. A shape is stitchable where a disc the width of a stitch fits
inside it, so shrinking by half that width and growing it back leaves exactly
the part that survives; whatever disappears is the defect. Two failures, said
differently because they mean different things: a shape that vanishes entirely
is a hairline, and one that loses a fraction of itself has a spike or a waist.

**The corner allowance is the part that makes it usable.** A disc cannot reach
into a corner, so opening a perfectly solid square still rounds all four off —
`r²(4−π)/4` each. A 5 mm square at a 1.5 mm limit therefore loses 1.9% of its
area to nothing but its own corners, and a naive threshold reports every small
rectangle in the file. So the rule computes what the corners were always going
to cost (`Σ r²(tan(θ/2) − θ/2)` over the vertices, which falls off as `θ³` and
so charges a flattened curve nothing) and only the *rest* counts. Real geometry
subtracted, rather than hidden under a fudge factor.

The check is in `embroidery-strict` and `plotter-vinyl`, whose descriptions
already promised "thicker minimum details" and "hair-thin details tear when
weeding" without delivering either. It is deliberately **not** in
`embroidery-basic`: that profile is the common denominator, and its verdict
should not vary with what a machine has installed.

Three fixers, and the batch is grouped by what they *need* rather than by risk —
the axis that matters when two of the three can be unavailable:

| Rule | Fix | Risk | Backend |
| --- | --- | --- | --- |
| `stroke.min_width` | thicken strokes to the minimum | lossy | no |
| `stroke.forbidden` | redraw the stroke as the filled shape it paints | lossy | yes |
| `geometry.min_feature_size` | cut away detail too fine to stitch | **destructive** | yes |

**Stroke → outline came out pixel-identical**, which was not a given: an 80 mm
box stroked at 4 mm becomes exactly the 84 mm square minus the 76 mm one, and
the renderer cannot tell the two files apart. It is still classified lossy —
curves are flattened to 0.02 mm and round joins become polygons, so *some* file
will move a pixel — but the budget is 5% and the measured answer is zero.

**Widening was left as the fix for `stroke.min_width`, not outlining.** The plan
said "widen thin strokes; better, convert stroke→filled outline", and that turns
out to be wrong on inspection: outlining a hairline satisfies the rule by
deleting the stroke, while leaving artwork just as thin as it was. It passes the
check by removing what the check was looking at. Widening is the honest repair
there; outlining is the honest repair for `stroke.forbidden`, which is what that
rule actually asks for. The two compose — outline first, then the new
`min_feature_size` check measures the filled result.

**The first destructive fixer.** Nothing turns a thin thing into a thick one
without inventing shape the designer did not draw, so the only honest repair is
to remove what the needle was going to drop anyway — and to make the user ask
for it by name. The tier had been empty since A2 defined it.

Its first version was wrong in an instructive way: the opening *is* the
measurement, so applying it as the repair rounded off every corner in the design
— including the ones the corner allowance had just finished excusing. The fix
changed more than the check had complained about, and handed back right angles
drawn as sixteen-segment arcs. Growing the eroded core back by *twice* the
radius and intersecting with the original repairs that: a corner sits `r√2` from
the core, comfortably inside `2r`, so every boundary that was fine returns
exactly as drawn and only the thin runs stay cut. Straight lines stay straight
lines, and the diff is the shape that was broken.

**Node now carries its full transform matrix**, not just the scale it works out
to. Measuring in millimetres means mapping points through the whole ancestor
chain, and reshaping means mapping the answer back — so `document.py`
accumulates the matrix and `transform_scale` derives from it. Free, because
`det(AB) = det(A)det(B)` makes the old per-element product identical to the new
one; the existing test that a scale is cumulative passes unchanged.

**Done when:** ~~offsetting a known shape matches a hand-computed result~~ ✅
(102² − 98² = 800 mm², to the last decimal), ~~and the new min-feature check
finds the defect in a purpose-built test file~~ ✅ — `examples/thin-detail.svg`
is a file every attribute-level check calls perfect: 12×12 cm, one colour, one
layer, every contour closed, not a stroke in it. The geometry finds a hairline,
a needle spike and a 0.6 mm waist, and leaves the 3 mm square and the disc
alone.

<details>
<summary>Original plan for this step</summary>

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

</details>

> **Still open: narrow gaps.** The opening finds detail too thin; the closing
> (grow, then shrink) finds *gaps* too narrow — two shapes that will bleed into
> each other once they are stitched. Same backend, same shape of code, and this
> was deferred to B4 on the assumption that its seam work would need the same
> operation. ~~It is left there rather than done twice.~~ **It wasn't needed
> there**: B4's seams are a pixel-grid problem, fixed by a pixel-grid dilation
> that needs no backend and runs on a phone, while a gap check on arbitrary
> artwork does need one. The two never shared code, so this is still unwritten
> and it belongs here, next to the opening it mirrors.

### A6. Surfacing it · ✅ CLEARED

Everything A2–A5 built, behind one command and one button. No new fixing
machinery: `FixEngine` already did the work, and A6 is the decision about *what
the user is shown and what they have to say out loud first.*

**The skip list is the product.** The gate file makes the point better than any
argument: `examples/bad-design.svg` has nine errors and **cannot** be made to
pass, because most of them are decisions no tool may make for you — live text
that is part of the artwork, a gradient something still references, a 15 mm gap
in a contour. So the run's job is not "fix everything", it is *account for
everything*. Each rule left failing is printed with the reason it was left:

```
⏭ skipped  color.max_count  [lossy]
       needs --allow lossy
⏭ skipped  element.forbidden  [safe]
       <text> are part of the artwork; removing them would delete visible
       content, so that is a manual decision
⏭ skipped  path.closed  [lossy]
       1 contour(s) have gaps wider than 1 mm; closing them would invent
       artwork, so that is a manual decision
```

Three different kinds of news — a flag you can pass, a decision you have to
make, a limit you can raise — and none of them is "it didn't work".

**A missing package is now visible without reading the source.** This was the
one item A5 left dangling. A rule with no capability behind it reports
`measured=False` and stays INFO, which meant the file passed *and never
mentioned* that a check had not run. Now `Report.unmeasured` pulls those out and
both commands surface them: `fix` prints them in the same list as the skips
(they are the same kind of news, and the most actionable one — an install fixes
it), and plain `check` shows them even without `-v`, with the count broken out
of the verdict line so "9 checks passed" never quietly includes one that didn't
happen. On a bare machine `svgemb fix examples/thin-detail.svg -p
embroidery-strict` prints exactly one line, and it is the `pip install`.

**Three things have to be said out loud.**

- *Where the output goes.* Nothing is written without `-o`, `--in-place` or
  `--stdout`; a bare `svgemb fix` is a preview. The alternative — defaulting to
  in-place — contradicts the project's own ground rule about never destroying
  input, for the sake of saving four characters. `--in-place` itself keeps the
  original as `FILE.bak` (`--no-backup` to opt out), which is the rest of that
  ground rule and was easy to forget until it was written down as a test.
- *Which risk ceiling.* `--allow lossy` is a **ceiling**, not a set: it implies
  safe, because nobody asking for lossy repairs wants the safe ones withheld.
- *Which destructive rule.* `--allow destructive` on its own is refused, with
  the exact flag to type instead. A2's docstring always said destructive fixes
  are "asked for explicitly, per rule"; without this the flag would have meant
  "do anything at all to make this pass", which is not a wish anyone can grant
  responsibly. (A7 widened *naming* to include `--choose rule.id=answer`, which
  is at least as explicit as `--only`, and narrowed the requirement to the rules
  a run can actually reach — with `--only` already set, the field is named.)

**Exit code 3.** `1` means your file still has errors, which is ordinary and
expected. A verification failure is a different event entirely: the engine
caught *its own output* misbehaving, wrote nothing, and the bug is ours. Folding
that into `1` would hide the only exit code that means "stop and report this".

**The web UI** gained `POST /api/fix` alongside `/api/check` (shared body,
profile and error handling; the route split is the only change to the existing
path), a **Fix what can be fixed** button on any failing report, the applied /
skipped / not-measured lists, a before-and-after preview pair, and a download
built from a `Blob` — so the fixed file never touches the server's disk and
never leaves the device. There is also *Keep going from the fixed file*, which
feeds the result back through the checker, since the realistic loop is fix, look,
allow one more level, look again. (A7 added the questions to that page as
buttons, which is also how a destructive repair became reachable there.)

**Two inconsistencies fixed on the way through, both found by looking at output
rather than at code:**

- The engine kept escalating through a rule's fixers after one of them had
  already satisfied it, so a run could report `skipped: needs --allow lossy`
  about a rule it had just finished fixing. It now stops at the first fixer that
  clears the rule — reusing the re-check it was already doing for the regression
  test, so this costs nothing.
- `--no-color` stripped icons from the per-file report but not from the
  multi-file summary, which is where a CI log actually looks. Both go through
  `_strip_icons` now (a pre-existing bug in `check`, not something A6 added).

**Done when** — `svgemb fix examples/bad-design.svg` turns a failing file into a
passing one, ✅ *or explains precisely why it can't*: the six rules it cannot
fix each print a reason, and `--allow lossy` moves the count from 9 errors to 4
without touching one of them. The other half of the bar is the destructive path:
`svgemb fix examples/thin-detail.svg -p embroidery-strict --allow destructive
--only geometry.min_feature_size` takes A5's gate file from ❌ 3 errors to
✅ PASS, and the re-check of the written file agrees.

<details>
<summary>Original plan for this step</summary>

- CLI: `svgemb fix design.svg -p myshop -o fixed.svg`, `--dry-run` (show a diff
  and the risk of each change), `--allow lossy` / `--allow destructive`,
  `--only <rule.id>`, `--in-place`
- Always re-check after fixing and print the before/after verdict
- Report what was *skipped* as prominently as what was fixed: after A5 a skip
  can mean "install the geometry extra", which is actionable and currently only
  visible through the library
- Web UI: a **Fix what can be fixed** button, before/after preview, download

**Done when:** `svgemb fix examples/bad-design.svg` turns a failing file into a
passing one, or explains precisely why it can't.

</details>

### A7. Repairs that ask · ✅ CLEARED

**Added after A6, from use.** A6 was proud of reporting "no automatic fix
available" clearly. Running it on real files showed what that sentence actually
is: a dead end, printed politely. The shop wants one colour per layer and the
artwork has none — something *has* to be regrouped, and the only reason the tool
stopped is that it did not know which way. That is a question, not a limit.

**The option carries the risk, not the fixer.** This is the piece that makes the
rest work. `structure.color_layers` has two honest answers and they are not the
same kind of change:

- *Keep the drawing order* wraps each **run** of same-coloured shapes. A design
  that goes red, blue, red becomes three layers rather than two, which still
  satisfies the rule — it asks that no layer mix colours, not that no colour
  repeat. Nothing is reordered, so it is `safe`, and A1 proves it: the run
  renders before and after and the images are identical.
- *One layer per colour* gives exactly one group per thread, which is what a
  shop's tooling usually wants, by moving shapes past each other. Where two
  colours overlap, the top one changes. `destructive`.

Same question, two answers, two prices. So `Fixer.risk` became the *cheapest way
in* — what the engine gates on before asking — and the chosen `Option.risk` is
what the run is actually held to. A fixer that asks is gated on the answer.

**Nothing blocks on a prompt.** `Decision` is data: the CLI prints it with the
exact command that answers it (`--allow lossy --choose color.no_gradients=...`,
assembled from the cheapest option's risk), `-I` asks at the terminal, and the
web UI renders the options as buttons. A run with no answers is a normal run
that reports open questions — so this stays usable in a script, and the answers
end up written down in one.

**Questions are shown even when no answer is affordable yet.** The first version
skipped them at the default risk level, on the reasonable-sounding grounds that
you should not be asked something you cannot act on. But a plain `svgemb fix` is
exactly where someone needs to *find out* a repair exists. Each option prints its
own price and the answer line names the flag.

**The web UI has no destructive setting, and does not need one.** A switch that
quietly enables "delete artwork" for a whole run is the thing that must not
exist; a button that says what it deletes is *better* than a CLI flag, because
the consequence is written next to it. `FixEngine(ask_first=...)` names that
policy: a risk listed there is reachable by being chosen and unreachable
otherwise, so `geometry.min_feature_size` — destructive and offering no choice —
stays command-line only, while `element.forbidden` can be answered with a tap.

**Four rules ask so far**, chosen as the ones that actually block real uploads:

| Rule | The question | Answers |
| --- | --- | --- |
| `structure.color_layers` | how to separate the colours | keep the drawing order (safe) · one per colour (destructive) |
| `color.no_gradients` | which flat colour replaces the ramp | weighted average · first stop (both lossy) |
| `element.forbidden` | delete artwork the profile forbids | delete it (destructive) |
| `path.closed` | close a gap too wide to be a slip | draw the segment (destructive) |

The gradient average is weighted by the span each stop owns, not by stop count,
so a ramp that is mostly dark averages dark instead of being pulled to the
midpoint by a sliver of highlight. The `<text>` answer says what the *better*
answer would have been — converting to paths, which needs the font and is
therefore Inkscape's job — rather than silently deleting someone's lettering.

**The run now repeats until it settles.** Found by reading output, not code:
flattening a gradient to a flat colour introduced a fourth colour *after*
`color.max_count` had already reduced the palette to three, and the engine never
looked again. The regression guard did not catch it — the error count had not
gone *up* against the original. So `fix_source` sweeps up to `MAX_PASSES` times
and stops when a pass changes nothing; skips and questions come from the last
pass (they describe the file as it now stands) while fixes accumulate across all
of them.

**Done when** — `examples/bad-design.svg`, written to be unfixable and used as
A6's "explain why you can't" gate, **passes** once its four questions are
answered: ❌ 9 errors → ✅ 0. Every answer is held to the same four invariants as
any other fixer, via `verify_fixer(..., choice=...)`.

> **Still open.** `fill.required`, `path.max_count`, `document.no_raster` and
> `geometry.aspect_ratio` still report "no automatic fix available". Each is a
> plausible question — which colour, merge what, trace or drop, pad or crop —
> and the mechanism is now the cheap part.

### Explicitly *not* in Phase A

**Text → paths.** Correct conversion means loading the font, extracting glyph
outlines, applying kerning and text layout. That's a font-engineering project
(`fonttools` at minimum) and it will be subtly wrong for years. Instead: detect
Inkscape on `PATH` and shell out to `inkscape --export-text-to-path`, otherwise
report it as a manual step. Revisit only if it becomes the top complaint.

---

## Gate between the phases

Do not start Phase B until: ~~A0 round-trips cleanly~~ ✅, ~~A1 can measure
visual difference~~ ✅, ~~A3 fixers hold their four invariants~~ ✅ (and every
fixer since, via `verify_fixer()`), and ~~A5 has a working geometry layer~~ ✅.
Phase B leans on all four — starting early means debugging a tracer and a broken
writer at the same time.

**The gate is clear, and Phase A is complete.** Every step A0–A7 is done, so
Phase B starts with a writer that doesn't damage files, a way to measure whether
an image changed, a fix protocol that verifies its own work, a geometry backend,
and a command to drive all of it.

---

## Phase B — Images → embroidery-ready SVG

The insight that shapes this phase: **quality comes from preprocessing, not from
the tracer.** A great tracer on a bad mask gives a bad SVG. So most of the work
below happens *before* anything is vectorised — and, since that claim is only
worth acting on if it can be checked, the measuring instrument (B1) is built
before the thing it measures.

### B0. Buy, don't build · ✅ CLEARED

Do not write a tracer — that's a research project. All three candidates were
wrapped in one interface and run over the whole corpus; the write-up is
[docs/spikes/B0-tracers.md](spikes/B0-tracers.md).

**Gate met.** `svgemb bench --tracers`:

```
tracer           images   paths   nodes     fit  seconds
────────────────────────────────────────────────────────
potrace 1.16         20    6649   72915   0.108     0.47
potracer 0.0.4       20    6791   73883   0.108     5.19
vtracer 0.6.15       20    1472   24337   0.201     0.46
```

**Decision: potrace, one mask per colour** — the recommendation to beat, kept.
potracer is the same algorithm in pure Python and scores identically (within
0.003 per image), so the choice between them is 11× speed and numpy-vs-nothing,
not quality; it stays as the fallback for machines that cannot install a binary.
autotrace was never evaluated: it is not packaged on this distribution at all,
and a tracer nobody ships is not a fallback.

`fit` — how much of the image the traced SVG gets wrong when rendered back — is
the column that decides, and the reason the table has four numbers rather than
one. **A tracer that draws fewer paths has either simplified the artwork or
thrown it away, and only `fit` says which:** read `paths` alone and vtracer wins
four to one.

**Two findings worth carrying forward.**

*vtracer is the best tracer here on scans and the worst on line art*, by a lot
in both directions — 0.030 vs potrace's 0.250 on `scan-clean`, 0.224 vs 0.036 on
`line-art-thin`. It is not a better tracer; it is a tracer with photograph-tuned
preprocessing bolted on. That is the strongest evidence this project has for its
own thesis that **quality comes from preprocessing, not from the tracer**, and it
sizes B3's prize: the gap between 0.250 and 0.030 on a clean scan, without
vtracer's habit of deleting thin strokes.

*B1's `edges` stand-in was honest.* It correlates 0.936 with path count and
**0.991 with node count** on the first real trace — better with nodes, which
makes sense, a boundary pixel being closer to a vertex than to a shape. It stays
in the table: it costs nothing and needs no tracer, so a machine with neither can
still predict what conversion will cost.

**The bug this spike nearly shipped**, because it is the kind that looks like a
result: potrace's writer puts every outline of a mask into one `<path>` while
potracer returns loose curves, and emitting one element each turned every hole
in the artwork into a solid blob — a 3.6× worse score attributed to the wrong
cause. `paths` is now counted as *subpaths of the finished document*, whoever
wrote it, and one layer is always one `<path>`.

### B1. Corpus and metrics — **do this first** · ✅ CLEARED

Without objective measurement, every later tweak is guesswork.

**Gate met.** `make bench` (`svgemb bench`) measures all twenty corpus images
and, against a recorded baseline, reports which numbers got better and which got
worse:

```
1 better, 2 worse, 0 changed with no direction:
  ❌ hatching  edges: 0.500 → 0.864
  ❌ monogram  flat: 0.999 → 0.965
  ✅ logo-two-colour  quant: 0.400 → 0.005
```

Grading requires knowing which way is up, so every column declares a direction
in `METRICS`. `--fail-on-regression` turns that into an exit code for CI.

**The corpus is generated, not collected** (`bench/make_corpus.py`), which buys
three things: byte-identical output on every machine, so a metric that moves is
the code moving; fixtures that can be *read* when a number looks wrong (what
makes `line-art-thin` thin is a constant in that file); and no licence question.
The cost, stated rather than hidden: generated images are cleaner than real
ones, so this proves the metrics discriminate, not that they match human
judgement on real scans. Real images can be dropped in alongside.

| Column | Measures | Better |
| --- | --- | --- |
| `res` / `mm/px` | the resolution the row was measured at, and what a pixel is worth | — |
| `colours` | distinct RGB values in the source | — |
| `flat` | share of pixels identical to all four neighbours — how vector-like it is | higher |
| `quant` | share of pixels changed by reducing to the profile's `k` | lower |
| `edges` | share of pixels on a colour boundary — a stand-in for path count | lower |
| `thin` | share of the image finer than this profile's needle | lower |
| `paths` | subpaths in the traced SVG — shapes the machine has to fill (B0) | lower |
| `nodes` | drawing commands in the traced SVG — where stitch count comes from (B0) | lower |
| `fit` | share of pixels the traced SVG gets wrong against its source (B0) | lower |
| `gaps` | share of the traced SVG with no paint on it at all — the seams between colour layers (B4) | lower |
| `verdict` | triage's good/marginal/hopeless, next to `expect` (B2) | — |
| `passes` | does the converted SVG pass its profile (B6) — declared, still empty | — |

Declaring a column before it can be filled is deliberate: the baseline grades it
from the first run that fills it in, rather than needing the harness changed
later. B0 filled three of the four that way.

**A run is only comparable to a baseline taken the same way.** The working
resolution and the tracer both change every number in the table, so `svgemb
bench` refuses the diff across either — with the reason, and with the tracer's
*version*, since potrace 1.16 and potrace 1.10 need not agree on a curve. Same
rule as the empty cell: a comparison that can't answer the question must not
print an answer. B3's preprocessing and B4's seam overlap joined the list as
they landed, each for the same reason.

**Two findings worth carrying forward.**

*The working resolution has to come from the profile, not from a constant.* At a
fixed 128px the table said `line-art-thick` (5px strokes) was worse than
`line-art-thin` (1px) — because at 0.78 mm/px the smallest kernel asks "thinner
than 2.3 mm?" while the profile said 1.5 mm. The fix is `scale_for()`: pick the
resolution that makes the profile's minimum feature a whole kernel wide, and
when the source is too coarse to answer at all (`lowres-icon`, 3.1 mm/px), leave
the cell empty with the reason. This is where the second ground rule above came
from.

*Flat colours must survive quantisation exactly.* Median cut represents a
cluster by its mean, and a logo's antialiased rim drags that mean a few units
off the brand colour — turning a 2% rim into a 20% "loss" and swamping every
comparison. Where one colour owns most of its cluster it is now kept verbatim;
only where none does (a photograph) is the mean right. Lloyd refinement was
added alongside, because median cut alone cuts *inside* a colour holding more
than half the image and leaves two rarer ones sharing an entry.

**What the corpus says today**, and it is not flattering to the current
pipeline: scans measure like junk. `scan-clean` is a line drawing on paper that
any human would call convertible, and it scores `flat` 0.001 with 84% of the
image "too thin" — because grain means no pixel equals its neighbour and the
quantiser turns speckle into unstitchable detail. That is an accurate
measurement of an *unpreprocessed* image, and it is exactly the gap B3's
denoising stage exists to close. It is pinned by a test that should start
failing when B3 lands.

**Measuring a PNG needs nothing installed** — the pure-Python decoder A1 already
carries doubles as the corpus reader. Pillow adds the other formats; without it
a JPEG is a row you don't get, with the reason printed.

### B2. Suitability triage · ✅ CLEARED

Lives in `triage.py`, behind `svgemb assess`. **It measures nothing.** It reads a
B1 `Measurement` — same resolution, same profile, same numbers `svgemb bench`
prints — and grades it, so the two commands cannot disagree about an image. The
verdict is *the worst thing about the image*, and the report names the reading
that decided, because "hopeless" is only useful next to why.

**Gate met: 19/20 against the corpus's `expect` column**, and the run says so
itself rather than leaving it to a test — `bench` gained a `verdict` column
sitting next to `expect`, plus a grading line and the disagreements:

```
triage agrees with 'expect' on 19/20 image(s)
ℹ️  scan-clean  expected good, triage says marginal
```

**The judgement the step rests on: on a speckled image, colour loss and flatness
are measuring the grain, not the artwork.** Reduce a grainy scan of a two-colour
drawing to three colours and every grain pixel moves — `quant` reports 0.87
about a design that is two flat colours. Left to vote it calls every scan
hopeless. So above `edges >= 0.40` those two readings **abstain and say so**,
which is the project's own rule (*a metric that can't answer the question must
not print a number*) applied to a verdict rather than a cell. The threshold is
the widest margin in the model and the reason it is safe: the three scans
measure 0.62–0.67 and the crosshatch 0.86, while **everything else in the corpus
is below 0.19**.

What *can* still be said under grain is whether anything survives the needle at
all. At `thin >= 0.95` nothing does, anywhere — and denoising cannot create
width, so that verdict survives B3. It is what separates `hatching` (0.993,
hopeless) from the scans (0.78–0.84, marginal), which no amount of threshold
tuning on the other metrics managed to do.

Everything else is three ordinary thresholds, any of which costs a band:
`quant > 0.15`, `thin > 0.02`, `flat < 0.85`. The tight ones are the first two —
`logo-five-colour` at 0.181 against `alpha-logo` at 0.112, and `line-art-thin`
at 0.030 against `line-art-thick` at 0.013 — and they are tight because the
corpus was built to put those pairs either side of a line. That is the corpus
doing its job, not a fudge.

**The one miss is `scan-clean`, and the roadmap predicted it.** A line drawing
on paper: a human says good, triage says marginal, because 84% of it is finer
than the needle and that 84% is grain. B1 had already recorded this as the gap
B3 exists to close. Two things make it acceptable rather than a failed gate: the
error is **one band in the cautious direction**, and the report says the word
*denoising* out loud rather than implying the artwork is at fault. A test pins
it, so B3 landing makes the assertion fail and the exception gets deleted
instead of forgotten.

**What was tried and rejected: doing B3's job here.** The roadmap offered
"either B2's thresholds run after B3's preprocessing, or B2 ships knowing it
under-rates scans". The first option was measured before being dropped — a 3×3
median filter, then a majority vote over the quantised labels, then up to four
passes of it. Denoising does move the numbers a long way (`scan-skewed`'s `thin`
0.782 → 0.176), but **it never separated the scans from the crosshatch**: after
two passes `hatching` sits at 0.131 and `scan-clean` at 0.153, the wrong way
round. A stand-in denoiser good enough to grade on is B3, and half of B3 built
inside the triage step would have handed B3 a baseline to match rather than
beat. So triage grades what it is given, and names denoising as the missing
input.

<details>
<summary>Original plan for this step</summary>

Not every image can become embroidery, and saying so early is a feature.

**Do:** `svgemb assess photo.jpg -p embroidery-basic` — measure colour count,
gradient smoothness, edge density, finest stroke; report *"this photo has 38,000
colours and soft shading; at 3 colours expect heavy detail loss."*

**Done when:** it correctly separates the corpus into good/marginal/hopeless.

**Mostly built already.** B1's `bench.measure()` computes every one of those
numbers, and the corpus manifest carries the human `expect` label to grade
against. What B2 adds is the *verdict* — thresholds turning four ratios into one
word — plus a sentence explaining it, and a single-file entry point. Note the
honest complication B1 turned up: the current metrics score a clean scan as
hopeless (see B1), so either B2's thresholds run after B3's preprocessing, or
B2 ships knowing it under-rates scans and says so.

</details>

> **Deliberately not in the web UI.** B7 is the step that owns the phone
> journey — upload, preview, sliders, download — and triage is one panel of it.
> Adding a lone assess button now would be guessing at that layout twice.

### B3. Preprocessing pipeline · ✅ CLEARED

Lives in `preprocess.py`, behind `svgemb bench --preprocess`. Six stages, all
pure Python — it is pixel arithmetic, so the same judgement A1 made about
decoding PNG applies and the conversion path does not vanish behind a compiler.

**Gate met, and it is the one B0 sized.** `svgemb bench --tracers --preprocess`
against the same command without it:

| | potrace `fit` raw | preprocessed | vtracer, for scale |
| --- | --- | --- | --- |
| `scan-clean` | 0.253 | **0.032** | 0.030 |
| `scan-noisy` | 0.421 | **0.029** | 0.848 |
| `scan-skewed` | 0.353 | **0.029** | 0.886 |
| `line-art-thin` | 0.036 | **0.052** | 0.255 |
| whole corpus | 0.108 | **0.074** | 0.141 |
| paths / nodes | 6791 / 73883 | **2125 / 21405** | — |

The bar was *"potrace reaches vtracer's number on scans without vtracer's habit
of deleting thin strokes"*. It reaches it on all three scans and beats it on two,
while thin line art costs 0.052 against vtracer's 0.255 — and the whole corpus
needs **a third of the paths and a third of the nodes**, which is stitch count.

The other half of the gate was the measurement: `scan-clean` went from `flat`
0.001 / `thin` 0.844 to **`flat` 0.907 / `thin` 0.010**, and B2 grades it
**good**, which is what the manifest said a human would say all along.

**Three stages were wrong first, and each mistake is the reason the code is
shaped as it is.**

*The noise estimate must not read artwork as noise.* Sizing the filter from the
**median** difference between neighbouring pixels puts `hatching` at 147 —
crosshatch means half of all neighbours straddle a stroke, so the median lands
mid-edge. At that width the filter blurs across the strokes: the verdict stayed
*hopeless*, but for the wrong reason and with the drawing turned to grey tone.
A low percentile cannot be dragged up that way, however much of the image is
edges, and the result is capped so that a filter which *would* cross a real edge
cannot be built. Then the estimate has to be taken **before** the background is
flattened — snapping the paper to one value sets half the pixels equal, and
`scan-noisy` estimated 37 as it arrived and 0 once flattened, so the filter sized
itself for an image with no grain and left the grain alone.

*Contrast normalisation is off unless it is needed.* Stretching an image that
already spans the range does nothing in the middle and clips at both ends, and
clipping manufactures flat pixels: it posterised `gradient-linear` from 189
colours to 34 and moved a ramp from *hopeless* to *marginal*. Preprocessing that
makes an unconvertible image look convertible is worse than none. It now fires
only below a measured threshold, which **no image in the corpus trips**, and is
tested on a purpose-built faded fixture instead.

*Stage 6 despeckles; it does not open.* The plan said "morphological open/close
… removes specks and bridges thinner than `stroke.min_width`". But an opening at
the minimum width **is** A5's `geometry.min_feature_size` fixer, which that step
measured, classified **destructive**, and put behind a flag naming the rule —
because it deletes any shape narrower than the needle, and a designer's hairline
is a shape. Run silently here it took `line-art-thin` from 2.9% ink to 0.1%:
vtracer's failure mode, faithfully reproduced. Dropping small *connected
regions* keeps the hairline, because a stroke crossing the image is thin without
being small.

**And then stage 6 turned out to have nothing to do, which is a finding rather
than a disappointment.** By the time it runs the bilateral filter has taken the
grain out, so the only regions small enough to absorb were the fragments of thin
line art — at 3 px it ate **42% of `hatching`**. Every setting that removed
anything removed artwork. So it defaults to removing nothing, stays implemented
and tested for the dust specks a *real* scan has and a generated corpus does not,
and the detail-removal the plan imagined stays where A5 put it. The second pass
of the filter is the same story in miniature: a third pass changes nothing on any
image, so there are two.

**The bug worth the most, found by reading a palette.** `scan-clean` came out of
the quantiser holding `#faf7f0` **and** `#f9f6ef` — 0.3 apart in CIE Lab, which
is not two colours. Median cut splits the paper in two whenever it has a spare
entry and nowhere better to put it. The cost is not cosmetic: two entries
alternating across one flat region put a boundary on every other pixel, so 2% of
the image measured as *too fine to stitch* — an entirely manufactured defect,
and on its own enough to keep the file out of *good*. Merging entries closer than
a just-noticeable difference is also what a shop wants on its own terms, because
a palette entry is a thread change.

The matching fix on the same image: the corner flood fill cannot reach the paper
**inside** a drawn ring, and no reading of that region calls it anything but
background. Filling only what is connected stranded a quarter of the paper with
its grain intact. The fill now *identifies* the colour and a snap *applies* it
wherever it sits.

**What it costs elsewhere, stated rather than buried.** `hatching` traces worse
(0.571 → 0.744) and so does `photo-landscape` (0.005 → 0.087); both are
*hopeless* either way, and a tracer's fidelity to an image nobody can stitch is
not a number worth optimising. `line-art-thin` gives up 0.016 of `fit` to the
contrast stretch, against 0.22–0.32 gained on each scan.

> **B2's warning, checked rather than trusted.** B2 predicted that convergent
> denoising pulls `hatching` *past* `scan-clean` — making the hopeless image look
> better than the good one — and left the rule that if B3 moves the crosshatch
> out of *hopeless*, B3 is deleting artwork. It does not: `hatching` keeps
> `thin` 0.993 and its verdict, and a test asserts both.

> **The triage table's miss moved, and the new one is honest.** Preprocessed,
> the corpus still grades 19/20, but the miss is now `scan-skewed` — *marginal*
> to a human because it is crooked on the glass, and **nothing here measures
> rotation**. Before B3 it landed on *marginal* for its grain, which was the
> right answer for the wrong reason; with the grain gone there is no signal left.
> A deskew stage is the obvious next thing the corpus is asking for.

<details>
<summary>Original plan for this step</summary>

The heart of the phase. Each stage independently testable:

1. load, honour EXIF rotation, upscale small inputs
2. background handling — alpha channel, or flood-fill from the corners
3. denoise (bilateral filter: smooths noise, keeps edges)
4. contrast normalisation
5. **colour quantisation to `k` colours in Lab space** — where `k` comes from
   the profile's `color.max_count`
6. **morphological open/close** — removes specks and bridges thinner than the
   profile's `stroke.min_width`, i.e. exactly the detail a needle can't render

Stages 5 and 6 already exist in RGB, in `raster.py`, because B1's metrics needed
them: `quantise()` (median cut + Lloyd, keeping dominant colours verbatim) and
`thin_ratio()`'s opening. B3 replaces the quantiser with a Lab one for the
*conversion* path; the measuring path stays dependency-free, and the baseline is
what shows whether Lab was worth it.

**Done when:** each stage has a before/after test on a fixture, and B1's metrics
improve measurably versus tracing the raw image. The specific numbers to move:
`scan-clean` currently measures `flat` 0.001 and `thin` 0.84 — denoising should
make a scanned line drawing measure like the flat artwork it is — and B0 put a
figure on the tracing half of the same gap. potrace scores `fit` 0.250 on that
image and spends 919 paths doing it; vtracer, whose photo-tuned preprocessing
smooths the grain first, gets 0.030. **B3 succeeds when potrace reaches
vtracer's number on scans without vtracer's habit of deleting thin strokes**
(`line-art-thin`, where the same preprocessing costs it 0.224 against potrace's
0.036).

B2 added a third gate and a warning. The gate: `scan-clean` should come out of
preprocessing graded **good**, which removes the one disagreement in the triage
table. The warning is what B2's rejected spike found — a majority-vote denoiser
run to convergence pulls `hatching` (0.993 → 0.131 `thin`) *past* `scan-clean`
(0.844 → 0.153), i.e. it makes the hopeless image look better than the good one
by smoothing the artwork away. **Preprocessing that improves every number is not
automatically preprocessing that works**, and the triage table is where that
shows up: if B3 moves `hatching` out of *hopeless*, B3 is deleting artwork, not
denoising.

</details>

> **One item deferred, deliberately: EXIF rotation.** Stage 1 says "load, honour
> EXIF rotation, upscale small inputs", and the upscale is built. Orientation is
> a property of the *file*, not of the pixels, and only Pillow can read it — so
> it belongs in `raster.load_image` beside the rest of the format handling
> rather than in a pipeline that takes a decoded raster. The corpus is PNG and
> carries no orientation, so shipping it here would have meant a stage no test
> could exercise. It lands with the first real JPEG.

### B4. Layered tracing · ✅ CLEARED

B0 had already built the easy half — one mask per colour, one `<g>` each, so
`structure.color_layers` passes by construction rather than by repair. What was
left was the half that decides whether the result is *stitchable*: the seams
between those layers, and the order they go down in.

**Gate met.** On the corpus's own three-colour logo, preprocessed and traced:
three layers, and `svgemb check -p embroidery-basic` says
`✅ PASS: 0 error(s), 0 warning(s), 13 check(s) passed`. The seams went from
0.4% of the image bare to 0.00%, and across the whole corpus:

| | butt joints | grown 1px |
| --- | --- | --- |
| `gaps`, mean over the corpus | 0.021 | **0.000** |
| `gaps`, worst image (`hatching`) | 0.274 | **0.000** |
| `fit`, mean | 0.074 | **0.073** |
| paths / nodes | 2125 / 21405 | **1856 / 19827** |

**The seam was real, and bigger than expected.** Tracing colours separately
means every shared border is drawn twice, once from each side, and two smoothed
curves through the same staircase do not coincide — so the document has no paint
at all along a hairline. Even where the outlines *do* agree exactly the joint
shows, because two shapes each covering half of a boundary pixel composite to
three quarters of one. Measured before any of this existed: 2–6% of a typical
image not fully covered, and on `scan-clean` pixels at **zero** coverage, which
is an outright hole. On fabric that is not a rendering artefact, it is bare
cloth between two blocks of stitching.

**The fix is the printer's trap, and the shape of it is the whole idea.** Not
"grow every mask" — that thickens every shape by a pixel and lets the *lower*
colour decide where the visible edge is, so the artwork moves. Each layer is
grown only into the pixels of layers stitched **after** it, which are painted
over afterwards anyway. Three things follow, and they are why this shape was
chosen over the obvious one:

- no gap can survive, because between any two layers the earlier one already
  covers the later one's first pixel;
- nothing visible moves, because the later layer keeps its own outline and
  paints it on top — verified by rendering with and without and checking that
  every pixel that changed had been a gap;
- the topmost layer is not grown at all, because there is nothing above it to
  hide the growth under.

It is also what an embroidery digitiser does by hand, for a reason no renderer
can show you: fabric moves under the needle, and a butt joint opens up on the
first wash.

**One working pixel, and the profile picks it without a knob.** B1's
`scale_for` already chooses the working resolution so the profile's thinnest
stitchable line spans three pixels — so one pixel is a third of the minimum
feature whatever the shop's numbers say: 0.5 mm against a 1.5 mm needle, which
is the range embroidery software calls pull compensation. `--overlap N` exists
because the measurement needs a way to turn the thing off, not because anyone
should be tuning it.

**Ordering: area decides, and darkness only breaks a tie.** The plan said
*darkest-last, so light backgrounds sit underneath*, which is a true observation
about the usual case and the wrong rule to write down — it is a claim about a
*colour*, and stacking is a question about *shape*. Give it a design on a black
background and darkest-last puts the background on top of the artwork: not a
seam bug, a blank picture. Area is the proxy that cannot fail that way because
it is a fact about the geometry — nothing can be surrounded by something smaller
than itself. Where two colours cover *exactly* the same number of pixels there
is no such fact to read, and only there does the darker one go later.

The roadmap suggested the profile should settle this. It shouldn't: nothing in a
profile expresses stitching order, no rule checks it, and adding a knob would
have created a second source of truth about something the labels already answer.
What made the decision small is the trap itself — since a layer is grown only
under its successors, every colour's *visible* edge is its own outline whatever
the order, so what is left to choose is the order the machine sews in.

**What it costs, and the finding underneath.** Corpus-wide the overlap *saves*
13% of the paths and 7% of the nodes, because dilating a mask fills in
single-pixel notches the tracer would otherwise have to follow. But
`logo-five-colour` goes the other way, 15 paths to 46, and the reason is worth
more than the number: **the tracer had been silently deleting speckle, and the
deletion was itself a source of gaps.** That image's bottom layer contains 60
regions, 53 of them one or two pixels — five flat colours squeezed into three
leaves islands — and potrace's `turdsize` was dropping every one of them,
leaving a hole where each had been. Grown by a pixel they clear the threshold
and get stitched.

That is the honest outcome rather than a regression: the document now says what
the labels say. And the checker can now see it — traced with butt joints,
`logo-five-colour` *passes* `geometry.min_feature_size` under
`embroidery-strict`; traced with the seams closed it fails, reporting 2% of the
path's area as finer than 1.5 mm. A5's check was being fed a file with the
evidence already removed. **Deleting detail too fine to stitch is B5's job, done
deliberately and reported** — not a tracer default nobody chose.

The same effect is the whole of the `fit` cost, which is otherwise nil: on
`monogram` and `line-art-thick` the overlap costs 0.004 and 0.002, and both are
paying for a palette entry of 56 and 460 pixels — an antialias remnant, not a
thread. Quantise those images to two colours instead of three and the overlap is
free to three decimal places (0.0074 → 0.0075) while still closing every gap.

**New column: `gaps`**, filled from the render `fit` was already doing, so it
costs nothing. It is the share of the traced document that has no paint on it,
and it needs no second render and no threshold: a renderer writes alpha below
255 exactly where nothing covered the pixel. `fit` cannot distinguish that from
a colour in the wrong place, and they are not the same news — one is a wrong
thread, the other is no thread. A one-pixel frame is excluded, because the
outermost row of any correct document is half-covered by the edge of the
artwork, and counting it would put a floor under the metric that no fix could
reach.

**Not zero, and the residue is a real thing rather than a tolerance.** Three
layers meeting at a point leave a few pixels a fraction short — the layer that
should cover the junction is doing it with a one-pixel spur, and a tracer
smooths those away. Six pixels on the test logo, none of them below 94%
coverage, 0.01% of the image. A second pixel of overlap closes it completely and
costs a millimetre of extra stitching over every border in the design to do so,
which is the wrong trade.

**vtracer is left alone**, and now says so on the run: it traces colour directly,
so it draws its own boundaries and no overlap of ours applies — whatever its
`gaps` cell says is its own doing. For the record it scores 0.000 there too, by
stacking whole shapes rather than tiling them. Regrouping its flat output by
fill would make it *look* profile-shaped while it still ignores the colour
budget, which is a worse kind of wrong than being honestly unsuitable. It is not
the default and B0 explained why.

<details>
<summary>Original plan for this step</summary>

Split the quantised image into one binary mask per colour and trace each
separately. This is the elegant part: **the "one colour per layer" requirement
falls straight out of the method** — each traced mask becomes its own `<g>`, so
`structure.color_layers` passes by construction rather than by repair.

Handle overlaps deliberately: trace darkest-last so light backgrounds sit
underneath, and grow each mask by a hair to avoid hairline gaps between
neighbouring colours.

**Done when:** a three-colour logo produces three layers, each single-coloured,
with no visible seams.

**Half-built by B0, and fed by B3.** The quantisation B4 splits is now the Lab
one, already despeckled and already merged down to the colours a person can
tell apart — so "one layer per colour" starts from a palette worth having.
`tracer.py`'s mask backends already split the quantisation
into per-colour masks, trace each, and assemble one `<g>` per colour ordered
largest-area-first, with the fill on the group — so the layered structure exists
and is tested. What B4 owes is the seam work (growing each mask by a hair) and
the ordering rule stated above, which is *darkest-last* where B0 used
*largest-first*; those disagree on a dark background and the profile should
settle it. Note also that vtracer, being a colour tracer, produces none of this:
its flat output is reported as a note on the run rather than quietly restructured.

</details>

> **A wart the step exposed, and fixed.** Adding a column bumps the baseline
> format, and a stale baseline printed *"re-record it with `svgemb bench
> --save`"* — then refused to run `--save` until the baseline was readable,
> which is exactly when it is not. Advice a command prints has to be advice that
> command will take.

> **Still open: `geometry.min_gap`.** A5 deferred the narrow-gap check here on
> the grounds that B4 would need the same closing operation for its seams. It
> turned out not to: seams are a pixel-grid problem and the fix is a pixel-grid
> dilation, pure Python and available on a phone, where a gap *check* on
> arbitrary artwork needs the geometry backend. So the two do not share code
> after all, and the check is still unwritten — it belongs with A5's opening,
> not here.

### B5. Vector cleanup · size M

Reuse Phase A's machinery on the tracer's raw output:

- simplify curves until `path.max_count` is satisfied
- drop shapes below minimum stitchable area
- close all subpaths, set canvas size in cm, assemble the layer structure

**Done when:** the tracer's output passes the target profile after cleanup.

**What B4 hands it, and why the second item moved up the list.** The layer
structure is assembled and the canvas is already in millimetres from the
profile, so that third line is done. The second one is now the interesting one:
closing the seams stopped potrace's `turdsize` from quietly deleting speckle, so
`logo-five-colour` traces 46 subpaths where it traced 15, and
`geometry.min_feature_size` reports 2% of a path as unstitchable where it
previously reported nothing. The detail was always there — the tracer was
removing the evidence along with it, and leaving a hole in its place. B5 is
where it gets removed on purpose, by a rule the profile names, with the removal
reported. A4's fixers and A5's `min_feature_size` repair are most of the
machinery.

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

The `passes` column is left empty until then on purpose — the interesting part
is the retry, not the single check. Though the single check is worth knowing
about: B0's traced `logo-two-colour`, with no cleanup at all, already returns
`✅ PASS: 0 error(s), 0 warning(s), 13 check(s) passed` against
`embroidery-basic`. One image is not a result, but B5 and B6 evidently start
closer to passing than this plan assumed.

### B7. In the UI · size M

Upload an image on the phone → side-by-side preview → sliders for colours and
detail → live re-check → download the SVG.

**Done when:** the full journey works on a phone, offline.

---

## Sequencing

```
A0 round-trip ──┬── A2 protocol ── A3 safe ── A4 lossy ──┐
A1 visual diff ─┘                                        ├── A6 CLI/UI ── A7 asking
                    A5 geometry ─────────────────────────┘
                         │
                    ═══ GATE ═══
                         │
B1 corpus+metrics ──┬── B3 preprocessing ── B4 tracing ── B5 cleanup ── B6 loop ── B7 UI
   ✅ CLEARED       │      ✅ CLEARED        ✅ CLEARED
B0 tracer spike ────┤
   ✅ CLEARED       │
B2 triage ──────────┘
   ✅ CLEARED
```

A0/A1 are independent of each other, as are B0 and B2. B1 turned out *not* to be
parallel with B0 after all: B0's gate is "a spike comparing output **on the
corpus**", so the corpus has to exist first — which is also why the roadmap
marks B1 "do this first" despite listing it second. Everything else is a chain.

**Where Phase B stands:** B0 through B4 are done, and what they add up to is a
pipeline: a scan goes in and a layered, seam-free SVG comes out that
`svgemb check` passes. `scan-clean` went from `fit` 0.253 to 0.032, the corpus
needs a third of the paths it did, and no image in it traces with a gap in it
any more. **B5 is next**, and B4 sharpened what it is for: the trace now
faithfully reproduces one- and two-pixel islands the tracer used to delete
behind everyone's back, `geometry.min_feature_size` can finally see them, and
dropping them is a decision that should be made and reported rather than
inherited from a tracer's default.

The corpus is also still asking for two things nobody planned: a **deskew**
stage (the only triage miss left is `scan-skewed`, and nothing measures
rotation) and **EXIF orientation** (deferred out of B3, see there).

## Risks, named up front

| Risk | Mitigation |
| --- | --- |
| ElementTree can't round-trip real files | A0 is a gate, and the text-patching fallback is chosen there — not discovered in A4 |
| Compiled dependencies break the Termux story | Everything heavy goes in optional extras; core stays stdlib+PyYAML; CI installs the bare core and runs the checker suite |
| "Fixed" files look wrong | Risk levels, `SAFE` by default, visual diff budget enforced in tests |
| Photos make bad embroidery, users blame the tool | B2 triage sets expectations *before* conversion |
| Tuning turns into endless guesswork | B1's metric table lands before any tuning work |
| Auto-fix quietly changes design intent | Every fix is reported; `--dry-run` shows the diff; nothing overwrites without a flag |
