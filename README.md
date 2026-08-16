# svg-for-embroidery

Check SVG files against the upload rules of embroidery and print-on-demand shops
— before the shop rejects them.

Every shop has its own list of requirements (max size, max colours, no gradients,
text as paths, minimum line width …). This project keeps the **checks** and the
**rules** apart: checks are small reusable Python classes, and each shop's ruleset
is a YAML file that picks the checks it needs. Supporting a new shop is usually a
ten-line YAML file, not new code.

```
$ svgemb check design.svg --profile embroidery-basic
📄 design.svg   [profile: embroidery-basic]
──────────────────────────────────────────────────────────────
❌ Width 5 cm is below the minimum of 10 cm.  [geometry.canvas_size]
    → Scale the artwork up, keeping the aspect ratio.
❌ 1 gradient/pattern definition(s) found (linearGradient).  [color.no_gradients]
    at /svg/defs[1]/linearGradient[1]
    → Replace gradients with flat colour areas.
❌ 1 <text> element(s) found — not allowed.  [element.forbidden]
    at /svg/text[1]
    → Convert text to paths (Inkscape: Path ▸ Object to Path).
──────────────────────────────────────────────────────────────
❌ FAIL: 3 error(s), 0 warning(s), 9 check(s) passed.
```

## Install

```bash
pip install -e .          # PyYAML is the only runtime dependency
pip install -e ".[dev]"   # + pytest
```

Python 3.9+. SVG parsing uses the standard library, so there is nothing to
compile.

## Usage

```bash
svgemb check design.svg                        # default profile: embroidery-basic
svgemb check design.svg -p plotter-vinyl       # a different shop's ruleset
svgemb check ./designs -r                      # every .svg in a directory tree
svgemb check design.svg -v                     # also list the checks that passed
svgemb check design.svg --strict               # warnings count as failures
svgemb check design.svg --json                 # machine readable
svgemb check design.svg -p ./profiles/myshop.yaml

svgemb fix design.svg                          # what it would repair; writes nothing
svgemb fix design.svg -o fixed.svg             # repair, then re-check the result
svgemb fix design.svg --dry-run                # the same, with a diff
svgemb fix design.svg --allow lossy            # also repairs that change the design
svgemb fix ./designs -r --in-place             # a whole folder (keeps each .svg.bak)
svgemb fix design.svg --only color.max_count   # one rule at a time
svgemb fix design.svg -I                       # answer its questions at the prompt
svgemb fix design.svg --choose structure.color_layers=runs
svgemb fix design.svg --stdout > fixed.svg     # for a pipe (the report goes to stderr)

svgemb assess photo.jpg                        # can this image become embroidery?
svgemb assess ./photos -r                      # a whole folder, one line each
svgemb assess photo.jpg -v                     # every reading, not just the deciding one
svgemb assess photo.jpg --strict               # "marginal" counts as a failure
svgemb assess --explain                        # the bands and their thresholds

svgemb profiles                                # list all rulesets
svgemb profiles example-shop                   # show one ruleset's rules
svgemb rules                                   # list all checks and their parameters

svgemb bench                                   # measure the image corpus (B1)
svgemb bench --preprocess                      # the same, after B3 cleans each image
svgemb bench --tracers                         # compare every installed tracer (B0)
svgemb bench --overlap 0                       # trace butt joints, to see the seams (B4)

svgemb serve                                   # local web UI on http://localhost:8000
svgemb roundtrip design.svg                    # verify read/write changes nothing
svgemb doctor                                  # what this machine can do
```

Exit codes: `0` pass, `1` the file violates the ruleset, `2` usage/configuration
error (so it drops straight into CI). `svgemb fix` adds `3` — svgemb caught its
own output misbehaving and refused to write it. `svgemb assess` returns `1` for
a hopeless image (and, with `--strict`, a marginal one).

As a library:

```python
from svg_embroidery import check_file, render_text

report = check_file("design.svg", profile="embroidery-basic")
print(report.passed(), report.counts())
for finding in report.errors:
    print(finding.rule_id, finding.message, finding.location)
print(render_text(report, verbose=True))
```

## Run it on your phone

There are no compiled dependencies (SVG parsing is standard library, PyYAML is
pure Python), so Termux installs it without a toolchain.

```bash
pkg update && pkg install python git
git clone https://github.com/GregsterBoe/svg-for-embroidery
cd svg-for-embroidery
pip install -e .

svgemb check ~/storage/shared/Download/design.svg   # command line
svgemb serve                                        # or the web UI
```

For the CLI, run `termux-setup-storage` once so Termux can reach your Downloads
folder. For the web UI, open **http://localhost:8000** in your phone's browser
and pick the file through the normal Android file picker — no storage permission
needed, since the browser reads the file and posts its contents to the local
server.

