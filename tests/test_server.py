import base64
import json
import pathlib
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from svg_embroidery.geometry import default_backend
from svg_embroidery.server import BUILD, MAX_BODY_BYTES, CheckRequestHandler, recall
from svg_embroidery.tracer import default_backend as tracer_backend


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


EXAMPLES = pathlib.Path(__file__).resolve().parents[1] / "examples"
EXAMPLE_BAD = (EXAMPLES / "bad-design.svg").read_text(encoding="utf-8")
THIN = EXAMPLES / "thin-detail.svg"

FOUR_COLOURS = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="12cm" height="12cm" viewBox="0 0 120 120">'
    '<g id="a"><path d="M0 0 L50 0 L50 50 Z" fill="#111111"/>'
    '<path d="M50 0 L100 0 L100 50 Z" fill="#222222"/>'
    '<path d="M0 50 L50 50 L50 100 Z" fill="#333333"/>'
    '<path d="M50 50 L100 50 L100 100 Z" fill="#444444"/></g></svg>'
)


def test_there_is_no_destructive_setting_in_the_browser(base_url):
    """A7's line: destructive repairs are reachable, but only by picking one.

    A switch that quietly enables "delete artwork" for the whole run is the
    thing that must not exist. A button that says what it deletes is fine —
    better than a flag, because the consequence is written next to it.
    """
    status, payload = post_json(
        base_url + "/api/fix", {"svg": BAD, "allow": "destructive"}
    )
    assert status == 400
    assert "offered as a choice" in payload["error"]

    assert post_json(base_url + "/api/fix", {"svg": BAD, "allow": "reckless"})[0] == 400


@pytest.mark.skipif(
    default_backend() is None, reason="no path geometry backend installed"
)
def test_a_destructive_repair_that_asks_nothing_stays_out_of_the_browser(base_url):
    """``geometry.min_feature_size`` deletes artwork and offers no choice."""
    _, payload = post_json(
        base_url + "/api/fix",
        {"svg": THIN.read_text(encoding="utf-8"), "profile": "embroidery-strict"},
    )
    reasons = {skip["rule"]: skip["reason"] for skip in payload["skipped"]}
    assert "only available from the command line" in reasons["geometry.min_feature_size"]
    assert payload["after"]["passed"] is False


def test_a_choice_can_reach_a_destructive_repair(base_url):
    """...but one that *does* ask can be answered, and then it runs."""
    _, offered = post_json(base_url + "/api/fix", {"svg": BAD})
    questions = {ask["rule"]: ask for ask in offered["pending"]}
    assert "element.forbidden" in questions
    option = questions["element.forbidden"]["options"][0]
    assert option["risk"] == "destructive"
    assert "<text>" in option["label"]

    _, answered = post_json(
        base_url + "/api/fix",
        {"svg": BAD, "choices": {"element.forbidden": option["key"]}},
    )
    applied = {fix["rule"]: fix for fix in answered["applied"]}
    assert applied["element.forbidden"]["risk"] == "destructive"
    assert applied["element.forbidden"]["chosen"]["key"] == option["key"]
    assert "<text" not in answered["svg"]


def test_answering_every_question_gets_the_broken_file_through(base_url):
    """The point of the whole feature, end to end from the browser."""
    _, payload = post_json(
        base_url + "/api/fix",
        {
            "svg": EXAMPLE_BAD,
            "allow": "lossy",
            "choices": {
                "color.no_gradients": "average",
                "structure.color_layers": "colors",
                "element.forbidden": "delete",
                "path.closed": "close",
            },
        },
    )
    assert payload["before"]["passed"] is False
    assert payload["after"]["passed"] is True
    assert payload["ok"] is True and payload["pending"] == []


def test_fix_rejects_the_same_bad_requests_as_check(base_url):
    assert post_json(base_url + "/api/fix", {"svg": ""})[0] == 400
    assert post_json(base_url + "/api/fix", {"svg": "not xml at all"})[0] == 400
    assert post_json(base_url + "/api/fix", {"svg": GOOD, "profile": "nope"})[0] == 400


