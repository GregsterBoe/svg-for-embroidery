# B0 — Which tracer?

**Decision: potrace, one mask per colour, with potracer as the fallback where a
binary cannot be installed. VTracer is kept as an option and is not the default.**

The roadmap's instruction for this step was *buy, don't build*, and its
recommendation-to-beat was "potrace, per colour layer". This is the spike that
tested it. The recommendation survived, but not for the reason it was written
down — and the run turned up a better argument for the phase's central claim
than the claim's author had.

Reproduce any number here with:

```
svgemb bench --tracers          # the comparison
svgemb bench --tracer potrace   # one tracer, in the ordinary table
```

## Candidates

| Candidate | What it is | Verdict |
| --- | --- | --- |
| **potrace 1.16** | The reference implementation, a C binary. Reads PBM on stdin, writes SVG on stdout. | **Chosen.** |
| **potracer 0.0.4** | The same algorithm ported to pure Python. Needs numpy. | Fallback. Same curves, 11× slower. |
| **vtracer 0.6.15** | Rust; quantises and traces colour in one step. | Kept as an option, not the default. |
| autotrace | Older, more artefacts. | **Not evaluated: it is not packaged on this distribution at all** (`apt-cache policy autotrace` has no candidate). A tracer nobody ships is not a fallback. |

## The measurement

All three run over the whole B1 corpus, at the resolution each image's profile
asks for, on the same quantised input — so what is being compared is the tracing
and nothing else.

```
tracer           images   paths   nodes     fit  seconds
────────────────────────────────────────────────────────
potrace 1.16         20    6649   72915   0.108     0.47
potracer 0.0.4       20    6791   73883   0.108     5.19
vtracer 0.6.15       20    1472   24337   0.201     0.46
```

`fit` is the share of pixels the traced SVG gets wrong when rendered back and
compared against the image it traced. It is the column that decides, and the
reason the table has four numbers instead of one: **a tracer that draws fewer
paths has either simplified the artwork or thrown it away, and only `fit` says
which.** Read `paths` alone and vtracer wins by a factor of four.

## Why potrace and not potracer

They score identically — 0.108 mean fit, and per image they agree to within
0.003 everywhere. That is the expected result for the same algorithm, and it is
worth stating plainly because it is also the evidence that *both wrappers are
correct*. Two independent implementations of potrace disagreeing would have
meant a bug in one of the wrappers, and during this spike it twice did:

- **The first version scored potracer at 0.310 where potrace got 0.085**, on the
  line-art fixture. The cause was not the tracer. potrace's SVG writer puts every
  outline of a mask into one `<path>`; potracer hands back loose curves, and the
  wrapper was emitting one `<path>` element each. A hole is a subpath wound
  against its container — give it its own element and there is nothing left for
  it to cut a hole in, so every counter in the artwork filled in solid. Both
  backends now emit one path per colour layer, and `test_a_hole_survives_the_trip_through_every_tracer`
  is there so it stays that way.
- **The same bug made the `paths` column meaningless**, in a way that looked
  like a result: potrace reported 1 path for a drawing potracer reported as 17.
  Neither number was wrong; they were counting different things. `paths` is now
  counted as *subpaths of the finished document*, whoever wrote it — the unit
  that survives a house-style difference, and the unit a machine sees as a shape
  to fill.

So the choice between them is not quality. It is:

- **Speed: 0.47s vs 5.19s** for the corpus, and worse than that ratio suggests
  on the hard cases — `hatching` alone takes 42ms in the binary and 2.5s in
  Python. Preprocessing (B3) is going to want to trace the same image several
  times to compare stages, and 11× is the difference between that being
  interactive and being a coffee break.
- **Dependencies: potracer needs numpy, potrace needs nothing.** Which inverts
  the usual argument for a pure-Python fallback. Where potracer earns its place
  is a machine that has numpy but cannot install a binary at all.

