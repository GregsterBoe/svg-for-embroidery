"""A2: the fix protocol, the engine, and its self-verification."""

from pathlib import Path

import pytest

from svg_embroidery.document import SvgDocument
from svg_embroidery.fixes import base as fixbase
from svg_embroidery.fixes import (
    FixEngine,
    FixOutcome,
    Fixer,
    FixerError,
    Risk,
    available_fixers,
    parse_risks,
    verify_fixer,
    verify_no_op,
)
from svg_embroidery.visual import default_renderer

CORPUS = sorted((Path(__file__).parent / "corpus").glob("*.svg"))

NO_VIEWBOX = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="12cm" height="12cm">\n'
    '  <g id="a" fill="#000000">\n'
    '    <path d="M10 10 L110 10 L110 110 L10 110 Z"/>\n'
    "  </g>\n"
    "</svg>\n"
)

TOO_MANY_COLOURS = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="12cm" height="12cm" viewBox="0 0 120 120">\n'
    '  <g id="a"><path d="M0 0 L50 0 L50 50 Z" fill="#111111"/>'
    '<path d="M50 0 L100 0 L100 50 Z" fill="#222222"/>'
    '<path d="M0 50 L50 50 L50 100 Z" fill="#333333"/>'
    '<path d="M50 50 L100 50 L100 100 Z" fill="#444444"/></g>\n'
    "</svg>\n"
)

needs_renderer = pytest.mark.skipif(
    default_renderer() is None, reason="no SVG renderer installed"
)


@pytest.fixture
def registry():
    """Register test fixers without leaking into other tests."""
    saved = dict(fixbase._FIXERS)
    yield fixbase._FIXERS
    fixbase._FIXERS.clear()
    fixbase._FIXERS.update(saved)


# -- registration ----------------------------------------------------------

def test_fixer_needs_a_rule_id(registry):
    with pytest.raises(FixerError, match="must set rule_id"):

        @fixbase.register_fixer
        class Nameless(Fixer):
            pass


def test_one_fixer_per_rule(registry):
    with pytest.raises(FixerError, match="already registered"):

        @fixbase.register_fixer
        class Duplicate(Fixer):
            rule_id = "geometry.require_viewbox"


def test_parse_risks():
    assert parse_risks(["safe", "LOSSY"]) == frozenset({Risk.SAFE, Risk.LOSSY})
    assert parse_risks([Risk.DESTRUCTIVE]) == frozenset({Risk.DESTRUCTIVE})
    with pytest.raises(FixerError, match="unknown risk level"):
        parse_risks(["reckless"])


def test_every_registered_fixer_targets_a_real_rule():
    from svg_embroidery.rules import get_rule_class

    for fixer in available_fixers():
        get_rule_class(fixer.rule_id)  # raises if the rule id is wrong
        assert fixer.summary, f"{fixer.__name__} has no summary"


# -- the floor: doing nothing must change nothing --------------------------

@pytest.mark.parametrize("path", CORPUS, ids=lambda p: p.name)
def test_a_run_that_fixes_nothing_returns_the_file_untouched(path):
    """Without this, no diff from any fixer could be trusted."""
    assert verify_no_op(path.read_bytes().decode("utf-8"))


def test_nothing_applied_means_no_change():
    engine = FixEngine.from_profile_name("embroidery-basic", allow=[])
    report = engine.fix_source(NO_VIEWBOX)
    assert not report.changed
    assert report.source_after == NO_VIEWBOX
    assert report.ok


# -- the reference fixer ---------------------------------------------------

def test_reference_fixer_satisfies_the_contract():
    result = verify_fixer(NO_VIEWBOX, "geometry.require_viewbox")
    assert result.ok, result.summary()
    assert result.risk is Risk.SAFE