def test_the_page_renders_the_questions_as_buttons(base_url):
    _, body = get(base_url + "/")
    assert "renderAsk" in body
    assert "answers[button.dataset.rule]" in body  # picking one re-runs the fix
    assert ".opt.danger" in body  # a destructive answer is styled as one
    assert "'destructive' ? 'danger'" in body


def test_the_page_offers_the_fix_and_a_download(base_url):
    _, body = get(base_url + "/")
    assert "Fix what can be fixed" in body
    assert "api/fix" in body
    assert "Download the fixed SVG" in body
    assert "URL.createObjectURL" in body  # the download never leaves the device


# -- B7: converting an image from the browser ------------------------------

CORPUS = pathlib.Path(__file__).resolve().parents[1] / "bench" / "corpus"
#: Too fine to stitch at the smallest size embroidery-strict allows and fine at
#: a larger one — so the loop has something to argue with, in two attempts.
CONVERTIBLE = CORPUS / "line-art-thick.png"

needs_tracer = pytest.mark.skipif(
    tracer_backend() is None, reason="no tracer installed here"
)


def upload(base_url, path=CONVERTIBLE, profile="embroidery-strict"):
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return post_json(
        base_url + "/api/image",
        {"image": encoded, "filename": path.name, "profile": profile},
    )


def test_capabilities_are_asked_of_the_code_that_does_the_work(base_url):
    status, body = get(base_url + "/api/capabilities")
    assert status == 200
    caps = json.loads(body)
    assert caps["tracer"] == (tracer_backend().name if tracer_backend() else None)
    assert ".png" in caps["formats"]  # readable with nothing installed at all
    assert caps["max_megapixels"] and caps["max_tries"]


def test_an_uploaded_image_is_graded_exactly_as_the_command_line_grades_it(base_url):
    """``svgemb assess`` and this page cannot disagree about an image."""
    from svg_embroidery import triage
    from svg_embroidery.bench import measure_file

    status, body = upload(base_url)
    assert status == 200
    expected = triage.assess(measure_file(CONVERTIBLE, profile="embroidery-strict"))
    assert body["assessment"]["verdict"] == expected.verdict.value
    assert body["assessment"]["headline"] == expected.headline()
    assert body["assessment"]["readings"] == [r.to_dict() for r in expected.readings]


def test_the_starting_settings_and_slider_ranges_come_from_the_profile(base_url):
    _, body = upload(base_url)
    assert body["settings"] == {
        "canvas_cm": 8.0, "colors": 2, "speck_area": 1, "work_side": 160,
        "describe": "8.0 cm, 2 colour(s)",
    }
    assert body["limits"]["canvas_min_cm"] == 8.0
    assert body["limits"]["canvas_max_cm"] == 30.0
    assert body["limits"]["colors_max"] == 2

    _, basic = upload(base_url, profile="embroidery-basic")
    assert basic["settings"]["canvas_cm"] == 10.0 and basic["limits"]["colors_max"] == 3


def test_an_upload_that_is_not_an_image_is_a_message_not_a_stack_trace(base_url):
    assert post_json(base_url + "/api/image", {"image": ""})[0] == 400
    status, body = post_json(base_url + "/api/image", {"image": "not base64 @@@"})
    assert status == 400 and "not readable" in body["error"]
    status, body = post_json(
        base_url + "/api/image",
        {"image": base64.b64encode(GOOD.encode()).decode(), "filename": "design.svg"},
    )
    assert status == 400  # an SVG is already a vector; assess takes the image


