"""A tiny local web UI: ``svgemb serve``.

Standard library only (``http.server``), so it runs anywhere Python does —
including Termux on Android, where there is no compiler for native wheels.

Endpoints:
    GET  /               the single-page UI (self contained, works offline)
    GET  /api/profiles   available rulesets
    POST /api/check      {svg, filename, profile, strict} -> report JSON
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .checker import Checker
from .document import SvgParseError
from .profiles import ProfileError, list_profiles, load_profile
from .report import render_text
from .rules import RuleConfigError

#: Refuse absurd uploads; embroidery SVGs are tiny.
MAX_BODY_BYTES = 8 * 1024 * 1024
#: An over-sized body is discarded in chunks (never buffered) so the client can
#: finish sending and actually receive the error. Past this, drop the connection.
DRAIN_LIMIT = 32 * 1024 * 1024
_CHUNK = 64 * 1024

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
h1 { font-size: 1.25rem; margin: 0 0 4px; }
.sub { color: var(--muted); font-size: .85rem; margin-bottom: 16px; }
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
  text-align: center; color: var(--muted); margin-bottom: 12px;
}
#drop.over { border-color: var(--accent); color: var(--fg); }
#drop strong { color: var(--fg); display: block; margin-bottom: 4px; }
input[type=file] { display: none; }
.row { display: flex; gap: 10px; align-items: center; margin-top: 12px; }
.row label { margin: 0; text-transform: none; letter-spacing: 0; font-size: .9rem; color: var(--fg); }
.verdict { border-radius: 12px; padding: 13px 14px; font-weight: 600; margin-bottom: 12px; }
.verdict.pass { background: color-mix(in srgb, var(--ok) 16%, transparent); color: var(--ok); }
.verdict.fail { background: color-mix(in srgb, var(--err) 16%, transparent); color: var(--err); }
.f { border-left: 3px solid var(--line); padding: 8px 0 8px 12px; margin: 12px 0; }
.f.error { border-color: var(--err); }
.f.warning { border-color: var(--warn); }
.f.info { border-color: var(--ok); }
.f .msg { font-weight: 500; }
.f .meta { color: var(--muted); font-size: .78rem; margin-top: 3px;
           font-family: ui-monospace, SFMono-Regular, Menlo, monospace; word-break: break-all; }
.f .hint { font-size: .86rem; margin-top: 5px; }
.preview { text-align: center; background: #fff; border-radius: 9px; padding: 12px; }
.preview img { max-width: 100%; max-height: 240px; }
.hidden { display: none; }
footer { color: var(--muted); font-size: .78rem; text-align: center; margin-top: 20px; }
</style>
</head>
<body>
<h1>SVG embroidery check</h1>
<div class="sub" id="host"></div>

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
    <strong>Tap to choose an SVG</strong>
    or drop a file here
  </div>
  <input type="file" id="file" accept=".svg,image/svg+xml">
  <div class="preview hidden" id="preview-box"><img id="preview" alt="design preview"></div>
</div>

<div id="out"></div>
<footer>svgemb &middot; running locally on your device</footer>

<script>
const $ = (id) => document.getElementById(id);
let profiles = [], lastSvg = null, lastName = null;

fetch('api/profiles').then(r => r.json()).then(data => {
  profiles = data;
  $('profile').innerHTML = data
    .map(p => `<option value="${p.name}">${p.title || p.name}</option>`).join('');
  describe();
});

function describe() {
  const p = profiles.find(p => p.name === $('profile').value);
  $('profile-desc').textContent = p ? `${p.name} — ${p.rule_count} rules. ${p.description || ''}` : '';
}

$('profile').addEventListener('change', () => { describe(); if (lastSvg) check(); });
$('strict').addEventListener('change', () => { if (lastSvg) check(); });
$('showall').addEventListener('change', () => { if (lastSvg) render(window.lastReport); });

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

function load(file) {
  lastName = file.name;
  const reader = new FileReader();
  reader.onload = () => {
    lastSvg = reader.result;
    // Rendered as an <img> data URI: scripts inside the file cannot run.
    $('preview').src = 'data:image/svg+xml;base64,' + btoa(unescape(encodeURIComponent(lastSvg)));
    $('preview-box').classList.remove('hidden');
    check();
  };
  reader.readAsText(file);
}

function check() {
  $('out').innerHTML = '<div class="card">Checking…</div>';
  fetch('api/check', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      svg: lastSvg, filename: lastName,
      profile: $('profile').value, strict: $('strict').checked
    })
  })
  .then(async r => ({ok: r.ok, body: await r.json()}))
  .then(({ok, body}) => {
    if (!ok) {
      $('out').innerHTML = `<div class="verdict fail">${escapeHtml(body.error || 'Check failed')}</div>`;
      return;
    }
    window.lastReport = body;
    render(body);
  })
  .catch(err => {
    $('out').innerHTML = `<div class="verdict fail">${escapeHtml(String(err))}</div>`;
  });
}

function render(report) {
  if (!report) return;
  const showAll = $('showall').checked;
  const shown = report.findings.filter(f => showAll || f.severity !== 'info');
  const c = report.counts;
  let html = `<div class="verdict ${report.passed ? 'pass' : 'fail'}">
    ${report.passed ? '✅ PASS' : '❌ FAIL'} — ${c.error} error(s), ${c.warning} warning(s),
    ${c.info} passed</div><div class="card">`;
  if (!shown.length) html += '<p>No issues found.</p>';
  const icon = {error: '❌', warning: '⚠️', info: '✅'};
  for (const f of shown) {
    html += `<div class="f ${f.severity}">
      <div class="msg">${icon[f.severity]} ${escapeHtml(f.message)}</div>
      <div class="meta">${escapeHtml(f.rule)}${f.location ? ' · ' + escapeHtml(f.location) : ''}</div>
      ${f.hint && f.severity !== 'info' ? `<div class="hint">→ ${escapeHtml(f.hint)}</div>` : ''}
    </div>`;
  }
  html += '</div><button class="secondary" id="copy">Copy report as text</button>';
  $('out').innerHTML = html;
  $('copy').addEventListener('click', () => {
    navigator.clipboard.writeText(report.text).then(() => {
      $('copy').textContent = 'Copied ✓';
      setTimeout(() => { $('copy').textContent = 'Copy report as text'; }, 1500);
    });
  });
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
            self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/profiles":
            self._send_json(200, self._profiles_payload())
        else:
            self._send_json(404, {"error": "not found"})

    do_HEAD = do_GET  # noqa: N815

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0].rstrip("/") != "/api/check":
            self._send_json(404, {"error": "not found"})
            return

        data, error = self._read_json()
        if error:
            self._send_json(400, {"error": error})
            return
        assert data is not None

        source = data.get("svg")
        if not isinstance(source, str) or not source.strip():
            self._send_json(400, {"error": "no SVG content received"})
            return

        strict = bool(data.get("strict"))
        filename = str(data.get("filename") or "design.svg")
        # Only the name matters for file.extension; never touch the filesystem.
        path = Path(Path(filename).name)

        try:
            # Loaded per request so edits to a profile YAML take effect at once.
            checker = Checker(load_profile(str(data.get("profile") or "embroidery-basic")))
        except (ProfileError, RuleConfigError) as exc:
            self._send_json(400, {"error": str(exc)})
            return

        try:
            report = checker.check_source(source, path=path)
        except SvgParseError as exc:
            self._send_json(400, {"error": f"Could not parse SVG: {exc}"})
            return

        payload = report.to_dict(strict=strict)
        payload["text"] = render_text(report, strict=strict, verbose=True)
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
        httpd.server_close()