The web UI is one self-contained page (no CDN, no external fonts), so it works
with the phone offline. It shows a preview of the design, lets you switch
rulesets to compare shops, and has a "copy report as text" button. A failing
file also gets a **Fix what can be fixed** button: it lists what was repaired
and what was left alone, shows the design before and after side by side, and
offers the result as a download — your file is never modified in place. Where a
repair needs a decision, the choices appear as buttons saying what each one
costs; that is the only way to reach a repair that deletes artwork, because
there is no *setting* here that quietly switches one on. Pull down
Termux's notification and tap **Acquire wakelock** if Android keeps killing the
server in the background.

```bash
svgemb serve --port 8080          # different port
svgemb serve --host 0.0.0.0       # reach it from your laptop: http://<phone-ip>:8000
```

`--host 0.0.0.0` exposes the checker to everyone on the same Wi-Fi, so use it
only on a network you trust; the default binds to localhost only. The server
writes nothing to disk — uploads are checked in memory and dropped, request
bodies are capped at 8 MB, and the uploaded filename is stripped to its basename.

Other options: **Pydroid 3** runs the CLI on Android without Termux, **a-Shell**
does the same on iOS, and you can always run `svgemb serve --host 0.0.0.0` on a
PC or Raspberry Pi and just browse to it from the phone.

## Adding a shop (the modular part)

A profile is a YAML file listing rule ids with parameters. It can `extends`
another profile, override single rules and `disable` inherited ones:

```yaml
name: myshop
title: My Shop
url: https://myshop.example/upload-requirements
extends: embroidery-basic

rules:
  - id: geometry.canvas_size      # override the inherited parameters
    params:
      min_cm: 8
      max_cm: 25

  - id: color.allowed_palette     # add a rule the parent doesn't use
    params:
      palette_name: "MyShop threads"
      colors: ["#000000", "#ffffff", "#c8102e"]

  - id: structure.color_layers    # keep the rule, soften the verdict
    severity: warning

disable:
  - stroke.min_width              # this shop doesn't care
```

Profiles are looked up in this order:

1. built-in profiles (`src/svg_embroidery/profiles/builtin/`)
2. every directory in `$SVG_EMBROIDERY_PROFILE_PATH`
3. `./profiles/` next to your working directory

or pass a path to the YAML file directly. `src/svg_embroidery/profiles/builtin/example-shop.yaml`
is a commented template to copy.

### Built-in profiles

| Profile | For |
| --- | --- |
| `embroidery-basic` | The common denominator: 10–38 cm, ≤3 colours one per layer, no gradients, text as paths, closed contours, ≥1.5 mm strokes, no specks under 2.25 mm² |
| `embroidery-strict` | Small/cheap stitching: 8–30 cm, ≤2 colours, no strokes at all, ≤60 shapes, no detail finer than 1.5 mm and no shape smaller than one stitch |
| `plotter-vinyl` | Cutting plotters: exactly 1 colour, closed contours, filled shapes only, no detail finer than 1 mm, nothing under 1 mm² to weed |
| `example-shop` | Commented template showing palette restriction and severity overrides |

## Available checks

`svgemb rules` prints these with their parameters and defaults.

| Rule id | Checks |
| --- | --- |
| `file.extension` | File uses an accepted extension |
| `geometry.canvas_size` | Width/height within limits (global or per axis) |
| `geometry.aspect_ratio` | Canvas not too elongated |
| `geometry.require_viewbox` | `<svg>` carries a `viewBox` |
| `color.max_count` | At most N distinct colours |
| `color.allowed_palette` | Only colours from a fixed thread palette |
| `color.no_gradients` | No `linearGradient` / `radialGradient` / `pattern` |
| `color.no_transparency` | No partial opacity |
| `structure.color_layers` | Each colour on its own layer |
| `element.forbidden` | No `<text>`, `<image>`, `<filter>`, … |
| `document.no_raster` | No embedded bitmap `data:image` payloads |
| `document.no_editor_metadata` | No leftover editor state (it can carry your file name) |
| `path.closed` | Every path *and subpath* ends with `Z` |
| `path.max_count` | Design doesn't exceed N shapes (subpaths, not elements) |
| `geometry.min_feature_size` | No filled detail finer than N mm (needs the geometry extra) |
| `geometry.min_area` | No shape smaller than N mm² — a speck the needle can't sew |
| `stroke.min_width` | Effective stroke width ≥ N mm |
| `stroke.forbidden` | Filled shapes only, no strokes |
| `fill.required` | Every visible shape has a fill |

Each rule takes a `severity` (`error`, `warning`, `info`) so the same check can
block one shop and merely warn for another.

### Writing a new check

Only needed when no existing rule covers the requirement:

```python
from svg_embroidery.rules import Rule, register

@register
class NoTinyHolesRule(Rule):
    id = "path.min_hole"
    summary = "Cut-out holes must be large enough to weed"
    params = {"min_mm": 2.0}

    def check(self, doc):
        ...
        yield self.fail("…", hint="…", location=node.location)
        # or: yield self.ok("…")
```

Rules receive an `SvgDocument` with resolved geometry (millimetres, viewBox
aware), resolved presentation attributes (inheritance and `style=` applied) and
cumulative transform scales — so a `stroke-width` inside `transform="scale(4)"`
is measured at its real size.

## Where this is going

[`docs/ROADMAP.md`](docs/ROADMAP.md) plans two phases: opt-in automatic fixing
of the errors reported here, then converting raster images into embroidery-ready
SVGs. Both are split into steps with explicit validation gates.

**Phase A is complete** — `svgemb fix` is the whole of it, seen from outside.
The steps behind it:

- **A0** — the tool writes SVGs back out without damaging them. An element
  nobody edited is copied byte for byte from the source, so a future fix
  produces a diff of the lines it actually changed.
- **A1** — it can measure whether a change altered the rendered image, so a
  "safe" fix can be proved safe rather than asserted to be.
- **A2** — the fix protocol. Fixers register against a rule, declare a risk
  level (`safe` / `lossy` / `destructive`), and only safe ones run by default.
  The engine verifies its own work: a "safe" fix that moves a pixel, or any fix
  that introduces a new error, fails the run instead of being written out.

```python
from svg_embroidery.fixes import FixEngine

engine = FixEngine.from_profile_name("embroidery-basic")
report = engine.fix_source(open("design.svg").read())
print(report.summary())   # what changed, and whether it verified
print(report.diff())      # unified diff of just the changed lines
```

- **A3** — the first batch of safe fixers. All of them preserve the image
  exactly, verified against a renderer:

| Rule | Fix |
| --- | --- |
| `geometry.canvas_size` | scale width/height into the allowed range |
| `geometry.require_viewbox` | add the viewBox implied by width/height |
| `color.no_gradients` | delete gradient definitions nothing references |
| `element.forbidden` | remove forbidden elements that draw nothing |
| `document.no_editor_metadata` | strip editor state, keeping layer annotations |

They only remove what was never being drawn, so anything visible is left alone
with an explanation.

- **A4** — fixes that change the picture on purpose. These need `--allow lossy`,
  and each declares how much of the image it may change:

| Rule | Fix |
| --- | --- |
| `color.max_count` | merge near-duplicate colours (in CIE Lab) down to the limit |
| `color.allowed_palette` | snap colours to the nearest stocked thread |
| `color.no_transparency` | composite semi-transparent paint onto the page |
| `path.closed` | close stroked contours whose ends nearly meet |

Every colour substitution is reported, so you can disagree with it. Note that
closing an *unstroked* contour is a safe fix, not a lossy one — a fill already
renders open subpaths as closed, so writing the `Z` changes nothing.