@needs_tracer
def test_the_browser_drives_the_same_loop_the_command_line_runs(base_url):
    """The gate for B7: the same image, the same profile, the same answer.

    The page asks for one attempt at a time so a phone can show each try as it
    lands. That is a difference in *pacing*, and this proves it is only that —
    the settings tried, the document kept and the verdict all match
    :func:`~svg_embroidery.convert.convert` running the loop by itself.
    """
    from svg_embroidery.convert import convert
    from svg_embroidery.profiles import load_profile
    from svg_embroidery.raster import load_image

    _, start = upload(base_url)
    key, settings = start["key"], None
    attempts = []
    for _ in range(start["max_tries"]):
        payload = {"key": key, "profile": "embroidery-strict"}
        if settings is not None:
            payload["settings"] = settings
        status, attempt = post_json(base_url + "/api/convert", payload)
        assert status == 200
        attempts.append(attempt)
        if not attempt["next"] or not attempt["next"]["turned"]:
            break
        settings = attempt["next"]["settings"]

    expected = convert(load_image(CONVERTIBLE), load_profile("embroidery-strict"))
    assert len(attempts) == expected.tries
    assert [a["settings"]["canvas_cm"] for a in attempts] == [
        round(one.settings.canvas_cm, 2) for one in expected.attempts
    ]
    kept = attempts[attempts[-1]["best_index"]]
    assert kept["svg"] == expected.svg
    assert kept["passes"] is expected.passes


@needs_tracer
def test_an_attempt_reports_what_it_drew_and_what_the_ruleset_says(base_url):
    _, start = upload(base_url)
    status, attempt = post_json(
        base_url + "/api/convert", {"key": start["key"], "profile": "embroidery-strict"}
    )
    assert status == 200
    assert attempt["svg"].lstrip().startswith("<?xml") or "<svg" in attempt["svg"][:200]
    assert attempt["shapes"] > 0 and attempt["nodes"] > 0
    assert attempt["report"]["profile"] == "embroidery-strict"
    assert attempt["verified"] is True
    # Every stage says what it did, in the words it said it in.
    assert any("quantise" in note for note in attempt["notes"])
    # The document that comes back is the one the checker would grade.
    _, rechecked = post_json(
        base_url + "/api/check",
        {"svg": attempt["svg"], "profile": "embroidery-strict"},
    )
    assert rechecked["counts"]["error"] == attempt["report"]["counts"]["error"]


@needs_tracer
def test_a_slider_dragged_past_the_ruleset_is_pulled_back_and_told_so(base_url):
    _, start = upload(base_url)
    _, attempt = post_json(
        base_url + "/api/convert",
        {
            "key": start["key"],
            "profile": "embroidery-strict",
            "settings": {"canvas_cm": 500, "colors": 9, "speck_area": 1},
        },
    )
    assert attempt["settings"]["canvas_cm"] == 30.0
    assert attempt["settings"]["colors"] == 2
    assert len(attempt["clamped"]) == 2

    status, refused = post_json(
        base_url + "/api/convert",
        {"key": start["key"], "profile": "embroidery-strict",
         "settings": {"canvas_cm": "wide"}},
    )
    assert status == 400 and "unusable settings" in refused["error"]


@needs_tracer
def test_a_hand_made_attempt_does_not_become_the_answer_by_being_last(base_url):
    """B6 keeps the smallest passing attempt; a slider does not overrule that."""
    _, start = upload(base_url)
    first = post_json(
        base_url + "/api/convert",
        {"key": start["key"], "profile": "embroidery-strict", "reset": True,
         "settings": {"canvas_cm": 12}},
    )[1]
    assert first["passes"] and first["best_index"] == 0
    second = post_json(
        base_url + "/api/convert",
        {"key": start["key"], "profile": "embroidery-strict",
         "settings": {"canvas_cm": 20}},
    )[1]
    assert second["passes"] and second["index"] == 1
    assert second["best_index"] == 0  # the smaller one still wins


def test_an_image_the_server_has_forgotten_is_asked_for_again(base_url):
    status, body = post_json(
        base_url + "/api/convert", {"key": "0000000000000000", "profile": "embroidery-basic"}
    )
    assert status == 409
    assert body["reupload"] is True


def test_only_a_couple_of_images_are_kept_at_once(base_url):
    """A decoded image is the largest thing this process holds."""
    from svg_embroidery.server import MAX_SESSIONS, forget_all

    forget_all()
    keys = [
        upload(base_url, path)[1]["key"]
        for path in sorted(CORPUS.glob("*.png"))[: MAX_SESSIONS + 1]
    ]
    assert post_json(
        base_url + "/api/convert", {"key": keys[0], "profile": "embroidery-basic"}
    )[0] == 409
    assert recall(keys[-1]) is not None