def test_reference_fixer_touches_only_the_line_it_changed():
    engine = FixEngine.from_profile_name("embroidery-basic")
    report = engine.fix_source(NO_VIEWBOX)
    assert report.changed
    assert 'viewBox="0 0 453.543307 453.543307"' in report.source_after
    # One changed line in the diff, thanks to the verbatim writer.
    changed = [
        line
        for line in report.diff().splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    assert len(changed) == 2  # one removed, one added


def test_fixer_declines_when_it_cannot_help():
    engine = FixEngine.from_profile_name("embroidery-basic")
    report = engine.fix_source('<svg xmlns="http://www.w3.org/2000/svg" width="50%" height="50%"/>')
    reasons = [skip.reason for skip in report.skipped]
    assert any("relative" in reason for reason in reasons)
    assert not report.changed


def test_verify_rejects_a_sample_that_does_not_fail():
    already_fine = NO_VIEWBOX.replace('height="12cm"', 'height="12cm" viewBox="0 0 120 120"')
    result = verify_fixer(already_fine, "geometry.require_viewbox")
    assert not result.ok
    assert "does not fail" in result.summary()


def test_verify_reports_a_missing_fixer():
    result = verify_fixer(NO_VIEWBOX, "color.no_gradients")
    assert not result.ok
    assert "no fixer is registered" in result.summary()


# -- risk gating -----------------------------------------------------------

def _register_colour_fixer(registry, risk):
    @fixbase.register_fixer
    class RepaintEverything(Fixer):
        rule_id = "color.max_count"
        summary = "repaint every shape in the first colour"

        def apply(self, doc: SvgDocument) -> FixOutcome:
            outcome = FixOutcome()
            for node in doc.drawables:
                if node.element.get("fill") not in (None, "#111111"):
                    node.element.set("fill", "#111111")
                    outcome.add("repainted", node.location)
            return outcome

    RepaintEverything.risk = risk
    return RepaintEverything


def test_lossy_fixes_are_not_applied_by_default(registry):
    _register_colour_fixer(registry, Risk.LOSSY)
    engine = FixEngine.from_profile_name("embroidery-basic")
    report = engine.fix_source(TOO_MANY_COLOURS)

    assert not report.changed
    skipped = {skip.rule_id: skip for skip in report.skipped}
    assert "needs --allow lossy" in skipped["color.max_count"].reason


def test_lossy_fixes_run_when_allowed(registry):
    _register_colour_fixer(registry, Risk.LOSSY)
    engine = FixEngine.from_profile_name(
        "embroidery-basic", allow=[Risk.SAFE, Risk.LOSSY], visual_budget=1.0
    )
    report = engine.fix_source(TOO_MANY_COLOURS)

    assert report.changed
    assert "color.max_count" in report.fixed_rules
    assert report.ok


def test_only_filter_restricts_what_runs(registry):
    _register_colour_fixer(registry, Risk.SAFE)
    engine = FixEngine.from_profile_name(
        "embroidery-basic", only={"geometry.require_viewbox"}
    )
    report = engine.fix_source(TOO_MANY_COLOURS.replace(' viewBox="0 0 120 120"', ""))

    applied = {fix.rule_id for fix in report.applied}
    assert applied == {"geometry.require_viewbox"}
    assert any(skip.reason == "not selected" for skip in report.skipped)


def test_rules_without_a_fixer_are_reported_as_such():
    engine = FixEngine.from_profile_name("embroidery-basic")
    report = engine.fix_source(TOO_MANY_COLOURS)
    reasons = {skip.rule_id: skip.reason for skip in report.skipped}
    assert reasons["color.max_count"] == "no automatic fix available"


# -- the engine polices its own output -------------------------------------

def test_a_fix_that_introduces_new_errors_is_caught(registry):
    """Verification does not need a renderer to catch a bad repair."""

    class OverEnthusiastic(Fixer):
        rule_id = "geometry.require_viewbox"
        risk = Risk.SAFE
        summary = "adds a viewBox but also breaks the canvas size"

        def apply(self, doc: SvgDocument) -> FixOutcome:
            outcome = FixOutcome()
            doc.root.set("viewBox", "0 0 100 100")
            doc.root.set("width", "80cm")  # now over the profile's 38 cm limit
            outcome.add("added a viewBox and wrecked the width")
            return outcome

    fixbase._FIXERS["geometry.require_viewbox"] = OverEnthusiastic

    engine = FixEngine.from_profile_name("embroidery-basic")
    report = engine.fix_source(NO_VIEWBOX)

    assert not report.ok
    assert "new errors" in report.verification_error
    assert "geometry.canvas_size" in report.verification_error


@needs_renderer
def test_a_safe_fix_that_moves_a_pixel_is_caught(registry):
    """The point of building A1 first: a risk claim is checked, not trusted."""

    class Liar(Fixer):
        rule_id = "geometry.require_viewbox"
        risk = Risk.SAFE  # ...but it repaints the artwork
        summary = "claims to be safe while changing the colours"

        def apply(self, doc: SvgDocument) -> FixOutcome:
            outcome = FixOutcome()
            doc.root.set("viewBox", "0 0 453.543307 453.543307")
            for node in doc.drawables:
                node.element.set("fill", "#ff0000")
                outcome.add("repainted", node.location)
            return outcome

    fixbase._FIXERS["geometry.require_viewbox"] = Liar

    engine = FixEngine.from_profile_name("embroidery-basic")
    report = engine.fix_source(NO_VIEWBOX)

    assert not report.ok
    assert "safe fix changed the rendered image" in report.verification_error


@needs_renderer
def test_a_lossy_fix_over_its_budget_is_caught(registry):
    _register_colour_fixer(registry, Risk.LOSSY)
    engine = FixEngine.from_profile_name(
        "embroidery-basic", allow=[Risk.LOSSY], visual_budget=0.01
    )
    report = engine.fix_source(TOO_MANY_COLOURS)

    assert not report.ok
    assert "exceeds the budget" in report.verification_error


def test_non_idempotent_fixer_fails_verification(registry):
    class NeverSatisfied(Fixer):
        rule_id = "geometry.require_viewbox"
        risk = Risk.SAFE
        summary = "marks the file on every run without ever fixing the rule"

        def apply(self, doc: SvgDocument) -> FixOutcome:
            # Never adds the viewBox, so the rule keeps failing and the engine
            # keeps calling this — appending another marker each time.
            outcome = FixOutcome()
            doc.root.set("data-passes", (doc.root.get("data-passes") or "") + "x")
            outcome.add("added a marker")
            return outcome

    fixbase._FIXERS["geometry.require_viewbox"] = NeverSatisfied

    result = verify_fixer(NO_VIEWBOX, "geometry.require_viewbox")
    assert not result.idempotent
    assert not result.fixed_target  # it never actually repairs the rule either
    assert not result.ok


# -- several fixers in one run ---------------------------------------------

def test_fixers_see_the_document_the_previous_one_left(registry):
    """Each fixer re-parses, so none of them reads a stale style or geometry."""
    observed = []

    @fixbase.register_fixer
    class RecordColours(Fixer):
        rule_id = "color.max_count"
        risk = Risk.SAFE
        summary = "records what it sees, then repaints"

        def apply(self, doc: SvgDocument) -> FixOutcome:
            observed.append(sorted(doc.colors()))
            outcome = FixOutcome()
            for node in doc.drawables:
                node.element.set("fill", "#111111")
                outcome.add("repainted", node.location)
            return outcome

    @fixbase.register_fixer
    class ObserveAfterwards(Fixer):
        rule_id = "fill.required"
        risk = Risk.SAFE
        summary = "observes the colours the previous fixer left behind"

        def apply(self, doc: SvgDocument) -> FixOutcome:
            observed.append(sorted(doc.colors()))
            return FixOutcome().decline("only observing")

    source = TOO_MANY_COLOURS.replace(
        "</g>", '<path d="M0 100 L20 100 L20 110 Z" fill="none"/></g>'
    )
    engine = FixEngine.from_profile_name("embroidery-basic", visual_budget=1.0)
    engine.fix_source(source)

    assert len(observed) == 2
    assert len(observed[0]) > 1              # saw the original palette
    assert observed[1] == ["#111111"]        # saw the repainted document


def test_report_reads_clearly():
    engine = FixEngine.from_profile_name("embroidery-basic")
    report = engine.fix_source(NO_VIEWBOX)
    text = report.summary()
    assert "geometry.require_viewbox" in text
    assert "errors: 0 -> 0" in text
    assert report.fixed_rules == ["geometry.require_viewbox"]
