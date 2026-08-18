"""A tiny local web UI: ``svgemb serve``.

Standard library only (``http.server``), so it runs anywhere Python does —
including Termux on Android, where there is no compiler for native wheels.

Endpoints:
    GET  /                 the single-page UI (self contained, works offline)
    GET  /api/profiles     available rulesets
    GET  /api/capabilities what this machine can do — a tracer, or the reason
    POST /api/check        {svg, filename, profile, strict} -> report JSON
    POST /api/fix          {svg, filename, profile, strict, allow} -> fix report
    POST /api/image        {image, filename, profile} -> triage + starting knobs
    POST /api/convert      {key, profile, settings} -> one attempt, and what
                           the loop would try next

Nothing here writes to disk. A fix comes back in the response and the browser
decides whether to download it — the file on the user's phone is never the thing
being edited.

**B7's one addition to that rule is a memory of the image you are working on.**
Converting is not one call: the loop traces up to four times, and the page runs
those one at a time so a phone can show each try as it lands rather than staring
at a spinner through all of them. Re-sending and re-decoding the upload for every
attempt would cost more than the trace, so a decoded source is kept — capped at
two images, dropped when a third arrives, never touching the filesystem. A
browser that asks about an image the server has forgotten is told to send it
again, which it can, because it still has it.
"""

from __future__ import annotations

import hashlib
import json
import threading
from base64 import b64decode
from binascii import Error as Base64Error
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import triage
from .bench import Measurement, measure_raster
from .checker import Checker
from .convert import (
    DEFAULT_TRIES,
    Attempt,
    ConvertError,
    Conversion,
    Settings,
    clamp,
    initial_settings,
    limits_for,
    one_attempt,
    plan_next,
    prepare,
)
from .document import SvgParseError
from .fixes import FixEngine, FixerError, Risk, answer_from_mapping, risks_up_to
from .profiles import Profile, ProfileError, list_profiles, load_profile
from .raster import MAX_PIXELS, RasterError, load_bytes, readable_suffixes
from .report import render_fix_text, render_text
from .rules import RuleConfigError
from .tracer import INSTALL_HINT as TRACER_INSTALL_HINT
from .tracer import Backend, TracerError, default_backend
from .visual import Raster, available_renderers

#: Refuse absurd uploads; embroidery SVGs are tiny, and an image big enough to
#: matter has already lost the argument with :data:`~svg_embroidery.raster.MAX_PIXELS`.
MAX_BODY_BYTES = 8 * 1024 * 1024

#: The riskiest repair a browser *setting* may switch on. Destructive repairs
#: are still reachable, but only by picking one from a list that says what it
#: costs — there is no switch that turns "delete artwork" on in the background.
MAX_WEB_RISK = Risk.LOSSY
#: An over-sized body is discarded in chunks (never buffered) so the client can
#: finish sending and actually receive the error. Past this, drop the connection.
DRAIN_LIMIT = 32 * 1024 * 1024
_CHUNK = 64 * 1024

#: How many decoded images the server remembers at once. Two, because the
#: realistic session is one image with a second one to compare against, and
#: because a decoded source is the largest thing this process ever holds.
MAX_SESSIONS = 2


# ------------------------------------------------------------- the session

@dataclass
class Session:
    """One uploaded image, decoded once and kept while someone works on it."""

    key: str
    name: str
    source: Raster
    #: Held for the length of an attempt. Tracing is the slow part, and two
    #: attempts appending to one conversion at the same time would produce a
    #: list of tries in an order nobody asked for.
    lock: threading.Lock = field(default_factory=threading.Lock)
    #: The pixels the pipeline works from, per working resolution. The
    #: resolution comes from the profile, so this holds one entry per profile
    #: the page has asked about — and re-deriving it per attempt would redo a
    #: box filter over the whole source every time a slider moves.
    work: Dict[int, Raster] = field(default_factory=dict)
    conversion: Optional[Conversion] = None
    #: The profile the attempts above were made for. A different one is a
    #: different question, so the attempts start again rather than mixing.
    conversion_profile: str = ""

    def working(self, settings: Settings) -> Raster:
        if settings.work_side not in self.work:
            self.work[settings.work_side] = prepare(self.source, settings)
        return self.work[settings.work_side]


_SESSIONS: "OrderedDict[str, Session]" = OrderedDict()
_SESSIONS_LOCK = threading.Lock()


def remember(name: str, source: Raster, data: bytes) -> Session:
    """Keep a decoded image under a key derived from its own bytes.

    Content-addressed on purpose: the same file uploaded twice is the same
    session, so re-opening a design in a second tab costs no memory and no
    decode.
    """
    key = hashlib.sha256(data).hexdigest()[:16]
    with _SESSIONS_LOCK:
        existing = _SESSIONS.get(key)
        if existing is not None:
            _SESSIONS.move_to_end(key)
            return existing
        session = Session(key=key, name=name, source=source)
        _SESSIONS[key] = session
        while len(_SESSIONS) > MAX_SESSIONS:
            _SESSIONS.popitem(last=False)
        return session


def recall(key: str) -> Optional[Session]:
    with _SESSIONS_LOCK:
        session = _SESSIONS.get(key)
        if session is not None:
            _SESSIONS.move_to_end(key)
        return session


def forget_all() -> None:
    """Drop every remembered image. For tests, and for a tidy shutdown."""
    with _SESSIONS_LOCK:
        _SESSIONS.clear()


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>SVG embroidery check</title>
<style>
:root {
  --bg: #f6f7f9; --card: #fff; --fg: #1a1d21; --muted: #5f6773; --line: #dfe3e8;
  --err: #c8102e; --warn: #a16207; --ok: #15803d; --accent: #2563eb;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14161a; --card: #1c1f24; --fg: #e8eaed; --muted: #9aa3af; --line: #2c3138;
    --err: #ff6b81; --warn: #e2b13c; --ok: #4ade80; --accent: #6ea8fe;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 16px; background: var(--bg); color: var(--fg);
  font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  padding-bottom: max(16px, env(safe-area-inset-bottom));
}
.wrap { max-width: 1180px; margin: 0 auto; }
h1 { font-size: 1.25rem; margin: 0 0 4px; }
.sub { color: var(--muted); font-size: .85rem; margin-bottom: 16px; }
/* One column on a phone, two on anything with room. Same page, same order:
   what you give it on the left, what it made of that on the right. */
.cols { display: grid; gap: 0 16px; grid-template-columns: 1fr; align-items: start; }
@media (min-width: 900px) { .cols { grid-template-columns: minmax(300px, 380px) 1fr; } }
/* Deliberately not sticky: the left column holds the sliders at its foot, and a
   sticky column taller than the window pins its own bottom out of reach. */
.card {
  background: var(--card); border: 1px solid var(--line); border-radius: 12px;
  padding: 14px; margin-bottom: 14px;
}
label { display: block; font-size: .8rem; text-transform: uppercase;
        letter-spacing: .04em; color: var(--muted); margin-bottom: 6px; }