def test_the_same_file_twice_is_the_same_session(base_url):
    assert upload(base_url)[1]["key"] == upload(base_url)[1]["key"]


def test_the_page_carries_the_whole_journey(base_url):
    _, body = get(base_url + "/")
    for piece in ("api/image", "api/convert", "api/capabilities",
                  "Is it worth converting?", "Convert to SVG",
                  "Download this SVG", "Check and fix this SVG"):
        assert piece in body, piece
    # The knobs are the ones the loop turns, and they are sliders.
    assert 'type="range"' in body
    for knob in ("canvas_cm", "colors", "speck_area"):
        assert knob in body
    # One column on a phone, two where there is room — one page, not two.
    assert "@media (min-width: 900px)" in body
    assert "grid-template-columns: 1fr" in body


def test_the_page_says_what_to_do_when_this_machine_cannot_trace(base_url):
    _, body = get(base_url + "/")
    assert "No tracer on this machine" in body
    assert "svgemb serve --host 0.0.0.0" in body  # the mobile/desktop answer


# -- a tab left open across a restart --------------------------------------

def headers_of(url):
    with urllib.request.urlopen(url, timeout=10) as response:
        return dict(response.headers)


def test_the_page_carries_the_build_that_served_it(base_url):
    _, body = get(base_url + "/")
    assert BUILD in body
    assert "__SVGEMB_BUILD__" not in body, "the placeholder must be substituted"
    # Every response says which build answered, so any of them can catch a
    # stale tab — not only the one that happens to fetch the page.
    for route in ("/", "/api/profiles"):
        assert headers_of(base_url + route)["X-Svgemb-Build"] == BUILD
    status, _ = post_json(base_url + "/api/check", {"svg": GOOD})
    assert status == 200


def test_the_stamp_moves_when_the_page_does_and_not_otherwise():
    """Otherwise the banner either never fires or fires on every restart."""
    import hashlib

    from svg_embroidery import server

    again = hashlib.sha256(server.INDEX_HTML.encode("utf-8")).hexdigest()[:12]
    assert again == BUILD, "an unchanged page keeps its stamp across restarts"

    edited = server.INDEX_HTML.replace("Fix what can be fixed", "Repair it")
    assert hashlib.sha256(edited.encode("utf-8")).hexdigest()[:12] != BUILD


def test_a_handler_never_shadows_a_function_it_has_to_call():
    """``renderFix(fix)`` once hid the global ``fix()`` from its own buttons.

    The handlers are defined inside these functions, so a parameter sharing a
    name with one of them silently rebinds it, and every click throws. Nothing
    in the page can catch that at load time, so catch it here.
    """
    import re

    from svg_embroidery.server import INDEX_HTML

    js = re.search(r"<script>(.*?)</script>", INDEX_HTML, re.S).group(1)
    declared = set(re.findall(r"^function (\w+)\(", js, re.M))
    assert {"fix", "check", "render", "renderFix", "download"} <= declared

    for name, params in re.findall(r"function (\w+)\(([^)]*)\)", js):
        taken = {p.strip() for p in params.split(",") if p.strip()}
        clash = taken & declared
        assert not clash, f"{name}() takes a parameter named {clash}, hiding it"


def test_the_page_says_so_when_it_breaks(base_url):
    """A silent exception is what turned one bug into an unusable page."""
    _, body = get(base_url + "/")
    assert "window.addEventListener('error'" in body
    assert "unhandledrejection" in body
    assert 'id="crash"' in body


def test_a_tab_running_an_older_build_is_told_to_reload(base_url):
    _, body = get(base_url + "/")
    assert "id=\"stale\"" in body
    assert "served !== BUILD" in body  # the comparison that raises the banner
    assert "location.reload()" in body
    # Wired into every request, so the notice appears on the next thing you do.
    assert body.count(".then(fresh)") == 3