Output size was deliberately *not* a criterion. potracer's documents come out
about twice as large, but that measures this project's writer against potrace's
— potrace's own uses relative integer coordinates at 10× scale — not one tracer
against the other. (Coordinate precision was pinned at two decimals for the
comparison, which costs nothing measurable: `fit` is unchanged to three
decimals.)

## Why not vtracer, and the interesting part

vtracer is genuinely good at what it is for. It is as fast as the binary, draws
a quarter of the paths, and its output is the cleanest of the three on
photographs. It also fits the corpus about **half as well overall** — and its
losses are not spread evenly:

```
fit by image        potrace   vtracer
line-art-thick        0.051     0.135
line-art-thin         0.036     0.224     ← 6x worse
hatching              0.571     0.994     ← erases it entirely
lowres-icon           0.125     0.433
scan-clean            0.250     0.030     ← 8x better
scan-noisy            0.425     0.848
photo-portrait        0.062     0.047
```

The bad rows are line art, which is the artwork this project exists to convert.
vtracer's speckle filtering and colour clustering are tuned for photographs, and
on a thin stroke they do not simplify it, they delete it. That alone settles the
default.

**But look at `scan-clean`: vtracer is eight times better there.** A clean line
drawing photographed on paper is the case where potrace is *worst* — grain means
no two neighbouring pixels are equal, so it faithfully traces the noise, 919
paths of it. vtracer smooths the grain away before it traces and gets a good
result from an input potrace chokes on.

That is not an argument for vtracer. It is the strongest evidence in this spike
for the phase's own thesis — **quality comes from preprocessing, not from the
tracer**. vtracer beats potrace on scans because it preprocesses, and loses
everywhere else because its preprocessing is fixed and aimed at photographs.
Doing that step ourselves, tuned for embroidery, is exactly what B3 is, and this
result says how much is on the table: the gap between 0.250 and 0.030 on
`scan-clean` is B3's to close, with a tracer that keeps the line art intact.

## A B1 prediction, checked

B1 shipped `edges` — the share of pixels on a colour boundary — described as "a
stand-in for path count", with no way to verify it. The first real trace can:

| against | correlation |
| --- | --- |
| `edges` vs `paths` | **0.936** |
| `edges` vs `nodes` | **0.991** |

The stand-in was honest, and it is a better predictor of node count than of path
count — which makes sense, since a boundary pixel is closer to being a vertex
than to being a shape. Worth keeping: `edges` costs nothing and needs no tracer,
so it stays the number that a machine with no tracer installed can still use to
predict what conversion will cost.

## What this leaves for later

- **vtracer's output is structurally wrong for us** — a flat list of `<path>`
  elements with no colour groups, and it picks its own colours regardless of the
  profile's budget. Both are reported as notes on the run rather than silently
  fixed. If it is ever wanted as more than an option, B4 would have to regroup
  its output by fill. *(B4 declined: regrouping would make the output look
  profile-shaped while it still ignores the colour budget, which is a worse kind
  of wrong than being honestly unsuitable. It gained a third note instead — it
  draws its own colour boundaries, so B4's seam overlap does not apply to it.)*
- **`passes` is still empty**, and deliberately so: whether a traced document
  survives its own profile is B6's question, because the interesting part is the
  *retry* — lower `k`, simplify harder — not the single check. Worth recording
  that the single check already works, though. Tracing `logo-two-colour` and
  running `svgemb check -p embroidery-basic` over the result gives
  `✅ PASS: 0 error(s), 0 warning(s), 13 check(s) passed` with no cleanup
  whatsoever. One image is not a result, but it does say B5 and B6 start from
  something closer to passing than the roadmap assumed.
- **The mask backends already produce the layered structure Phase A wants**:
  one group per colour, largest area first, `fill` on the group. That was not
  free — it is what the wrapper does instead of the tracer — but it means B4
  starts from a document that `structure.color_layers` already accepts. *(What
  it did not produce was a document without hairline gaps between those groups:
  2% of a typical image, 27% of `hatching`. That is what B4 turned out to be
  about — see the roadmap.)*