select, button {
  font: inherit; color: var(--fg); background: var(--card);
  border: 1px solid var(--line); border-radius: 9px; padding: 11px 12px; width: 100%;
}
button { background: var(--accent); color: #fff; border-color: transparent; font-weight: 600; }
button:disabled { opacity: .5; }
button.secondary { background: transparent; color: var(--fg); font-weight: 400; }
.desc { color: var(--muted); font-size: .82rem; margin-top: 8px; }
#drop {
  border: 2px dashed var(--line); border-radius: 12px; padding: 26px 14px;
  text-align: center; color: var(--muted);
}
#drop.over { border-color: var(--accent); color: var(--fg); }
#drop strong { color: var(--fg); display: block; margin-bottom: 4px; }
input[type=file] { display: none; }
.row { display: flex; gap: 10px; align-items: center; margin-top: 12px; }
.row label { margin: 0; text-transform: none; letter-spacing: 0; font-size: .9rem; color: var(--fg); }
.verdict { border-radius: 12px; padding: 13px 14px; font-weight: 600; margin-bottom: 12px; }
.verdict.pass { background: color-mix(in srgb, var(--ok) 16%, transparent); color: var(--ok); }
.verdict.fail { background: color-mix(in srgb, var(--err) 16%, transparent); color: var(--err); }
.verdict.warn { background: color-mix(in srgb, var(--warn) 18%, transparent); color: var(--warn); }
.verdict.busy { background: color-mix(in srgb, var(--accent) 14%, transparent); color: var(--accent); }
.f { border-left: 3px solid var(--line); padding: 8px 0 8px 12px; margin: 12px 0; }
.f.error { border-color: var(--err); }
.f.warning { border-color: var(--warn); }
.f.info { border-color: var(--ok); }
.f .msg { font-weight: 500; }
.f .meta { color: var(--muted); font-size: .78rem; margin-top: 3px;
           font-family: ui-monospace, SFMono-Regular, Menlo, monospace; word-break: break-all; }
