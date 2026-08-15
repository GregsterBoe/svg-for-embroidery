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

svgemb profiles                                # list all rulesets
svgemb profiles example-shop                   # show one ruleset's rules
svgemb rules                                   # list all checks and their parameters
```

Exit codes: `0` pass, `1` the file violates the ruleset, `2` usage/configuration
error (so it drops straight into CI).

As a library:

```python
from svg_embroidery import check_file, render_text

report = check_file("design.svg", profile="embroidery-basic")
print(report.passed(), report.counts())
for finding in report.errors:
    print(finding.rule_id, finding.message, finding.location)
print(render_text(report, verbose=True))
```

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
| `embroidery-strict` | Small/cheap stitching: 8–30 cm, ≤2 colours, no strokes at all, ≤60 shapes |
| `plotter-vinyl` | Cutting plotters: exactly 1 colour, closed contours, filled shapes only |
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
| `path.closed` | Every path *and subpath* ends with `Z` |
| `path.max_count` | Design doesn't exceed N shapes |
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

## What is *not* checked

- **Minimum detail/gap size** — needs real path outline analysis (offsetting),
  not just attribute inspection. `stroke.min_width` covers stroke weight only.
- **Overlapping shapes / stitch order** — out of scope for a static checker.
- **`<use>` clones** are flagged (via `element.forbidden`) rather than expanded.
- Colours behind `url(#…)` references count as "not a flat colour"; the gradient
  rule reports them instead.

## Development

```bash
python -m pytest        # 87 tests
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
  cli.py           svgemb
```