- **A5** — the geometry layer: a new check (`geometry.min_feature_size`, which
  finds detail a needle cannot render) and three fixes. Flattening curves to
  points is pure Python and always available, so *measuring* a design never
  needs a compiled dependency; offsetting and boolean operations use
  [shapely](https://shapely.readthedocs.io/) and are detected at runtime.

| Rule | Fix | Risk |
| --- | --- | --- |
| `stroke.min_width` | thicken strokes to the minimum (no geometry backend needed) | lossy |
| `stroke.forbidden` | redraw the stroke as the filled shape it paints | lossy |
| `geometry.min_feature_size` | cut away detail too fine to stitch | destructive |

A stroke converted to an outline comes out **pixel-identical** — an 80 mm box
stroked at 4 mm is exactly the 84 mm square minus the 76 mm one. The last of
the three is the first `destructive` fix in the project and stays out of every
run that does not ask for it by name: nothing makes a thin shape thick without
inventing artwork, so the only honest repair is to remove what the needle was
going to drop, and to make you say so.

Without the geometry extra those two fixes are unavailable and
`geometry.min_feature_size` reports that it did not measure — it never changes
a verdict based on what happens to be installed.

- **A6** — `svgemb fix`, and the same thing as a button in the web UI. The
  command applies what it is allowed to, re-checks the file, and prints the
  before/after verdict:

```
$ svgemb fix design.svg --allow lossy -o fixed.svg
📄 design.svg   [profile: embroidery-basic]
──────────────────────────────────────────────────────────────
✅ fixed    geometry.canvas_size  [safe]
       width 5cm -> 10cm  (/svg)
✅ fixed    color.max_count  [lossy]
       #654321 -> #123456 (1 place(s))
⏭ skipped  element.forbidden  [safe]
       <text> are part of the artwork; removing them would delete visible
       content, so that is a manual decision
ℹ️ not measured  geometry.min_feature_size
       …this machine has no path geometry backend. To measure it, install
       the geometry extra: pip install "svg-for-embroidery[geometry]".
──────────────────────────────────────────────────────────────
❌ FAIL → ❌ FAIL   9 → 4 error(s), 1 → 1 warning(s)
   visual: 1437/65536 pixels differ (2.19%), max channel delta 255
   still failing: color.no_gradients, element.forbidden, path.closed
wrote fixed.svg
```

**The skips are the important half.** A file that still fails after fixing is
the normal case — the remaining errors are the ones needing a human — so each is
listed with the reason it was left: a decision only you can make, a risk level
you didn't allow, or a package you can install. Nothing is written unless you
say where (`-o`, `--in-place`, `--stdout`), `--in-place` keeps a `.bak`, and
`--allow destructive` will not run until you name the rule.

- **A7** — the repairs that **ask**. "No automatic fix available" is a dead end
  for the cases that matter most: the shop wants one colour per layer and your
  artwork has none, so *something* has to be regrouped, and only you know
  whether the order things overlap in is load-bearing. So the tool states the
  question and the answers it can carry out, and each answer prices itself:

```
❓ your call  structure.color_layers
       2 colours (#000000, #c8102e) across 3 top-level shapes, in 3 runs of colour.
       How should the colours be separated into layers?
       * runs       Keep the drawing order — 3 layers  [safe]
                    Wraps each run of same-coloured shapes, so 2 colours become 3
                    layers and a colour may appear on more than one. Nothing is
                    reordered: the design renders exactly as it does now.
         colors     One layer per colour — 2 layers  [destructive]
                    Exactly one layer per thread, which is what most shops' tooling
                    expects. Shapes move past each other to get there, so wherever
                    two colours overlap the one on top changes.
       answer with: --choose structure.color_layers=runs|colors
```

Answer with `--choose rule.id=option`, or `-I` to be asked at the prompt, or by
tapping the option in the web UI. Four rules ask so far:

| Rule | The question | Answers |
| --- | --- | --- |
| `structure.color_layers` | how to separate the colours | keep the drawing order (safe) · one layer per colour (destructive) |
| `color.no_gradients` | which flat colour replaces the ramp | its weighted average · its first stop (both lossy) |
| `element.forbidden` | delete artwork the profile forbids | delete it (destructive) |
| `path.closed` | close a gap too wide to be a slip | draw the segment (destructive) |
| `geometry.min_area` | delete shapes too small to sew (B5) | drop all of them (destructive) |
| `path.max_count` | drop the smallest shapes to get within the limit (B5) | drop the N smallest (destructive) |

Every answer is still a fix: it is held to the same four invariants, so *keep
the drawing order* is checked against a renderer and really does move no pixel.
And the run now repeats until it settles — flattening a gradient can push the
palette back over the limit that was reduced two rules earlier, so a single pass
would leave the file failing something the run itself broke.

**The upshot:** `examples/bad-design.svg` was written to be unfixable, and with
its four questions answered it passes.

```bash
svgemb fix examples/bad-design.svg --allow destructive \
  --choose color.no_gradients=average --choose structure.color_layers=colors \
  --choose element.forbidden=delete --choose path.closed=close \
  --choose geometry.min_area=drop
# ❌ FAIL → ✅ PASS   9 → 0 error(s)
```

`--allow destructive` has to name every destructive repair the *profile* offers,
not just the ones this file needs — which is why `geometry.min_area` is answered
here for a file that has no specks in it. The list of what a run may do is then
the same whichever file it is pointed at.

Check any of it against your own files:

```bash
svgemb roundtrip ~/designs -r
```

### Phase B has started: images → SVG

**B1 — the measuring instrument, built before the thing it measures.** The
premise of Phase B is that quality comes from preprocessing, not from the
tracer; that only pays off if preprocessing can be measured, so the benchmark
comes first and the tracer gets fitted to it.

```bash
make bench          # or: svgemb bench
```

Twenty generated images — flat logos, line art, scans, photos, transparency,
gradients, low-resolution junk — each measured against the profile it is aimed
at:

```
image              kind      expect     verdict       size   res   mm/px   colours    k    flat   quant   edges    thin
logo-two-colour    logo      good          good    256x256   200   0.500        12    3   0.957   0.005   0.025   0.003
line-art-thick     line-art  good          good    256x256   200   0.500         5    3   0.889   0.002   0.069   0.013
line-art-thin      line-art  marginal  marginal    256x256   200   0.500         5    3   0.918   0.001   0.060   0.030
hatching           line-art  hopeless  hopeless    256x256   200   0.500         8    3   0.047   0.382   0.864   0.993
photo-portrait     photo     hopeless  hopeless    256x256   200   0.500      1356    3   0.000   0.926   0.084   0.080
lowres-icon        junk      marginal  marginal      32x32    32   3.125         2    3   0.753   0.000   0.138       ·
```

`flat` is how vector-like the image is, `quant` what reducing to the profile's
colour budget costs, `edges` how much boundary a tracer must follow, `thin` how
much of it is finer than that shop's needle. Each metric declares which
direction is better, so the run grades itself against a saved baseline:

```
1 better, 2 worse, 0 changed with no direction:
  ❌ hatching  edges: 0.500 → 0.864
  ✅ logo-two-colour  quant: 0.400 → 0.005
```

Two things fell out of building it that are worth keeping:

**The resolution has to come from the profile.** Measured at a fixed 128px, a
5px stroke scored *worse* than a 1px one — at 0.78mm per pixel the smallest
kernel asks "thinner than 2.3mm?" when the profile said 1.5mm, so it condemned
strokes that stitch fine. Each image is now measured at whatever resolution
makes the profile's minimum feature a whole kernel wide. When the source is too
coarse to answer at all — `lowres-icon`, at 3.1mm per pixel — the cell is empty
and says why, rather than printing a number that means something else.

**A flat colour has to survive quantisation exactly.** Representing a colour
cluster by its mean is the textbook answer and it is wrong here: a logo's
antialiased rim drags the entry a few units off the brand colour, and then every
pixel of the logo counts as changed. A 2% rim became a 20% loss. Where one
colour dominates its cluster it is now kept verbatim; only where none does —
a photograph — is the mean right.

**B0 — buy a tracer, don't build one.** Writing one is a research project; three
already exist, so all three were wrapped in one interface and run over the same
corpus. The full write-up is [`docs/spikes/B0-tracers.md`](docs/spikes/B0-tracers.md).

```bash
svgemb bench --tracers
```

```
tracer           images   paths   nodes     fit  seconds
────────────────────────────────────────────────────────
potrace 1.16         20    6649   72915   0.108     0.47
potracer 0.0.4       20    6791   73883   0.108     5.19
vtracer 0.6.15       20    1472   24337   0.201     0.46
```

`fit` is the share of pixels the traced SVG gets wrong when rendered back and
compared to what it traced, and it is the column that decides. **A tracer that
draws fewer paths has either simplified the artwork or thrown it away, and only
`fit` says which** — read `paths` alone and vtracer wins four to one.

The result: **potrace, one mask per colour.** potracer is the same algorithm in
pure Python and scores identically, so the choice between them is 11× speed and
numpy-versus-nothing, not quality; it stays as the fallback for machines that
cannot install a binary. Each traced mask becomes its own `<g>`, largest area
first — so the layered structure `structure.color_layers` wants comes out of the
method rather than out of a repair.

The interesting result was vtracer's. It is the **best** tracer here on scans
(0.030 against potrace's 0.250) and the **worst** on line art (0.224 against
0.036) — because it is not a better tracer, it is a tracer with photograph-tuned
preprocessing bolted on. That is the clearest evidence yet for this phase's
premise, and it puts a number on what B3's preprocessing is worth.

**B2 — say "no" early, and say why.** Not every image can become embroidery.
Triage grades one before anything is converted, out of the numbers B1 already
takes — it measures nothing of its own, so `svgemb assess` and `svgemb bench`
cannot disagree about an image.

```bash
svgemb assess photo.jpg
```

```
🖼  photo-portrait.png   [profile: embroidery-basic]
──────────────────────────────────────────────────────────────
256x256; 1,356 colours; at 3 colours 93% of the image is repainted

❌ HOPELESS
   colour loss: reducing to 3 colours repaints 93% of the image, and what is
      lost is smooth shading rather than detail — there are no flat regions
      here to become stitches
```

The verdict is **the worst thing about the image**, and the report names the
reading that decided — "hopeless" is only useful next to *why*. The `verdict`
column in `svgemb bench` is the same call, printed next to the `expect` column
that records what a human says before measuring anything, so the run grades its
own thresholds:

```
triage agrees with 'expect' on 19/20 image(s)
ℹ️  scan-clean  expected good, triage says marginal
```

**The judgement the whole thing rests on: on a speckled image, colour loss and
flatness are measuring the grain, not the artwork.** Reduce a grainy scan of a
two-colour drawing to three colours and every grain pixel moves — `quant`
reports 0.87 about a design that is two flat colours. Left to vote, that number
calls every scan hopeless. So when a colour boundary falls on more than 40% of
the pixels (the three scans measure 0.62–0.67; everything else in the corpus is
below 0.19), those two readings **abstain and say so**, for the same reason B1
leaves a cell empty: a metric that cannot answer the question must not print a
number.

What can still be said under grain is whether anything survives the needle at
all — and at `thin ≥ 0.95` nothing does, anywhere, which no amount of denoising
can change. That is what separates the crosshatch (0.993, hopeless) from the
scans (0.78–0.84, marginal).

The one miss is **`scan-clean`**: a line drawing on paper that a human calls
good and triage calls marginal, because 84% of it is finer than the needle and
that 84% is grain. Telling the drawing from the paper needs the denoising B3
owes, so this is the same gap B1 recorded — and the report says the word
"denoising" out loud rather than implying the artwork is at fault. The error is
one band, in the cautious direction, and it is pinned by a test that will start
failing when B3 lands.

**B3 — the preprocessing pipeline, which is where the quality was.** Six stages,
all pure Python: upscale a tiny source, flatten the background, denoise with a
bilateral filter, normalise contrast when it is faded, quantise to the profile's
colour budget in CIE Lab, drop specks. Each takes its settings from the profile
rather than from a constant.

```bash
make prep           # or: svgemb bench --preprocess
svgemb bench --tracers --preprocess
```

The gate B0 set was *potrace reaching vtracer's number on scans, without
vtracer's habit of deleting thin strokes*:

| | potrace `fit` raw | preprocessed | vtracer, for scale |
| --- | --- | --- | --- |
| `scan-clean` | 0.253 | **0.032** | 0.030 |
| `scan-noisy` | 0.421 | **0.029** | 0.848 |
| `scan-skewed` | 0.353 | **0.029** | 0.886 |
| `line-art-thin` | 0.036 | **0.052** | 0.255 |
| whole corpus | 0.108 | **0.074** | 0.141 |
| paths / nodes | 6791 / 73883 | **2125 / 21405** | — |

It reaches vtracer's number on all three scans and beats it on two, while thin
line art costs 0.052 against vtracer's 0.255 — and the corpus needs **a third of
the paths and a third of the nodes**, which is stitch count. `scan-clean` also
stops measuring like junk: `flat` 0.001 → 0.907, `thin` 0.844 → 0.010, and
triage now grades it **good**.

Three stages were wrong first, and each mistake shaped the code:

**A noise estimate must not read artwork as noise.** Sizing the filter from the
*median* difference between neighbours puts `hatching` at 147 — crosshatch means
half of all neighbours straddle a stroke, so the median lands mid-edge. At that
width the filter blurs the drawing into grey tone. A low percentile can't be
dragged up that way, and the result is capped so a filter that *would* cross a
real edge cannot be built. The estimate also has to be taken **before** the
background is flattened, or it measures the flat region instead of the image.

**Contrast normalisation clips, and clipping manufactures flat pixels.** Applied
unconditionally it posterised `gradient-linear` from 189 colours to 34 and moved
a ramp from *hopeless* to *marginal* — preprocessing that makes an unconvertible
image look convertible is worse than none. It now fires only below a measured
threshold, which no corpus image trips.

**Stage 6 despeckles; it does not open.** An opening at the profile's minimum
width *is* Phase A's `geometry.min_feature_size` fixer, which is classified
destructive and gated behind a flag naming the rule. Run silently here it took
`line-art-thin` from 2.9% ink to 0.1% — vtracer's failure mode, reproduced. And
then it turned out to have nothing to do: the filter has already removed the
grain, so every setting aggressive enough to matter was eating artwork (42% of
`hatching` at 3px). It ships off by default, for the dust a real scan has and a
generated corpus does not.

The single most valuable fix came from reading a palette. `scan-clean` was
quantising to `#faf7f0` **and** `#f9f6ef` — 0.3 apart in CIE Lab, which is not
two colours. Two entries alternating across one flat region put a boundary on
every other pixel, so 2% of the image measured as *too fine to stitch*: a
manufactured defect, and on its own enough to keep the file out of *good*.
Merging entries closer than a just-noticeable difference is also what a shop
wants, because a palette entry is a thread change.

Costs, stated rather than buried: `hatching` (0.571 → 0.744) and
`photo-landscape` (0.005 → 0.087) trace *worse*. Both are hopeless either way,
and a tracer's fidelity to an image nobody can stitch is not worth optimising.

**B4 — layered tracing, and the seams between the layers.** One mask per colour
gives the layer structure for free; what it does not give is a join. Every
shared border is traced twice, once from each side, and two smoothed curves
through the same staircase do not coincide — so the document has no paint along
a hairline. Even where the outlines agree exactly the joint shows, because two
shapes each covering half a pixel composite to three quarters of one. On fabric
that is bare cloth between two blocks of stitching.

```bash
svgemb bench --preprocess               # the gaps column, with the fix on
svgemb bench --preprocess --overlap 0   # ...and with butt joints, to see it
```

| | butt joints | grown 1px |
| --- | --- | --- |
| `gaps`, mean over the corpus | 0.021 | **0.000** |
| `gaps`, worst image (`hatching`) | 0.274 | **0.000** |
| `fit`, mean | 0.074 | **0.073** |
| paths / nodes | 2125 / 21405 | **1856 / 19827** |

The fix is the printer's trap: each layer is grown **only into the pixels of
layers stitched after it**, which get painted over anyway. Not "grow
everything" — that thickens every shape and lets the lower colour decide the
visible edge, so the artwork moves. This way no gap can survive, nothing visible
moves, and the topmost layer is not grown at all. It is also what a digitiser
does by hand, because fabric shifts under the needle and a butt joint opens up
on the first wash. One working pixel is a third of the profile's minimum
feature by construction — around 0.5 mm for a 1.5 mm needle, which is the usual
pull compensation — so the profile sizes it without a knob.

**Ordering: area decides, darkness only breaks a tie.** "Darkest last, so light
backgrounds sit underneath" is true of the usual case and wrong as a rule — it
is a claim about a colour, and stacking is a question about shape. On a dark
background it puts the background on top of the artwork. Area cannot fail that
way, because nothing can be surrounded by something smaller than itself.

The finding worth carrying: corpus-wide the overlap *saves* 13% of the paths,
but `logo-five-colour` goes from 15 to 46 — because **the tracer had been
silently deleting speckle, and the deletion was itself making holes**. That
image's bottom layer has 60 regions, 53 of them one or two pixels, and potrace's
`turdsize` dropped every one. Grown by a pixel they survive and get stitched, so
`geometry.min_feature_size` now reports 2% of a path as unstitchable where it
previously reported nothing at all. Removing detail too fine for the needle is a
decision that should be made and reported — that is B5 — not inherited from a
tracer's default.

**B5 — vector cleanup: removing what cannot be sewn, on purpose.** The step
adds no fixing machinery. Phase A already has an engine that verifies its own
output and repairs that report every change; B5 is one rule the checker was
missing, two repairs registered against rules, and one decision about what a run
may do to a document nobody drew.

```bash
make clean-up       # or: svgemb bench --preprocess --cleanup
```

| | traced | cleaned up |
| --- | --- | --- |
| subpaths over the corpus | 1856 | **611** |
| nodes | 19827 | **13970** |
| `fit`, mean | 0.073 | **0.070** |
| images at `embroidery-strict` that pass | 11/20 | **16/20** |

**Two thirds of the shapes the machine was going to sew were shapes it cannot
sew** — and removing them makes the render *closer* to the source, because a
fleck below the tracer's own resolution mostly rendered as a smudge anyway.

**The rule that was missing: `geometry.min_area`.** B4 stopped the tracer
silently deleting one- and two-pixel islands, and `geometry.min_feature_size`
still could not see them: it measures a whole element — a traced colour layer is
one `<path>` — and forgives up to a tolerance of its area, so fifty specks
disappear into the rounding of a shape that is otherwise solid. A tolerance is
the right idea about a fraction of one shape and the wrong idea about a whole
one; every speck is a trim, a knot and a jump whatever share of the design it
is. It needs **nothing installed** (the area of a flattened contour is the
shoelace formula), so unlike the backend-dependent thinness check it can sit in
the common-denominator profile without its verdict depending on the machine.

**`path.max_count` was counting elements, and the traced `hatching` puts 864
shapes in two of them** — so the design that most needed a "too many shapes"
warning was guaranteed never to get it. It counts subpaths now, the unit an
embroidery machine sees and the one B0's benchmark already used.

**Deleting is not reshaping.** Using A5's thinness repair to remove one fleck
rebuilds the whole layer as a polyline, because a boolean result has no curves
in it — 207 nodes became 414 on `scan-clean` to delete 2% of the area. Cutting
the subpath out of the `d` attribute instead leaves every surviving curve byte
for byte as the tracer wrote it. Reshaping rescues a shape that is mostly fine;
deleting drops one that was never viable.

**Cleanup is allowed more than `svgemb fix` is, and the reason is written down.**
Repairing a designer's file is careful because there is intent in it. A traced
document has none — it was generated milliseconds ago from an image, and the
artwork of record is still the image. So it runs at the top risk level, the
engine still verifies every repair, every removal is reported, and exactly one
kind of question is answered in advance: `geometry.min_area` asks "may I delete
these 29 flecks" when the profile has already said in millimetres that a fleck
that size cannot be sewn. `path.max_count` asks which artwork to sacrifice to a
shape count — nothing in a profile ranks one shape above another — so that one
is left open and reported.

The safety net firing is the most useful thing that happened: `hatching` is a
crosshatch of hairlines, cleanup would have repainted 25.8% of it against a
declared budget of 25%, and the engine **discarded its own output** and handed
back the file it was given. A design made of detail below the needle cannot be
cleaned into one that isn't.

The last empty column is filled: `passes` says whether the traced document
satisfies the profile it was aimed at. Note what it does not say — at
`embroidery-basic` every corpus image passes, photographs included, because that
profile checks colours and structure rather than detail. **Passing a profile is
not the same as being worth stitching**, which is what the `verdict` column
beside it is for. B6 owns the interesting half: retrying with different settings
when the answer is no.

### Optional capabilities

The checker needs nothing but the standard library and PyYAML. Heavier work is
detected at runtime and degrades explicitly — a capability you don't have is a
measurement you don't get, never a crash. `svgemb doctor` shows where you stand
and what to install:

| Tier | Enables | Install |
| --- | --- | --- |
| Core | check, fix, write, round-trip, web UI | nothing — always available |
| Rendering | visual regression, before/after previews | `pkg install librsvg` (Termux), `apt install librsvg2-bin`, or `pip install cairosvg` |
| Path geometry | stroke → outline, minimum feature size | `pip install "svg-for-embroidery[geometry]"` |
| Reading images | measuring JPEG/GIF/BMP/WebP sources | `pip install "svg-for-embroidery[convert]"` |
| Tracing | turning a reduced image into paths | `pkg install potrace` / `apt install potrace`, or `pip install "svg-for-embroidery[trace]"` |

Measuring a **PNG** needs nothing at all — the pure-Python decoder the visual
harness already carries doubles as the corpus reader, so `svgemb bench` works on
a bare install. Pillow adds every other image format; without it a JPEG is a row
you don't get, with the reason printed, and the sweep continues.

Tracing degrades the same way: with no tracer the `paths`, `nodes`, `fit` and
`gaps` columns are empty and every other column still fills. And because the
tracer changes every one of those numbers, a run traced with a different tracer —
or a different *version* of the same one — is not diffed against the baseline at all.
It says which conditions differ and offers to re-record, rather than printing a
comparison that grades the tracer and looks like it grades the code.

There is deliberately **no separate mobile edition** — see the decision in
[`docs/ROADMAP.md`](docs/ROADMAP.md). When a phone can't run a heavy step, run
`svgemb serve --host 0.0.0.0` on a desktop and use it from the phone's browser.

## What is *not* checked

- **Minimum gap size** — the other half of `geometry.min_feature_size`: two
  shapes close enough to bleed into each other once stitched. Same machinery,
  a closing rather than an opening. It was expected to arrive with the tracer,
  on the grounds that layered tracing needed it for its seams; it didn't — those
  turned out to be a pixel-grid problem with a pixel-grid fix — so this is still
  waiting, next to the opening it mirrors.
- **Overlapping shapes / stitch order** — out of scope for a static checker.
- **`<use>` clones** are flagged (via `element.forbidden`) rather than expanded.
- Colours behind `url(#…)` references count as "not a flat colour"; the gradient
  rule reports them instead.

## Development

```bash
make test         # 429 tests
make degraded     # the same suite with every optional extra switched off
make bench        # measure the image corpus against the baseline
make tracers      # compare every installed tracer on the corpus (roadmap B0)
```

Layout:

```
src/svg_embroidery/
  document.py      SVG parsing → geometry, resolved styles, transforms
  units.py         length parsing/conversion (everything in mm)
  colors.py        colour normalisation (#FFF, white, rgb() → #ffffff)
  findings.py      Finding / Severity / Report
  rules/           the checks, one module per topic
  profiles/        profile loader + builtin/*.yaml rulesets
  checker.py       runs a profile against a document
  report.py        text / JSON rendering
  writer.py        serialising back out without damaging the file
  roundtrip.py     proof that read -> write changes nothing (roadmap A0)
  visual.py        render + compare images; pure-Python PNG (roadmap A1)
  geometry.py      flattening (pure Python) + offsetting/booleans (roadmap A5)
  raster.py        reading images, quantising, measuring them (roadmap B1)
  tracer.py        potrace / potracer / vtracer behind one API (roadmap B0)
  bench.py         the corpus sweep and the baseline diff (roadmap B1)
  capabilities.py  what this machine can do (svgemb doctor)
  fixes/           the fix protocol, engine and verifier (roadmap A2)
                     safe.py / lossy.py / geometry.py / choices.py — the fixers
  server.py        stdlib-only web UI: check and fix (svgemb serve)
  cli.py           svgemb check / fix / roundtrip / doctor / bench / serve
bench/
  make_corpus.py   generates the 20-image corpus, deterministically
  corpus/          the images and their manifest
  baseline.json    the numbers to beat
docs/
  ROADMAP.md       the plan, and what each step actually cost
  spikes/          write-ups for decisions that needed evidence first
```
