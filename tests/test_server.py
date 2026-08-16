import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from svg_embroidery.server import MAX_BODY_BYTES, CheckRequestHandler


@pytest.fixture(scope="module")
def base_url():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), CheckRequestHandler)
    httpd.verbose = False
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def get(url):
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.status, response.read().decode("utf-8")


def post_json(url, payload):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


GOOD = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="12cm" height="12cm" viewBox="0 0 120 120">'
    '<g id="a"><path d="M10 10 L110 10 L110 110 L10 110 Z" fill="#000000"/></g></svg>'
)
BAD = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="5cm" height="5cm" viewBox="0 0 50 50">'
    '<text x="1" y="1">hi</text><path d="M0 0 L10 10" fill="#ff0000"/></svg>'
)


def test_index_page_is_self_contained(base_url):
    status, body = get(base_url + "/")
    assert status == 200
    assert "<title>SVG embroidery check</title>" in body
    # no external resources: must work offline on a phone
    assert "http://" not in body.replace('xmlns="http://www.w3.org/2000/svg"', "")
    assert "src=\"//" not in body


def test_profiles_endpoint(base_url):
    status, body = get(base_url + "/api/profiles")
    assert status == 200
    profiles = json.loads(body)
    names = {p["name"] for p in profiles}
    assert "embroidery-basic" in names
    assert all(p["rule_count"] > 0 for p in profiles)


def test_check_passing_file(base_url):
    status, payload = post_json(
        base_url + "/api/check",
        {"svg": GOOD, "filename": "design.svg", "profile": "embroidery-basic"},
    )
    assert status == 200
    assert payload["passed"] is True
    assert payload["file"] == "design.svg"
    assert "PASS" in payload["text"]


def test_check_failing_file(base_url):
    status, payload = post_json(
        base_url + "/api/check", {"svg": BAD, "profile": "embroidery-basic"}
    )
    assert status == 200
    assert payload["passed"] is False
    rules = {f["rule"] for f in payload["findings"] if f["severity"] == "error"}
    assert {"geometry.canvas_size", "element.forbidden", "path.closed"} <= rules


def test_strict_flag_is_honoured(base_url):
    no_viewbox = '<svg xmlns="http://www.w3.org/2000/svg" width="12cm" height="12cm">' \
                 '<path d="M0 0 L10 10 Z" fill="#000"/></svg>'
    _, lenient = post_json(base_url + "/api/check", {"svg": no_viewbox})
    _, strict = post_json(base_url + "/api/check", {"svg": no_viewbox, "strict": True})
    assert lenient["passed"] is True
    assert strict["passed"] is False


def test_profile_selection(base_url):
    _, basic = post_json(base_url + "/api/check", {"svg": GOOD, "profile": "embroidery-basic"})
    _, vinyl = post_json(base_url + "/api/check", {"svg": GOOD, "profile": "plotter-vinyl"})
    assert basic["profile"] == "embroidery-basic"
    assert vinyl["profile"] == "plotter-vinyl"


def test_filename_cannot_escape_to_the_filesystem(base_url):
    status, payload = post_json(
        base_url + "/api/check", {"svg": GOOD, "filename": "../../etc/passwd.svg"}
    )
    assert status == 200
    assert payload["file"] == "passwd.svg"


def test_bad_requests(base_url):
    assert post_json(base_url + "/api/check", {"svg": ""})[0] == 400
    assert post_json(base_url + "/api/check", {"svg": "<svg><oops>"})[0] == 400
    assert post_json(base_url + "/api/check", {"svg": GOOD, "profile": "nope"})[0] == 400

    status, payload = post_json(base_url + "/api/check", {"svg": "not xml at all"})
    assert status == 400 and "parse" in payload["error"].lower()


def test_unknown_routes(base_url):
    for url in (base_url + "/nope", base_url + "/api/nope"):
        with pytest.raises(urllib.error.HTTPError) as exc:
            get(url)
        assert exc.value.code == 404


def test_oversized_body_is_rejected(base_url):
    huge = "x" * (MAX_BODY_BYTES + 1)
    request = urllib.request.Request(
        base_url + "/api/check",
        data=huge.encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request, timeout=10)
    assert exc.value.code == 400
    assert "too large" in json.loads(exc.value.read().decode("utf-8"))["error"]


# -- A6: fixing from the browser -------------------------------------------

FIXABLE = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="5cm" height="5cm">\n'
    '  <g id="a"><path d="M10 10 L40 10 L40 40 L10 40 Z" fill="#000000"/></g>\n'
    "</svg>\n"
)


def test_fix_returns_the_repaired_file_and_the_reasoning(base_url):
    status, payload = post_json(
        base_url + "/api/fix", {"svg": FIXABLE, "filename": "small.svg"}
    )
    assert status == 200
    assert payload["ok"] is True and payload["changed"] is True

    fixed = {fix["rule"] for fix in payload["applied"]}
    assert "geometry.canvas_size" in fixed
    assert payload["before"]["passed"] is False
    assert payload["after"]["passed"] is True
    assert "viewBox=" in payload["svg"]
    assert payload["diff"].startswith("--- a/small.svg")
    assert "✅ fixed" in payload["text"]

    # What comes back is what the browser will download: re-checking it agrees.
    _, rechecked = post_json(base_url + "/api/check", {"svg": payload["svg"]})
    assert rechecked["passed"] is True


def test_fix_defaults_to_safe_and_says_what_it_held_back(base_url):
    _, safe = post_json(base_url + "/api/fix", {"svg": FOUR_COLOURS})
    skipped = {skip["rule"]: skip for skip in safe["skipped"]}
    assert skipped["color.max_count"]["reason"] == "needs --allow lossy"
    assert skipped["color.max_count"]["risk"] == "lossy"

    _, lossy = post_json(base_url + "/api/fix", {"svg": FOUR_COLOURS, "allow": "lossy"})
    assert "color.max_count" in {fix["rule"] for fix in lossy["applied"]}
    assert lossy["after"]["counts"]["error"] < safe["after"]["counts"]["error"]


FOUR_COLOURS = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="12cm" height="12cm" viewBox="0 0 120 120">'
    '<g id="a"><path d="M0 0 L50 0 L50 50 Z" fill="#111111"/>'
    '<path d="M50 0 L100 0 L100 50 Z" fill="#222222"/>'
    '<path d="M0 50 L50 50 L50 100 Z" fill="#333333"/>'
    '<path d="M50 50 L100 50 L100 100 Z" fill="#444444"/></g></svg>'
)


def test_the_browser_cannot_ask_for_a_destructive_fix(base_url):
    """One tap must not delete artwork — that needs a typed command."""
    status, payload = post_json(
        base_url + "/api/fix", {"svg": BAD, "allow": "destructive"}
    )
    assert status == 400
    assert "--allow destructive --only" in payload["error"]

    assert post_json(base_url + "/api/fix", {"svg": BAD, "allow": "reckless"})[0] == 400


def test_fix_rejects_the_same_bad_requests_as_check(base_url):
    assert post_json(base_url + "/api/fix", {"svg": ""})[0] == 400
    assert post_json(base_url + "/api/fix", {"svg": "not xml at all"})[0] == 400
    assert post_json(base_url + "/api/fix", {"svg": GOOD, "profile": "nope"})[0] == 400


def test_the_page_offers_the_fix_and_a_download(base_url):
    _, body = get(base_url + "/")
    assert "Fix what can be fixed" in body
    assert "api/fix" in body
    assert "Download the fixed SVG" in body
    assert "URL.createObjectURL" in body  # the download never leaves the device
