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

svgemb profiles                                # list all rulesets
svgemb profiles example-shop                   # show one ruleset's rules
svgemb rules                                   # list all checks and their parameters

svgemb serve                                   # local web UI on http://localhost:8000
svgemb roundtrip design.svg                    # verify read/write changes nothing
svgemb doctor                                  # what this machine can do
```

Exit codes: `0` pass, `1` the file violates the ruleset, `2` usage/configuration
error (so it drops straight into CI). `svgemb fix` adds `3` — svgemb caught its
own output misbehaving and refused to write it.

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
| `embroidery-basic` | The common denominator: 10–38 cm, ≤3 colours one per layer, no gradients, text as paths, closed contours, ≥1.5 mm strokes |
| `embroidery-strict` | Small/cheap stitching: 8–30 cm, ≤2 colours, no strokes at all, ≤60 shapes, no detail finer than 1.5 mm |
| `plotter-vinyl` | Cutting plotters: exactly 1 colour, closed contours, filled shapes only, no detail finer than 1 mm |
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
| `path.max_count` | Design doesn't exceed N shapes |
| `geometry.min_feature_size` | No filled detail finer than N mm (needs the geometry extra) |
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
  --choose element.forbidden=delete --choose path.closed=close
# ❌ FAIL → ✅ PASS   9 → 0 error(s)
```

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
image              kind      expect         size   res   mm/px   colours    k    flat   quant   edges    thin
logo-two-colour    logo      good        256x256   200   0.500        12    3   0.957   0.005   0.025   0.003
line-art-thick     line-art  good        256x256   200   0.500         5    3   0.889   0.002   0.069   0.013
line-art-thin      line-art  marginal    256x256   200   0.500         5    3   0.918   0.001   0.060   0.030
hatching           line-art  hopeless    256x256   200   0.500         8    3   0.047   0.382   0.864   0.993
photo-portrait     photo     hopeless    256x256   200   0.500      1356    3   0.000   0.926   0.084   0.080
lowres-icon        junk      marginal      32x32    32   3.125         2    3   0.753   0.000   0.138       ·
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

The columns for path count and "does it pass" are declared but empty: there is
no tracer yet (B0 picks one, B4 wires it in). They are declared now so the
baseline can grade them from the first run that fills them in.

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
| Raster | image → SVG conversion | `pip install "svg-for-embroidery[convert]"` + potrace |

Measuring a **PNG** needs nothing at all — the pure-Python decoder the visual
harness already carries doubles as the corpus reader, so `svgemb bench` works on
a bare install. Pillow adds every other image format; without it a JPEG is a row
you don't get, with the reason printed, and the sweep continues.

There is deliberately **no separate mobile edition** — see the decision in
[`docs/ROADMAP.md`](docs/ROADMAP.md). When a phone can't run a heavy step, run
`svgemb serve --host 0.0.0.0` on a desktop and use it from the phone's browser.

## What is *not* checked

- **Minimum gap size** — the other half of `geometry.min_feature_size`: two
  shapes close enough to bleed into each other once stitched. Same machinery
  (a closing rather than an opening); it lands with the tracer, which needs it
  to keep neighbouring colours from leaving seams.
- **Overlapping shapes / stitch order** — out of scope for a static checker.
- **`<use>` clones** are flagged (via `element.forbidden`) rather than expanded.
- Colours behind `url(#…)` references count as "not a flat colour"; the gradient
  rule reports them instead.

## Development

```bash
make test         # 403 tests
make degraded     # the same suite with every optional extra switched off
make bench        # measure the image corpus against the baseline
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
```
