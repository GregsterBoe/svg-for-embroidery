"""A7: repairs that ask a question, and the answers that drive them."""

from pathlib import Path

import pytest

from svg_embroidery.cli import main
from svg_embroidery.fixes import (
    Decision,
    FixEngine,
    Option,
    Risk,
    answer_from_mapping,
    verify_fixer,
)
from svg_embroidery.fixes.choices import _average_stop

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
BAD = EXAMPLES / "bad-design.svg"


def svg(body: str, attrs: str = 'width="12cm" height="12cm" viewBox="0 0 120 120"') -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" {attrs}>\n{body}\n</svg>\n'


#: Black, red, black — the colours interleave, so keeping the drawing order and
#: collapsing to one layer per colour are genuinely different answers.
INTERLEAVED = svg(
    '  <path id="a" d="M10 10 L50 10 L50 50 L10 50 Z" fill="#000000"/>\n'
    '  <path id="b" d="M60 10 L110 10 L110 50 L60 50 Z" fill="#c8102e"/>\n'
    '  <path id="c" d="M10 60 L50 60 L50 110 L10 110 Z" fill="#000000"/>'
)

#: The same two colours, already in colour order: both answers agree.
SORTED = svg(
    '  <path id="a" d="M10 10 L50 10 L50 50 L10 50 Z" fill="#000000"/>\n'
    '  <path id="c" d="M10 60 L50 60 L50 110 L10 110 Z" fill="#000000"/>\n'
    '  <path id="b" d="M60 10 L110 10 L110 50 L60 50 Z" fill="#c8102e"/>'
)


def fix(source: str, answers=None, allow=(Risk.SAFE,), **kwargs):
    engine = FixEngine.from_profile_name(
        "embroidery-basic",
        allow=allow,
        decide=answer_from_mapping(answers) if answers else None,
        **kwargs,
    )
    return engine.fix_source(source)


# -- the protocol -----------------------------------------------------------

def test_a_decision_knows_its_default_and_its_price():
    decision = Decision(
        rule_id="x.y",
        question="Which?",
        options=[
            Option("cheap", "Cheap", risk=Risk.SAFE),
            Option("dear", "Dear", risk=Risk.DESTRUCTIVE, recommended=True),
        ],
    )
    assert decision.default.key == "dear"  # recommended wins over order
    assert decision.option("cheap").risk is Risk.SAFE
    assert decision.option("nope") is None

    affordable = decision.within({Risk.SAFE})
    assert [option.key for option in affordable.options] == ["cheap"]
    # Filtering leaves a decision, not a list: the question travels with it.
    assert affordable.question == "Which?"


def test_a_single_option_is_its_own_default():
    only = Decision("x.y", "Do it?", [Option("go", "Go")])
    assert only.default.key == "go"
    assert Decision("x.y", "?", []).default is None


def test_answer_from_mapping_reads_the_shorthand():
    decision = Decision(
        "x.y", "?", [Option("a", "A"), Option("b", "B", recommended=True)]
    )
    assert answer_from_mapping({"x.y": "a"})(decision) == "a"
    assert answer_from_mapping({"x.y": ""})(decision) == "b"  # "the sensible one"
    assert answer_from_mapping({"other": "a"})(decision) is None


# -- a question nobody answered is not a failure ----------------------------

def test_an_unanswered_question_is_reported_rather_than_guessed():
    report = fix(INTERLEAVED)
    assert not report.changed
    assert not report.applied
    assert [decision.rule_id for decision in report.pending] == [
        "structure.color_layers"
    ]
    question = report.pending[0]
    assert {option.key for option in question.options} == {"runs", "colors"}
    assert "#000000" in question.context and "#c8102e" in question.context


def test_a_question_is_asked_even_when_no_answer_is_affordable_yet():
    """Discoverability: the default run is where you find out it exists."""
    report = fix(BAD.read_text(encoding="utf-8"))
    asked = {decision.rule_id for decision in report.pending}
    assert {"color.no_gradients", "element.forbidden", "path.closed"} <= asked
    gradients = next(d for d in report.pending if d.rule_id == "color.no_gradients")
    assert all(option.risk is Risk.LOSSY for option in gradients.options)


def test_an_answer_that_is_not_on_the_list_is_reported(capsys):
    report = fix(INTERLEAVED, {"structure.color_layers": "sideways"})
    reasons = [skip.reason for skip in report.skipped]
    assert any("'sideways' is not an option" in reason for reason in reasons)
    assert not report.changed


def test_an_answer_above_the_risk_ceiling_names_the_flag():
    report = fix(INTERLEAVED, {"structure.color_layers": "colors"})
    reasons = [skip.reason for skip in report.skipped]
    assert "'colors' needs --allow destructive" in reasons
    assert not report.changed


# -- the answers actually differ --------------------------------------------

def test_keeping_the_drawing_order_cannot_move_a_pixel():
    report = fix(INTERLEAVED, {"structure.color_layers": "runs"})
    assert report.ok and report.changed
    assert report.after.passed()
    # Three runs of two colours: a colour may appear on more than one layer.
    assert report.source_after.count("<g id=") == 3
    assert 'id="layer-000000"' in report.source_after
    assert 'id="layer-000000-2"' in report.source_after  # and ids stay unique
    if report.visual is not None:
        assert report.visual.identical, "reordering nothing must render the same"