.f .hint { font-size: .86rem; margin-top: 5px; }
.preview { text-align: center; background: #fff; border-radius: 9px; padding: 12px; margin-top: 12px; }
.preview img { max-width: 100%; max-height: 240px; }
.hidden { display: none; }
.risk { font-size: .72rem; text-transform: uppercase; letter-spacing: .05em;
        border: 1px solid var(--line); border-radius: 5px; padding: 1px 5px; color: var(--muted); }
.ba { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.ba figure { margin: 0; min-width: 0; }
.ba figcaption { font-size: .74rem; text-transform: uppercase; letter-spacing: .04em;
                 color: var(--muted); text-align: center; margin-bottom: 5px; }
.ba img { width: 100%; background: #fff; border-radius: 8px; padding: 8px; }
/* B8: a converted document has no background unless something paints one, and
   on white that is invisible — the page composites onto white exactly as
   visual.py does, so a hole and a white fill render identically. The chequer is
   how the fabric shows: what you see through it is what the garment will be. */
.ba img.fabric, .preview img.fabric {
  background-color: #e9e5dd;
  background-image:
    linear-gradient(45deg, #d3ccc0 25%, transparent 25%, transparent 75%, #d3ccc0 75%),
    linear-gradient(45deg, #d3ccc0 25%, transparent 25%, transparent 75%, #d3ccc0 75%);
  background-size: 16px 16px;
  background-position: 0 0, 8px 8px;
}
.ba select { padding: 6px 8px; font-size: .8rem; margin-bottom: 6px; }
.stack > button { margin-top: 8px; }
.ask { border: 1px solid var(--accent); border-radius: 12px; padding: 14px;
       margin-bottom: 14px; background: var(--card); }
.ask h3 { margin: 0 0 4px; font-size: 1rem; }
.ask .ctx { color: var(--muted); font-size: .84rem; margin-bottom: 12px; }
.opt { width: 100%; text-align: left; background: transparent; color: var(--fg);
       border: 1px solid var(--line); font-weight: 400; margin-top: 8px; }
.opt b { display: block; font-weight: 600; margin-bottom: 3px; }
.opt small { color: var(--muted); display: block; margin-top: 4px; }
.opt .risk { float: right; margin-left: 8px; }
.opt.danger { border-color: var(--err); }
.opt.danger .risk { color: var(--err); border-color: var(--err); }
.stale { cursor: pointer; font-weight: 400; }
h2 { font-size: .8rem; text-transform: uppercase; letter-spacing: .04em;
     color: var(--muted); margin: 0 0 10px; font-weight: 600; }
/* the knobs the loop turns, as things you can turn */
.knob { margin-bottom: 16px; }
.knob .top { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
.knob .val { font-weight: 600; font-variant-numeric: tabular-nums; }
.knob input[type=range] { width: 100%; margin: 6px 0 0; accent-color: var(--accent);
                          height: 28px; touch-action: pan-y; }
.knob .cost { color: var(--muted); font-size: .78rem; }
.knob.fixed .val { color: var(--muted); }
/* one row per try, and the reason it led to the next one */
.try { display: flex; gap: 10px; align-items: baseline; width: auto; text-align: left;
       background: transparent; color: var(--fg); border: 1px solid var(--line);
       font-weight: 400; margin-top: 8px; }
.try.on { border-color: var(--accent); }
.try .n { color: var(--muted); font-size: .78rem; white-space: nowrap; }
.try .what { flex: 1; min-width: 0; }
.try .rules { color: var(--muted); font-size: .78rem;
              font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
              word-break: break-all; }
.why { color: var(--muted); font-size: .82rem; margin: 6px 0 0 14px;
       border-left: 2px solid var(--line); padding-left: 10px; }
.spin { display: inline-block; width: 13px; height: 13px; border-radius: 50%;
        border: 2px solid currentColor; border-right-color: transparent;
        animation: turn .8s linear infinite; vertical-align: -1px; margin-right: 6px; }
@keyframes turn { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spin { animation-duration: 3s; } }
.note { color: var(--muted); font-size: .82rem; margin-top: 6px; }
footer { color: var(--muted); font-size: .78rem; text-align: center; margin-top: 20px; }
</style>
</head>
<body>
<div class="wrap">
<h1>SVG embroidery check</h1>
<div class="sub" id="host"></div>

<div class="verdict fail stale hidden" id="stale">
  ⚠️ This tab is running an older svgemb than the one answering it — anything new
  is missing here. <u>Tap to reload.</u>
</div>
<div class="verdict fail hidden" id="crash"></div>

<div class="cols">
<div class="col-in">
  <div class="card">
    <label for="profile">Ruleset</label>
    <select id="profile"></select>
    <div class="desc" id="profile-desc"></div>
    <div class="row">
      <input type="checkbox" id="strict" style="width:auto">
      <label for="strict">Treat warnings as failures</label>
    </div>
    <div class="row">
      <input type="checkbox" id="showall" style="width:auto">
      <label for="showall">Show checks that passed</label>
    </div>
  </div>

  <div class="card">
    <div id="drop">
      <strong id="droptitle">Tap to choose a file</strong>
      <span id="dropsub">an SVG to check, or an image to convert</span>
    </div>
    <input type="file" id="file">
    <div class="preview hidden" id="preview-box"><img id="preview" alt="design preview"></div>
    <div class="desc hidden" id="sourcenote"></div>
  </div>

  <div id="triage"></div>
  <div id="knobs"></div>
</div>

<div class="col-out">
  <div id="out"></div>
  <div id="fixout"></div>
  <div id="convertout"></div>
</div>
</div>

<footer>svgemb &middot; running locally on your device</footer>
</div>

<script>
const $ = (id) => document.getElementById(id);
let profiles = [], caps = null;
let mode = null;                       // 'svg' or 'image'
let lastSvg = null, lastName = null;   // the SVG journey (A6/A7)
let image = null;                      // the conversion journey (B7)

// An exception thrown while wiring up buttons used to be invisible: the page
// kept its layout, the handlers were never attached, and tapping did nothing
// at all. A dead button that says why beats a dead button that doesn't.
function crashed(message) {
  $('crash').textContent = '⚠️ Something in this page broke, so parts of it will '
    + 'not respond: ' + message + ' — reload to start again.';
  $('crash').classList.remove('hidden');
}
window.addEventListener('error', e => crashed(e.message));
window.addEventListener('unhandledrejection', e => crashed(String(e.reason)));

// A tab left open across a server restart keeps running the JavaScript it was
// loaded with, so a UI that gained a feature is missing it here and nothing
// says why. Every response carries the build that served it; when it stops
// matching ours, say so instead of quietly behaving like the old version.
const BUILD = '__SVGEMB_BUILD__';
function fresh(response) {
  const served = response.headers.get('X-Svgemb-Build');
  if (served && served !== BUILD) $('stale').classList.remove('hidden');
  return response;
}
$('stale').addEventListener('click', () => location.reload());

function post(route, body) {
  return fetch(route, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  }).then(fresh).then(async r => ({ok: r.ok, status: r.status, body: await r.json()}));
}

fetch('api/profiles').then(fresh).then(r => r.json()).then(data => {
  profiles = data;
  $('profile').innerHTML = data
    .map(p => `<option value="${p.name}">${p.title || p.name}</option>`).join('');
  describe();
});

fetch('api/capabilities').then(fresh).then(r => r.json()).then(data => {
  caps = data;
  $('file').accept = ['.svg', 'image/svg+xml'].concat(data.formats).join(',');
  if (!data.tracer) {
    $('dropsub').textContent = 'an SVG to check — converting images needs a tracer';
  } else {
    $('dropsub').textContent = 'an SVG to check, or an image to convert to one';
  }
});

function describe() {
  const p = profiles.find(p => p.name === $('profile').value);
  $('profile-desc').textContent = p ? `${p.name} — ${p.rule_count} rules. ${p.description || ''}` : '';
}

$('profile').addEventListener('change', () => {
  describe();
  if (mode === 'svg' && lastSvg) check();
  if (mode === 'image' && image) assess();     // the profile is what triage grades against
});
$('strict').addEventListener('change', () => { if (mode === 'svg' && lastSvg) check(); });
$('showall').addEventListener('change', () => {
  if (mode === 'svg' && lastSvg) render(window.lastReport);
  if (mode === 'image' && image) renderConvert();
});

$('drop').addEventListener('click', () => $('file').click());
$('file').addEventListener('change', e => { if (e.target.files[0]) load(e.target.files[0]); });

['dragenter', 'dragover'].forEach(ev => $('drop').addEventListener(ev, e => {
  e.preventDefault(); $('drop').classList.add('over');
}));
['dragleave', 'drop'].forEach(ev => $('drop').addEventListener(ev, e => {
  e.preventDefault(); $('drop').classList.remove('over');
}));
$('drop').addEventListener('drop', e => {
  if (e.dataTransfer.files[0]) load(e.dataTransfer.files[0]);
});

function clearAll() {
  ['out', 'fixout', 'convertout', 'triage', 'knobs'].forEach(id => $(id).innerHTML = '');
  $('sourcenote').classList.add('hidden');
}

function isSvg(file) {
  return file.type === 'image/svg+xml' || /\\.svg$/i.test(file.name);
}

function load(file) {
  clearAll();
  lastName = file.name;
  const reader = new FileReader();
  if (isSvg(file)) {
    mode = 'svg';
    image = null;
    reader.onload = () => {
      lastSvg = reader.result;
      // Rendered as an <img> data URI: scripts inside the file cannot run.
      $('preview').src = dataUri(lastSvg);
      $('preview-box').classList.remove('hidden');
      check();
    };
    reader.readAsText(file);
    return;
  }
  mode = 'image';
  lastSvg = null;
  reader.onload = () => {
    const url = reader.result;                       // data:<type>;base64,<data>
    image = {name: file.name, url: url, data: url.slice(url.indexOf(',') + 1),
             key: null, attempts: [], shown: -1, left: 'source'};
    $('preview').src = url;
    $('preview-box').classList.remove('hidden');
    assess();
  };
  reader.readAsDataURL(file);
}

function dataUri(svg) {
  return 'data:image/svg+xml;base64,' + btoa(unescape(encodeURIComponent(svg)));
}

// ------------------------------------------------------------- the SVG journey

function check() {
  $('out').innerHTML = '<div class="card"><span class="spin"></span>Checking…</div>';
  $('fixout').innerHTML = '';
  post('api/check', {
    svg: lastSvg, filename: lastName,
    profile: $('profile').value, strict: $('strict').checked
  }).then(({ok, body}) => {
    if (!ok) {
      $('out').innerHTML = `<div class="verdict fail">${escapeHtml(body.error || 'Check failed')}</div>`;
      return;
    }
    window.lastReport = body;
    render(body);
  }).catch(err => {
    $('out').innerHTML = `<div class="verdict fail">${escapeHtml(String(err))}</div>`;
  });
}

function findingsHtml(report) {
  const showAll = $('showall').checked;
  const shownFindings = report.findings.filter(f => showAll || f.severity !== 'info');
  const icon = {error: '❌', warning: '⚠️', info: '✅'};
  if (!shownFindings.length) return '<p>No issues found.</p>';
  return shownFindings.map(f => `<div class="f ${f.severity}">
      <div class="msg">${icon[f.severity]} ${escapeHtml(f.message)}</div>
      <div class="meta">${escapeHtml(f.rule)}${f.location ? ' · ' + escapeHtml(f.location) : ''}</div>
      ${f.hint && f.severity !== 'info' ? `<div class="hint">→ ${escapeHtml(f.hint)}</div>` : ''}
    </div>`).join('');
}

function verdictHtml(report) {
  const c = report.counts;
  return `<div class="verdict ${report.passed ? 'pass' : 'fail'}">
    ${report.passed ? '✅ PASS' : '❌ FAIL'} — ${c.error} error(s), ${c.warning} warning(s),
    ${c.info} passed</div>`;
}

function render(report) {
  if (!report) return;
  let html = verdictHtml(report) + '<div class="card">' + findingsHtml(report) + '</div>';
  if (!report.passed || report.counts.warning) {
    html += `<div class="card stack">
      <div class="row">
        <input type="checkbox" id="lossy" style="width:auto">
        <label for="lossy">Also allow repairs that change the design</label>
      </div>
      <button id="fix">Fix what can be fixed</button>
      <div class="desc">Your file is never modified — you get a new one to download.
        Repairs that delete artwork are offered as a choice, never as a setting.</div>
    </div>`;
  }
  html += '<button class="secondary" id="copy">Copy report as text</button>';
  $('out').innerHTML = html;
  $('copy').addEventListener('click', () => {
    navigator.clipboard.writeText(report.text).then(() => {
      $('copy').textContent = 'Copied ✓';
      setTimeout(() => { $('copy').textContent = 'Copy report as text'; }, 1500);
    });
  });
  if ($('fix')) $('fix').addEventListener('click', () => {
    answers = {};                       // a fresh run starts with no answers
    lossyWanted = $('lossy').checked;
    fix();
  });
}

// Questions this file has already been answered, kept so that answering a
// second one does not undo the first: every run replays all of them.
let answers = {}, lossyWanted = false;

function renderAsk(decision) {
  const options = decision.options.map(o => `
    <button class="opt ${o.risk === 'destructive' ? 'danger' : ''}"
            data-rule="${escapeHtml(decision.rule)}" data-key="${escapeHtml(o.key)}">
      <span class="risk">${escapeHtml(o.risk)}</span>
      <b>${o.recommended ? '★ ' : ''}${escapeHtml(o.label)}</b>
      <small>${escapeHtml(o.detail)}</small>
    </button>`).join('');
  return `<div class="ask">
    <h3>${escapeHtml(decision.question)}</h3>
    <div class="ctx">${escapeHtml(decision.context)} <em>${escapeHtml(decision.rule)}</em></div>
    ${options}
  </div>`;
}

function fix() {
  $('fixout').innerHTML = '<div class="card"><span class="spin"></span>Fixing…</div>';
  post('api/fix', {
    svg: lastSvg, filename: lastName, profile: $('profile').value,
    strict: $('strict').checked, allow: lossyWanted ? 'lossy' : 'safe',
    choices: answers
  }).then(({ok, body}) => {
    if (!ok) {
      $('fixout').innerHTML = `<div class="verdict fail">${escapeHtml(body.error)}</div>`;
      return;
    }
    renderFix(body);
  }).catch(err => {
    $('fixout').innerHTML = `<div class="verdict fail">${escapeHtml(String(err))}</div>`;
  });
}

// Named `result`, not `fix`: the click handlers below close over this scope and
// have to be able to call the global fix() to re-run with a new answer.
function renderFix(result) {
  const before = result.before, after = result.after;
  let html = `<div class="verdict ${after.passed ? 'pass' : 'fail'}">
    ${before.passed ? '✅' : '❌'} → ${after.passed ? '✅ PASS' : '❌ FAIL'} —
    ${before.counts.error} → ${after.counts.error} error(s),
    ${before.counts.warning} → ${after.counts.warning} warning(s)</div>`;

  if (!result.ok) {
    html += `<div class="verdict fail">svgemb caught its own output misbehaving:
      ${escapeHtml(result.verification_error)}. Nothing is offered for download.</div>`;
  }

  // The questions come first: they are the part that needs someone.
  for (const decision of result.pending) html += renderAsk(decision);

  html += '<div class="card">';
  if (!result.applied.length && !result.pending.length) {
    html += '<p>Nothing could be repaired automatically.</p>';
  }
  for (const f of result.applied) {
    html += `<div class="f info"><div class="msg">✅ ${escapeHtml(f.rule)}
      <span class="risk">${escapeHtml(f.risk)}</span></div>` +
      (f.chosen ? `<div class="hint">you chose: ${escapeHtml(f.chosen.label)}</div>` : '') +
      f.changes.map(c => `<div class="hint">${escapeHtml(c.description)}</div>`).join('') +
      '</div>';
  }
  for (const s of result.skipped) {
    html += `<div class="f warning"><div class="msg">⏭ ${escapeHtml(s.rule)}
      ${s.risk ? `<span class="risk">${escapeHtml(s.risk)}</span>` : ''}</div>
      <div class="hint">${escapeHtml(s.reason)}</div></div>`;
  }
  for (const u of result.unmeasured) {
    html += `<div class="f"><div class="msg">ℹ️ ${escapeHtml(u.rule)}</div>
      <div class="hint">${escapeHtml(u.message)}</div></div>`;
  }
  html += `<div class="desc">${result.visual
    ? escapeHtml(result.visual.text)
    : 'No renderer on this machine, so the change was not measured against the image.'}</div>`;
  html += '</div>';

  if (result.changed) {
    html += `<div class="card"><div class="ba">
      <figure><figcaption>before</figcaption><img id="ba-before" alt="before"></figure>
      <figure><figcaption>after</figcaption><img id="ba-after" alt="after"></figure>
    </div></div>`;
  }
  if (result.ok && result.changed) {
    html += '<div class="stack"><button id="download">Download the fixed SVG</button>' +
            '<button class="secondary" id="usefix">Keep going from the fixed file</button></div>';
  }
  $('fixout').innerHTML = html;

  // Answering re-runs the whole fix from the original file, so a choice can be
  // changed by choosing again rather than by starting over.
  for (const button of document.querySelectorAll('.opt')) {
    button.addEventListener('click', () => {
      answers[button.dataset.rule] = button.dataset.key;
      fix();
    });
  }
  if (result.changed) {
    $('ba-before').src = dataUri(lastSvg);
    $('ba-after').src = dataUri(result.svg);
  }
  if ($('download')) {
    $('download').addEventListener('click', () => download(result.svg, '-fixed.svg'));
    $('usefix').addEventListener('click', () => {
      lastSvg = result.svg;
      $('preview').src = dataUri(lastSvg);
      check();
    });
  }
}

// ------------------------------------------------------ the image journey (B7)

function assess() {
  $('triage').innerHTML = '<div class="card"><span class="spin"></span>Measuring…</div>';
  $('convertout').innerHTML = '';
  $('knobs').innerHTML = '';
  image.attempts = [];
  image.shown = -1;
  post('api/image', {
    image: image.data, filename: image.name, profile: $('profile').value
  }).then(({ok, body}) => {
    if (!ok) {
      $('triage').innerHTML = `<div class="verdict fail">${escapeHtml(body.error)}</div>`;
      return;
    }
    image.key = body.key;
    image.settings = body.settings;
    image.limits = body.limits;
    image.maxTries = body.max_tries;
    renderTriage(body);
    renderKnobs();
  }).catch(err => {
    $('triage').innerHTML = `<div class="verdict fail">${escapeHtml(String(err))}</div>`;
  });
}

const BAND = {good: 'pass', marginal: 'warn', hopeless: 'fail'};
const BAND_ICON = {good: '✅', marginal: '⚠️', hopeless: '❌'};

function renderTriage(body) {
  const a = body.assessment;
  let html = '<div class="card"><h2>Is it worth converting?</h2>';
  if (!a.verdict) {
    html += `<div class="verdict warn">${escapeHtml(a.headline)}</div></div>`;
    $('triage').innerHTML = html;
    return;
  }
  html += `<div class="verdict ${BAND[a.verdict]}">${BAND_ICON[a.verdict]}
    ${escapeHtml(a.verdict.toUpperCase())}</div>`;
  // The headline already opens with the size — triage writes it that way.
  html += `<div class="note">${escapeHtml(a.headline)}</div>`;
  for (const r of a.readings) {
    html += `<div class="f ${r.band === 'hopeless' ? 'error' : 'warning'}">
      <div class="msg">${escapeHtml(r.reading)}</div>
      <div class="hint">${escapeHtml(r.says)}</div></div>`;
  }
  if (!a.readings.length) {
    html += '<div class="note">flat colour, crisp edges, nothing finer than the needle.</div>';
  }
  for (const item of a.abstained) {
    html += `<div class="note">ℹ️ ${escapeHtml(item.reading)} did not vote:
      ${escapeHtml(item.abstained)}</div>`;
  }
  if (body.upscaled) html += `<div class="note">ℹ️ ${escapeHtml(body.upscaled)}</div>`;
  html += '</div>';

  if (!caps || !caps.tracer) {
    html += `<div class="card"><div class="verdict warn">No tracer on this machine, so
      images cannot be converted here.</div>
      <div class="desc">${escapeHtml(caps ? caps.tracer_hint : '')}</div>
      <div class="desc">Or run <b>svgemb serve --host 0.0.0.0</b> on a computer that has
      one and open it from this browser — the phone is the client, and nothing else
      changes.</div></div>`;
  } else {
    const warn = a.verdict === 'hopeless'
      ? '<div class="desc">Converting this is unlikely to be worth stitching — the ' +
        'reasons above do not go away in the tracer. You can still try.</div>' : '';
    html += `<div class="card stack"><button id="convert">Convert to SVG</button>${warn}
      <div class="desc">Traced at ${escapeHtml(String(body.settings.work_side))}px,
      up to ${body.max_tries} attempts, each one adjusting a setting this ruleset
      allows. Nothing is written to disk.</div></div>`;
  }
  $('triage').innerHTML = html;
  if ($('convert')) $('convert').addEventListener('click', () => runLoop());
}

// The knobs are the ones the loop turns, so moving one is overriding a retry
// rather than reaching for a setting nobody has thought about.
const KNOBS = [
  {key: 'canvas_cm', label: 'Stitched size', unit: ' cm', step: 0.5,
   min: 'canvas_min_cm', max: 'canvas_max_cm',
   cost: 'costs nothing — the same artwork, sewn larger'},
  {key: 'colors', label: 'Colours', unit: '', step: 1,
   min: 'colors_min', max: 'colors_max',
   cost: 'costs part of the design: a colour dropped is gone'},
  {key: 'speck_area', label: 'Absorb specks under', unit: ' px', step: 1,
   min: 'speck_min', max: 'speck_max',
   cost: 'costs shapes this ruleset already calls too small to sew'}
];

function renderKnobs() {
  if (!image || !image.settings || !caps || !caps.tracer) return;
  const s = image.settings, limits = image.limits;
  let html = '<div class="card"><h2>Settings</h2>';
  for (const knob of KNOBS) {
    const lo = limits[knob.min], hi = limits[knob.max];
    const value = s[knob.key];
    const fixed = hi <= lo;
    html += `<div class="knob ${fixed ? 'fixed' : ''}">
      <div class="top"><label for="k-${knob.key}">${knob.label}</label>
        <span class="val" id="v-${knob.key}">${value}${knob.unit}</span></div>
      <input type="range" id="k-${knob.key}" min="${lo}" max="${hi}" step="${knob.step}"
             value="${value}" ${fixed ? 'disabled' : ''}>
      <div class="cost">${lo}${knob.unit} – ${hi}${knob.unit} · ${knob.cost}</div>
    </div>`;
  }
  if (limits.canvas_open_ended) {
    html += `<div class="note">This ruleset sets no maximum size, so the size slider
      stops where the retry loop would: that end is this page's number, not the shop's.</div>`;
  }
  html += '<button id="retry">Convert with these settings</button>';
  html += `<div class="desc">One attempt, exactly as asked. Values outside what the
    ruleset allows are pulled back, and it says so.</div></div>`;
  $('knobs').innerHTML = html;

  for (const knob of KNOBS) {
    const input = $('k-' + knob.key);
    input.addEventListener('input', () => {
      $('v-' + knob.key).textContent = input.value + knob.unit;
    });
  }
  $('retry').addEventListener('click', () => {
    const wanted = {};
    for (const knob of KNOBS) wanted[knob.key] = parseFloat($('k-' + knob.key).value);
    runOnce(wanted);
  });
  // The card is rebuilt after every attempt — including the ones the loop is
  // still working through — so it has to come back in whatever state the page
  // is in rather than in the state it was written in.
  $('retry').disabled = busy;
}

let busy = false, stopped = false;

function setBusy(on) {
  busy = on;
  for (const id of ['convert', 'retry']) if ($(id)) $(id).disabled = on;
}

// One attempt per request. The loop traces up to four times, and a phone that
// has to run potracer in pure Python takes real seconds over each one — so the
// page asks for them one at a time and shows each as it lands, instead of
// holding one request open and having nothing to say for a minute.
async function convertOnce(settings, reset) {
  const body = {key: image.key, profile: $('profile').value, filename: image.name};
  if (settings) body.settings = settings;
  if (reset) body.reset = true;
  let answer = await post('api/convert', body);
  if (answer.status === 409 && answer.body.reupload) {
    // The server forgot this image (a restart, or a third upload pushed it out).
    // The browser still has it, so send it again rather than making someone else
    // do the remembering.
    const again = await post('api/image', {
      image: image.data, filename: image.name, profile: $('profile').value
    });
    if (!again.ok) return again;
    image.key = again.body.key;
    body.key = image.key;
    answer = await post('api/convert', body);
  }
  return answer;
}

function pending(what, stoppable) {
  $('convertout').innerHTML = renderAttempts()
    + `<div class="verdict busy"><span class="spin"></span>${escapeHtml(what)}</div>`
    + (stoppable ? '<button class="secondary" id="stop">Stop after this try</button>' : '');
  if ($('stop')) $('stop').addEventListener('click', () => {
    stopped = true;
    $('stop').textContent = 'Stopping after this try…';
    $('stop').disabled = true;
  });
}

async function runLoop() {
  if (busy) return;
  setBusy(true);
  stopped = false;
  image.attempts = [];
  image.shown = -1;
  let settings = null;                 // null: start where the ruleset starts
  let broke = false;
  try {
    for (let n = 0; n < (image.maxTries || 4); n++) {
      pending(`Tracing try ${n + 1}…`, n > 0 || image.maxTries > 1);
      const {ok, body} = await convertOnce(settings, n === 0);
      if (!ok) {
        broke = true;                  // leave the error on screen, not a redraw
        $('convertout').innerHTML = `<div class="verdict fail">${escapeHtml(body.error)}</div>`;
        return;
      }
      took(body);
      renderConvert();
      if (stopped || !body.next || !body.next.turned || !body.next.settings) return;
      settings = body.next.settings;
    }
  } finally {
    setBusy(false);
    // The loop hands back the attempt that kept the most drawing, not the last
    // one it made — so that is the one the page lands on, exactly as `svgemb
    // convert` does. Every try is still one tap away.
    if (!broke) show(image.bestIndex);
  }
}

function show(index) {
  if (!image.attempts[index]) return;
  image.shown = index;
  image.left = index > 0 ? String(index - 1) : 'source';
  image.settings = image.attempts[index].settings;
  image.limits = image.attempts[index].limits;
  renderKnobs();
  renderConvert();
}

async function runOnce(settings) {
  if (busy) return;
  setBusy(true);
  try {
    pending('Tracing…');
    const {ok, body} = await convertOnce(settings);
    if (!ok) {
      $('convertout').innerHTML = `<div class="verdict fail">${escapeHtml(body.error)}</div>`;
      return;
    }
    took(body);
    renderConvert();
  } finally {
    setBusy(false);
  }
}

function took(body) {
  image.attempts[body.index] = body;
  image.attempts.length = body.tries;
  image.shown = body.index;
  image.settings = body.settings;
  image.limits = body.limits;
  image.bestIndex = body.best_index;
  // The left pane defaults to the try before this one: the comparison worth
  // making is 8 cm against 12 cm, not image against SVG.
  image.left = body.index > 0 ? String(body.index - 1) : 'source';
  renderKnobs();
}

function renderAttempts() {
  if (!image.attempts.length) return '';
  let html = '<div class="card"><h2>Attempts</h2>';
  image.attempts.forEach((a, i) => {
    if (!a) return;
    const verdict = a.passes
      ? (a.cut_detail ? '✅ passes, artwork cut' : '✅ passes')
      : `❌ ${a.report.counts.error} error(s)`;
    html += `<button class="try ${i === image.shown ? 'on' : ''}" data-try="${i}">
      <span class="n">try ${i + 1}${i === image.bestIndex ? ' ★' : ''}</span>
      <span class="what">${escapeHtml(a.settings.describe)} — ${verdict}
        ${a.failing.length ? `<span class="rules">${escapeHtml(a.failing.join(', '))}</span>` : ''}
      </span></button>`;
    if (a.next && a.next.says) {
      html += `<div class="why">${a.next.turned ? '↻' : '⏹'} ${escapeHtml(a.next.says)}</div>`;
    }
  });
  html += '</div>';
  return html;
}

function renderConvert() {
  const a = image.attempts[image.shown];
  if (!a) { $('convertout').innerHTML = renderAttempts(); return; }
  let html = renderAttempts();

  html += verdictHtml(a.report);
  if (!a.verified) {
    html += `<div class="verdict fail">svgemb caught its own output misbehaving, so this
      document was discarded rather than offered.</div>`;
  }
  const best = image.attempts[image.bestIndex];
  if (best && image.bestIndex !== image.shown) {
    // Why that one is B6's ordering, said in words: a document that passes
    // beats one that does not, fewer complaints is closer, keeping the whole
    // drawing beats passing by cutting part of it away, and among equals the
    // smallest wins because every retry asked for something.
    const because = best.passes
      ? 'it passes at the smallest size that worked'
      : 'it comes closest, and kept the most of the drawing';
    html += `<div class="note">★ try ${image.bestIndex + 1} is the one svgemb would keep:
      ${because}. You are looking at try ${image.shown + 1}.</div>`;
  }

  html += `<div class="card"><div class="ba">
    <figure>
      <select id="pane-left">${paneOptions(image.left)}</select>
      <img id="ba-left" alt="left">
    </figure>
    <figure>
      <figcaption>try ${image.shown + 1} · ${escapeHtml(a.settings.describe)}</figcaption>
      <img id="ba-right" class="fabric" alt="right">
    </figure>
  </div>
  <div class="note">${a.shapes} shape(s), ${a.nodes} node(s) —
    what the machine has to sew.</div>${unstitchedHtml(a)}</div>`;

  html += '<div class="card"><h2>What the ruleset says about it</h2>'
        + findingsHtml(a.report) + '</div>';

  if (a.clamped.length || a.notes.length) {
    html += '<div class="card"><h2>Notes</h2>'
      + a.clamped.map(n => `<div class="note">⚠️ ${escapeHtml(n)}</div>`).join('')
      + a.notes.map(n => `<div class="note">${escapeHtml(n)}</div>`).join('')
      + '</div>';
  }

  html += '<div class="stack">'
    + (a.verified ? '<button id="get">Download this SVG</button>' : '')
    + '<button class="secondary" id="tocheck">Check and fix this SVG</button></div>';
  $('convertout').innerHTML = html;

  $('ba-right').src = dataUri(a.svg);
  showLeft();
  $('pane-left').addEventListener('change', () => {
    image.left = $('pane-left').value;
    showLeft();
  });
  for (const button of document.querySelectorAll('.try')) {
    button.addEventListener('click', () => show(parseInt(button.dataset.try, 10)));
  }
  if ($('get')) $('get').addEventListener('click', () => download(a.svg, '.svg'));
  $('tocheck').addEventListener('click', () => {
    // Hand the conversion to the checker journey: same page, same buttons, and
    // the repairs that ask are reachable from here too.
    mode = 'svg';
    lastSvg = a.svg;
    lastName = (image.name || 'design').replace(/\\.[^.]+$/, '') + '.svg';
    $('preview').src = dataUri(lastSvg);
    $('triage').innerHTML = '';
    $('knobs').innerHTML = '';
    $('convertout').innerHTML = '';
    check();
  });
}

function paneOptions(selected) {
  let html = `<option value="source"${selected === 'source' ? ' selected' : ''}>the image</option>`;
  image.attempts.forEach((a, i) => {
    if (!a) return;
    html += `<option value="${i}"${selected === String(i) ? ' selected' : ''}>try ${i + 1} ·
      ${escapeHtml(a.settings.describe)}</option>`;
  });
  return html;
}

function unstitchedHtml(a) {
  // B8: the hole is the point, so it is said in words as well as drawn. The
  // chequer behind the preview is the fabric — without it a dropped background
  // and a white one look identical, which is the whole trap this step is in.
  if (!a.unstitched) return '';
  const share = (a.unstitched.share * 100).toFixed(0);
  return `<div class="note">🧵 ${escapeHtml(a.unstitched.color)} is the ground, not a
    thread: ${share}% of the design is left unstitched, so the garment shows through
    there. The chequer behind the preview is that fabric. Convert with
    <code>--keep-background</code> to stitch it instead, at the cost of one colour.</div>`;
}

function showLeft() {
  const which = image.left;
  const source = which === 'source';
  $('ba-left').src = source ? image.url : dataUri(image.attempts[parseInt(which, 10)].svg);
  // The source is a photograph of something; only a converted document has a
  // hole in it, so only that pane gets the fabric behind it.
  $('ba-left').classList.toggle('fabric', !source);
}

// ---------------------------------------------------------------- shared

function download(svg, suffix) {
  const url = URL.createObjectURL(new Blob([svg], {type: 'image/svg+xml'}));
  const link = document.createElement('a');
  link.href = url;
  link.download = (lastName || image && image.name || 'design.svg')
    .replace(/\\.[^.]+$/, '') + suffix;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, ch => (
    {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]
  ));
}
$('host').textContent = location.host;
</script>
</body>
</html>
"""

#: The placeholder the page carries so it can recognise its own vintage. The
#: stamp is hashed from the template *before* substitution, so it does not
#: depend on itself and stays identical across restarts of unchanged code.
_BUILD_TOKEN = "__SVGEMB_BUILD__"
BUILD = hashlib.sha256(INDEX_HTML.encode("utf-8")).hexdigest()[:12]
_PAGE = INDEX_HTML.replace(_BUILD_TOKEN, BUILD).encode("utf-8")


class CheckRequestHandler(BaseHTTPRequestHandler):
    server_version = "svgemb"
    protocol_version = "HTTP/1.1"

    # -- plumbing ----------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        if status >= 400:  # never keep a connection alive after an error
            self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if status >= 400:
            self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Svgemb-Build", BUILD)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _read_json(self) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None, "invalid Content-Length"
        if length <= 0:
            return None, "empty request body"
        if length > MAX_BODY_BYTES:
            self._drain(length)
            return None, f"file too large (limit {MAX_BODY_BYTES // (1024 * 1024)} MB)"
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return None, f"invalid request body: {exc}"
        if not isinstance(data, dict):
            return None, "request body must be a JSON object"
        return data, None

    def _drain(self, length: int) -> None:
        """Read and throw away a rejected body, in bounded chunks."""
        remaining = min(length, DRAIN_LIMIT)
        while remaining > 0:
            chunk = self.rfile.read(min(_CHUNK, remaining))
            if not chunk:
                return
            remaining -= len(chunk)

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter default log
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    # -- routes ------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/":
            self._send(200, _PAGE, "text/html; charset=utf-8")
        elif path == "/api/profiles":
            self._send_json(200, self._profiles_payload())
        elif path == "/api/capabilities":
            self._send_json(200, self._capabilities_payload())
        else:
            self._send_json(404, {"error": "not found"})

    do_HEAD = do_GET  # noqa: N815

    def do_POST(self) -> None:  # noqa: N802
        route = self.path.split("?", 1)[0].rstrip("/")
        if route in ("/api/image", "/api/convert"):
            self._do_raster_route(route)
            return
        if route not in ("/api/check", "/api/fix"):
            self._send_json(404, {"error": "not found"})
            return

        request = self._read_request()
        if isinstance(request, str):
            self._send_json(400, {"error": request})
            return
        source, path, profile, strict, data = request

        if route == "/api/check":
            self._do_check(source, path, profile, strict)
        else:
            self._do_fix(source, path, profile, strict, data)

    def _read_request(self):
        """Everything both SVG routes need, or an error string."""
        data, error = self._read_json()
        if error:
            return error
        assert data is not None

        source = data.get("svg")
        if not isinstance(source, str) or not source.strip():
            return "no SVG content received"

        # Only the name matters, for file.extension; never touch the filesystem.
        path = Path(Path(str(data.get("filename") or "design.svg")).name)
        profile = self._profile_from(data)
        if isinstance(profile, str):
            return profile
        return source, path, profile, bool(data.get("strict")), data

    @staticmethod
    def _profile_from(data: Dict[str, Any]):
        try:
            # Loaded per request so edits to a profile YAML take effect at once.
            profile = load_profile(str(data.get("profile") or "embroidery-basic"))
            Checker(profile)  # surfaces a bad parameter as a message, not a 500
        except (ProfileError, RuleConfigError) as exc:
            return str(exc)
        return profile

    def _do_check(self, source: str, path: Path, profile: Profile, strict: bool) -> None:
        try:
            report = Checker(profile).check_source(source, path=path)
        except SvgParseError as exc:
            self._send_json(400, {"error": f"Could not parse SVG: {exc}"})
            return
        payload = report.to_dict(strict=strict)
        payload["text"] = render_text(report, strict=strict, verbose=True)
        self._send_json(200, payload)

    def _do_fix(
        self, source: str, path: Path, profile: Profile, strict: bool, data: Dict[str, Any]
    ) -> None:
        try:
            allow = risks_up_to(data.get("allow") or Risk.SAFE.value)
        except FixerError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        if any(risk.rank > MAX_WEB_RISK.rank for risk in allow):
            self._send_json(
                400,
                {
                    "error": "There is no destructive *setting* here. A repair that "
                    "deletes artwork is offered as a choice, with what it costs written "
                    "next to it, and runs only when you pick it."
                },
            )
            return

        answers = data.get("choices")
        if not isinstance(answers, dict):
            answers = {}
        answers = {str(key): str(value) for key, value in answers.items()}

        try:
            report = FixEngine(
                profile,
                allow=allow,
                decide=answer_from_mapping(answers) if answers else None,
                # Destructive repairs are reachable here, but only by picking
                # one: ``ask_first`` offers them as answers and blocks any that
                # would run on their own. The checkbox stays a safe/lossy
                # switch, and nothing deletes artwork without being asked to.
                ask_first=(Risk.DESTRUCTIVE,),
            ).fix_source(source, path=path)
        except SvgParseError as exc:
            self._send_json(400, {"error": f"Could not parse SVG: {exc}"})
            return

        payload = report.to_dict(strict=strict, include_source=True, include_diff=True)
        payload["text"] = render_fix_text(report, strict=strict)
        self._send_json(200, payload)

    # -- the conversion journey (B7) ---------------------------------------

    def _do_raster_route(self, route: str) -> None:
        data, error = self._read_json()
        if error:
            self._send_json(400, {"error": error})
            return
        assert data is not None
        profile = self._profile_from(data)
        if isinstance(profile, str):
            self._send_json(400, {"error": profile})
            return
        if route == "/api/image":
            self._do_image(data, profile)
        else:
            self._do_convert(data, profile)

    def _do_image(self, data: Dict[str, Any], profile: Profile) -> None:
        """Decode an upload, remember it, and say what triage makes of it."""
        encoded = data.get("image")
        if not isinstance(encoded, str) or not encoded.strip():
            self._send_json(400, {"error": "no image received"})
            return
        # A browser sends a data: URI when it is easier; take either.
        if "," in encoded[:64] and encoded.lstrip().startswith("data:"):
            encoded = encoded.split(",", 1)[1]
        try:
            raw = b64decode(encoded, validate=True)
        except (Base64Error, ValueError) as exc:
            self._send_json(400, {"error": f"the upload was not readable: {exc}"})
            return

        name = Path(str(data.get("filename") or "image")).name
        try:
            source = load_bytes(raw, name)
        except RasterError as exc:
            self._send_json(400, {"error": str(exc)})
            return

        session = remember(name, source, raw)
        # Assessing is where the page starts, so any attempts from a previous
        # ruleset belong to a question that is no longer being asked.
        with session.lock:
            session.conversion = None
            session.conversion_profile = ""
        row = Measurement(name=name, category="", expect="", profile=profile.name)
        row = measure_raster(source, row, profile)
        assessment = triage.assess(row, name=name)
        settings, upscaled = initial_settings(profile, max(source.width, source.height))
        self._send_json(
            200,
            {
                "key": session.key,
                "name": name,
                "size": f"{source.width}x{source.height}",
                "width": source.width,
                "height": source.height,
                "profile": profile.name,
                "assessment": assessment.to_dict(),
                "text": triage.render_assessment(assessment, verbose=True),
                "settings": _settings_dict(settings),
                "limits": limits_for(profile, settings).to_dict(),
                "upscaled": upscaled,
                "max_tries": DEFAULT_TRIES,
                "tracer": _tracer_name(),
            },
        )

    def _do_convert(self, data: Dict[str, Any], profile: Profile) -> None:
        """Trace once, at the settings asked for, and say what to try next.

        One attempt per request rather than the whole loop, so the page can show
        each try as it lands. The *judgement* stays here — which knob answers
        which complaint, and which attempt is worth keeping — because a browser
        re-implementing that would be a second copy of B6 with no test suite.
        """
        session = recall(str(data.get("key") or ""))
        if session is None:
            self._send_json(
                409,
                {
                    "reupload": True,
                    "error": "this server no longer has that image in memory — "
                    "send it again",
                },
            )
            return

        backend = default_backend()
        if backend is None:
            self._send_json(
                503,
                {
                    "error": "converting an image needs a tracer, and none is installed "
                    f"here — {TRACER_INSTALL_HINT}"
                },
            )
            return

        with session.lock:
            try:
                payload = _attempt_payload(
                    session,
                    profile,
                    backend,
                    data.get("settings"),
                    reset=bool(data.get("reset")),
                )
            except (ConvertError, TracerError) as exc:
                self._send_json(500, {"error": str(exc)})
                return
            except (TypeError, ValueError) as exc:
                self._send_json(400, {"error": f"unusable settings: {exc}"})
                return
        self._send_json(200, payload)

    @staticmethod
    def _profiles_payload() -> Any:
        return [
            {
                "name": profile.name,
                "title": profile.title,
                "description": " ".join(profile.description.split()),
                "url": profile.url,
                "rule_count": len(profile.rules),
            }
            for profile in list_profiles()
        ]

    @staticmethod
    def _capabilities_payload() -> Any:
        """What this machine can do, so the page can say what it cannot.

        Asked of the modules that do the work, the way ``svgemb doctor`` asks:
        a page that advertised a tracer the code will not use would be worse
        than one that offers nothing.
        """
        renderers = available_renderers()
        return {
            "tracer": _tracer_name(),
            "tracer_hint": TRACER_INSTALL_HINT,
            "formats": list(readable_suffixes()),
            "renderer": renderers[0].name if renderers else None,
            "max_mb": MAX_BODY_BYTES // (1024 * 1024),
            "max_megapixels": MAX_PIXELS // 1_000_000,
            "max_tries": DEFAULT_TRIES,
        }


def _tracer_name() -> Optional[str]:
    backend = default_backend()
    return backend.name if backend is not None else None


def _settings_dict(settings: Settings) -> Dict[str, Any]:
    return {
        "canvas_cm": round(settings.canvas_cm, 2),
        "colors": settings.colors,
        "speck_area": settings.speck_area,
        "work_side": settings.work_side,
        "describe": settings.describe(),
    }


def _settings_from(data: Any, current: Settings) -> Settings:
    """The knobs a slider moved, on top of the ones this attempt started with."""
    if not isinstance(data, dict):
        return current
    return replace(
        current,
        canvas_mm=float(data.get("canvas_cm", current.canvas_cm)) * 10.0,
        colors=int(data.get("colors", current.colors)),
        speck_area=int(data.get("speck_area", current.speck_area)),
    )


def _attempt_payload(
    session: Session,
    profile: Profile,
    backend: Backend,
    wanted: Any,
    reset: bool = False,
) -> Dict[str, Any]:
    """Run one attempt for this session and describe it.

    The session keeps the whole :class:`~svg_embroidery.convert.Conversion`, so
    ``best`` stays where B6 put it: an attempt made by hand at a larger size
    does not become "the answer" just by being the most recent one, and the page
    is told which try the loop would keep.
    """
    conversion = session.conversion
    if reset or conversion is None or session.conversion_profile != profile.name:
        conversion = Conversion(
            name=session.name, profile=profile.name, tracer=backend.name
        )
        session.conversion = conversion
        session.conversion_profile = profile.name

    source_side = max(session.source.width, session.source.height)
    start, upscaled = initial_settings(profile, source_side)
    if wanted is None and not conversion.attempts:
        settings = start
    else:
        base = conversion.attempts[-1].settings if conversion.attempts else start
        settings = _settings_from(wanted, base)
    settings, clamped = clamp(settings, profile)

    attempt = one_attempt(session.working(settings), profile, settings, backend)
    conversion.attempts.append(attempt)
    decided = plan_next(attempt, profile)
    index = len(conversion.attempts) - 1
    if decided is not None:
        attempt.adjustment = decided[1]

    next_up: Optional[Dict[str, Any]] = None
    if decided is not None:
        proposed, adjustment = decided
        next_up = {
            "knob": adjustment.knob,
            "because": adjustment.rule_id,
            "says": adjustment.says,
            # A knob with no room left is a stop with a reason, and so is the
            # try budget: past it the loop would keep tracing on its own, and
            # the only honest way on is someone choosing a setting.
            "turned": adjustment.turned and index + 1 < DEFAULT_TRIES,
            "settings": _settings_dict(proposed) if proposed is not None else None,
        }
        if adjustment.turned and index + 1 >= DEFAULT_TRIES:
            next_up["says"] = (
                f"the loop stops itself after {DEFAULT_TRIES} tries. What it would "
                f"turn next: {adjustment.says}"
            )
            next_up["settings"] = None

    return {
        "key": session.key,
        "index": index,
        "tries": conversion.tries,
        "best_index": conversion.best_index,
        "settings": _settings_dict(attempt.settings),
        "limits": limits_for(profile, attempt.settings).to_dict(),
        "clamped": clamped,
        "svg": attempt.svg,
        "passes": attempt.passes,
        "cut_detail": attempt.cut_detail,
        "verified": attempt.cleanup.ok,
        "shapes": attempt.shapes,
        "nodes": attempt.nodes,
        "failing": attempt.failing_rules,
        "seconds": round(attempt.seconds, 2),
        "report": attempt.report.to_dict(),
        "notes": _attempt_notes(attempt, upscaled if index == 0 else ""),
        "unstitched": _unstitched(attempt),
        "next": next_up,
    }


def _attempt_notes(attempt: Attempt, upscaled: str) -> List[str]:
    """What every stage said about this attempt, in the words it said it in."""
    notes = [upscaled] if upscaled else []
    notes.extend(f"{stage.name}: {stage.says}" for stage in attempt.prepared.stages)
    notes.extend(f"trace: {note}" for note in attempt.trace_notes)
    notes.extend(attempt.cleanup.notes())
    return notes


def _unstitched(attempt: Attempt) -> Optional[Dict[str, Any]]:
    """B8's dropped background, for a page that has to make a hole visible.

    The CLI can print "left unstitched" and be understood. A page cannot: it
    renders the document onto whatever is behind it, so a hole where the paper
    was looks exactly like white paint — the same trap ``visual.py`` has, where
    compositing onto white before comparing makes a dropped background score
    zero difference. So the page is told which colour left and how much of the
    picture it was, and previews the result on a ground that is not white.
    """
    index = attempt.prepared.background
    if index is None:
        return None
    quantisation = attempt.prepared.quantisation
    return {
        "color": "#{:02x}{:02x}{:02x}".format(*quantisation.palette[index]),
        "share": round(quantisation.area(index), 4),
    }


def serve(host: str = "127.0.0.1", port: int = 8000, verbose: bool = False) -> None:
    """Run the local web UI until interrupted."""
    httpd = ThreadingHTTPServer((host, port), CheckRequestHandler)
    httpd.verbose = verbose  # type: ignore[attr-defined]
    shown_host = "localhost" if host in ("127.0.0.1", "0.0.0.0", "::") else host
    print(f"svgemb serving on http://{shown_host}:{port}  (Ctrl+C to stop)")
    if host not in ("127.0.0.1", "::1", "localhost"):
        print(f"⚠️  Reachable from your network on port {port} — anyone on this Wi-Fi can use it.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        forget_all()
        httpd.server_close()