def test_one_layer_per_colour_is_the_destructive_answer():
    report = fix(
        INTERLEAVED,
        {"structure.color_layers": "colors"},
        allow=(Risk.SAFE, Risk.LOSSY, Risk.DESTRUCTIVE),
    )
    assert report.ok and report.after.passed()
    assert report.source_after.count("<g id=") == 2  # one per colour
    applied = report.applied[0]
    assert applied.risk is Risk.DESTRUCTIVE  # the option's risk, not the fixer's
    assert applied.chosen.key == "colors"


def test_artwork_already_in_colour_order_gets_one_free_answer():
    report = fix(SORTED)
    question = report.pending[0]
    assert [option.key for option in question.options] == ["colors"]
    assert question.options[0].risk is Risk.SAFE  # nothing has to move

    done = fix(SORTED, {"structure.color_layers": "colors"})
    assert done.after.passed()
    assert done.source_after.count("<g id=") == 2
    if done.visual is not None:
        assert done.visual.identical


def test_the_fixer_declines_artwork_it_cannot_sort():
    """A single shape painting two colours needs splitting, not regrouping."""
    two_toned = svg(
        '  <path id="a" d="M10 10 L50 10 L50 50 Z" fill="#000000" stroke="#c8102e" '
        'stroke-width="2"/>\n'
        '  <path id="b" d="M60 10 L110 10 L110 50 Z" fill="#c8102e"/>'
    )
    report = fix(two_toned)
    assert not report.pending
    reasons = [skip.reason for skip in report.skipped]
    assert any("paints in more than one colour" in reason for reason in reasons)


# -- gradients ---------------------------------------------------------------

def test_a_gradient_averages_over_the_span_each_stop_owns():
    # Two stops each own half the ramp: the mean is the midpoint.
    assert _average_stop([(0.0, "#ff0000", 1.0), (1.0, "#0000ff", 1.0)]) == "#800080"
    # A stop squeezed against the end owns almost none of it, so barely counts.
    mostly_black = _average_stop(
        [(0.0, "#000000", 1.0), (0.98, "#000000", 1.0), (1.0, "#ffffff", 1.0)]
    )
    assert mostly_black < "#111111"
    assert _average_stop([(0.0, "#123456", 1.0)]) == "#123456"


def test_flattening_a_live_gradient_and_clearing_up_after_it():
    source = BAD.read_text(encoding="utf-8")
    report = fix(
        source, {"color.no_gradients": "first"}, allow=(Risk.SAFE, Risk.LOSSY)
    )
    assert "color.no_gradients" in report.fixed_rules
    assert 'fill="#ff0000"' in report.source_after  # the first stop, as drawn
    assert "linearGradient" not in report.source_after  # nothing points at it now
    assert "url(#fade)" not in report.source_after


# -- the contract ------------------------------------------------------------

@pytest.mark.parametrize(
    "answer, risk", [("runs", Risk.SAFE), ("colors", Risk.DESTRUCTIVE)]
)
def test_each_answer_holds_the_four_invariants(answer, risk):
    """Every answer is a fix, so every answer meets the same four bars."""
    result = verify_fixer(INTERLEAVED, "structure.color_layers", choice=answer)
    assert result.ok, result.summary()
    assert result.risk is risk


def test_a_choice_fixer_still_gets_rolled_back_if_it_breaks_something():
    """The engine's guarantees do not weaken because a human picked the option."""
    report = fix(INTERLEAVED, {"structure.color_layers": "runs"})
    assert report.ok
    assert not report.verification_error
    before = {finding.rule_id for finding in report.before.errors}
    after = {finding.rule_id for finding in report.after.errors}
    assert not (after - before)


# -- the whole point ---------------------------------------------------------

def test_answering_everything_gets_the_unfixable_file_through(tmp_path, capsys):
    """``bad-design.svg`` was built to be unfixable. With answers, it passes."""
    target = tmp_path / "fixed.svg"
    code = main(
        [
            "fix", str(BAD),
            "--allow", "destructive",
            "--choose", "color.no_gradients=average",
            "--choose", "structure.color_layers=colors",
            "--choose", "element.forbidden=delete",
            "--choose", "path.closed=close",
            "-o", str(target),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "❌ FAIL → ✅ PASS   9 → 0 error(s)" in out
    assert main(["check", str(target)]) == 0


def test_the_run_repeats_until_it_settles():
    """Flattening a gradient adds a colour, which re-breaks the palette limit.

    A single pass would leave the file failing a rule the run itself broke, so
    the engine sweeps again — and the second sweep is visible in the report.
    """
    report = fix(
        BAD.read_text(encoding="utf-8"),
        {"color.no_gradients": "average"},
        allow=(Risk.SAFE, Risk.LOSSY),
    )
    palette_fixes = [fix for fix in report.applied if fix.rule_id == "color.max_count"]
    assert len(palette_fixes) == 2, "the palette was reduced, broken, and reduced again"
    assert "color.max_count" not in report.remaining_rules
