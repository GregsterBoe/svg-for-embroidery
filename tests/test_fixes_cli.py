"""A6: ``svgemb fix`` — the command, its gates, and what it refuses to do."""

import json
from pathlib import Path

import pytest

from svg_embroidery.cli import main
from svg_embroidery.fixes import FixEngine, FixerError, Risk, risks_up_to
from svg_embroidery.geometry import default_backend

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
BAD = EXAMPLES / "bad-design.svg"
GOOD = EXAMPLES / "good-design.svg"
THIN = EXAMPLES / "thin-detail.svg"

needs_backend = pytest.mark.skipif(
    default_backend() is None, reason="no path geometry backend installed"
)


@pytest.fixture
def copy_of(tmp_path):
    """A throwaway copy, so a test may overwrite it in place."""

    def _copy(source: Path) -> Path:
        target = tmp_path / source.name
        target.write_bytes(source.read_bytes())
        return target

    return _copy


# -- the risk ceiling -------------------------------------------------------

def test_allowing_a_level_allows_the_safer_ones():
    assert risks_up_to("safe") == frozenset({Risk.SAFE})
    assert risks_up_to("lossy") == frozenset({Risk.SAFE, Risk.LOSSY})
    assert risks_up_to(Risk.DESTRUCTIVE) == frozenset(Risk)
    with pytest.raises(FixerError, match="unknown risk level"):
        risks_up_to("reckless")


# -- the gate: bad-design.svg ----------------------------------------------

def test_the_gate_explains_precisely_what_it_cannot_fix(capsys):
    """A6's bar: pass, or say exactly why not.

    ``bad-design.svg`` is deliberately full of decisions no tool may make —
    live text, a referenced gradient, a 15 mm gap in a contour. So the useful
    half of the answer is the skip list, and every entry in it has to name a
    reason a person can act on.
    """
    assert main(["fix", str(BAD)]) == 1
    out = capsys.readouterr().out

    assert "✅ fixed    geometry.canvas_size  [safe]" in out
    assert "width 5cm -> 10cm" in out
    # Each remaining rule is accounted for: nothing fails in silence, and each
    # is either a flag you can pass, a question you can answer, or a dead end.
    for rule_id in ("color.max_count", "fill.required"):
        assert f"⏭ skipped  {rule_id}" in out
    for rule_id in ("structure.color_layers", "element.forbidden", "path.closed"):
        assert f"❓ your call  {rule_id}" in out
    assert "needs --allow lossy" in out
    assert "no automatic fix available" in out
    assert "❌ FAIL → ❌ FAIL   9 → 6 error(s)" in out
    assert "still failing:" in out


def test_a_clean_file_has_nothing_to_do(capsys):
    assert main(["fix", str(GOOD)]) == 0
    out = capsys.readouterr().out
    assert "Nothing to fix." in out
    assert "✅ PASS → ✅ PASS" in out


def test_allowing_lossy_repairs_more(capsys):
    assert main(["fix", str(BAD), "--allow", "lossy"]) == 1
    out = capsys.readouterr().out
    assert "✅ fixed    color.max_count  [lossy]" in out
    assert "✅ fixed    stroke.min_width  [lossy]" in out
    # The lossy fixer for path.closed still declines: the gap is 15 mm wide.
    assert "closing them would invent artwork" in out
    assert "9 → 4 error(s)" in out


# -- writing, and not writing ----------------------------------------------

def test_nothing_is_written_without_a_destination(capsys, copy_of):
    original = copy_of(BAD)
    before = original.read_bytes()
    assert main(["fix", str(original)]) == 1
    assert original.read_bytes() == before
    assert "nothing written" in capsys.readouterr().out


def test_output_file_holds_what_the_report_promised(tmp_path, capsys):
    target = tmp_path / "out" / "fixed.svg"
    assert main(["fix", str(BAD), "--allow", "lossy", "-o", str(target)]) == 1
    assert f"wrote {target}" in capsys.readouterr().out

    # The verdict printed for the run is the verdict of the file on disk.
    assert main(["check", str(target)]) == 1
    out = capsys.readouterr().out
    assert "4 error(s)" in out


def test_in_place_overwrites_and_is_idempotent(copy_of, capsys):
    target = copy_of(BAD)
    assert main(["fix", str(target), "--allow", "lossy", "--in-place"]) == 1
    once = target.read_bytes()
    assert once != BAD.read_bytes()
    assert "original kept as bad-design.svg.bak" in capsys.readouterr().out

    assert main(["fix", str(target), "--allow", "lossy", "--in-place"]) == 1
    assert target.read_bytes() == once  # a second pass changes nothing


def test_in_place_keeps_the_original_beside_it(copy_of, capsys):
    """Overwriting artwork is the one irreversible thing this tool does."""
    target = copy_of(BAD)
    assert main(["fix", str(target), "--in-place"]) == 1
    backup = target.with_suffix(".svg.bak")
    assert backup.read_bytes() == BAD.read_bytes()
    assert target.read_bytes() != BAD.read_bytes()
    capsys.readouterr()

    other = copy_of(GOOD)  # a clean file is not touched, so there is no .bak
    assert main(["fix", str(other), "--in-place"]) == 0
    assert not other.with_suffix(".svg.bak").exists()

    third = copy_of(BAD)
    third.with_suffix(".svg.bak").unlink(missing_ok=True)
    assert main(["fix", str(third), "--in-place", "--no-backup"]) == 1
    assert not third.with_suffix(".svg.bak").exists()


def test_dry_run_writes_nothing_even_with_an_output(tmp_path, capsys):
    target = tmp_path / "fixed.svg"
    assert main(["fix", str(BAD), "-o", str(target), "--dry-run"]) == 1
    assert not target.exists()
    out = capsys.readouterr().out
    assert "dry run: nothing was written" in out
    assert "--- a/bad-design.svg" in out  # a dry run shows the diff
    assert '+<svg xmlns="http://www.w3.org/2000/svg" width="10cm" height="10cm"' in out
    assert '-     width="5cm" height="5cm" viewBox="0 0 50 50">' in out


def test_stdout_keeps_the_report_off_the_pipe(capsys):
    assert main(["fix", str(BAD), "--stdout"]) == 1
    captured = capsys.readouterr()
    assert captured.out.startswith("<?xml")
    assert 'width="10cm"' in captured.out
    assert "⏭ skipped" in captured.err  # the report went the other way


def test_a_directory_gets_one_line_per_file(capsys):
    assert main(["fix", str(EXAMPLES), "-p", "embroidery-strict", "--no-color"]) == 1
    out = capsys.readouterr().out
    assert "[ok] PASS  " in out and "good-design.svg" in out
    assert "file(s) pass after fixing." in out
    assert "✅" not in out and "⏭" not in out  # --no-color means no icons


# -- refusals ---------------------------------------------------------------

def test_destructive_has_to_be_asked_for_by_name(capsys):
    assert main(["fix", str(THIN), "-p", "embroidery-strict", "--allow", "destructive"]) == 2
    err = capsys.readouterr().err
    assert "--only geometry.min_feature_size" in err


def test_answering_a_question_names_the_rule_too(capsys):
    """--choose is at least as explicit as --only, so it counts as naming."""
    assert main(
        [
            "fix", str(BAD), "--allow", "destructive",
            "--only", "element.forbidden",
            "--choose", "element.forbidden=delete",
        ]
    ) == 1
    out = capsys.readouterr().out
    assert "✅ fixed    element.forbidden  [destructive]" in out
    assert "deleted <text> from the artwork" in out


def test_destructive_when_the_selected_rules_have_none(capsys):
    assert main(
        ["fix", str(BAD), "--allow", "destructive", "--only", "geometry.canvas_size"]
    ) == 2
    assert "no rule in profile 'embroidery-basic' has a destructive fix" in (
        capsys.readouterr().err
    )


def test_only_refuses_a_rule_it_would_never_run(capsys):
    assert main(["fix", str(BAD), "--only", "nope.rule"]) == 2
    assert "unknown rule 'nope.rule'" in capsys.readouterr().err

    assert main(["fix", str(BAD), "--only", "geometry.min_feature_size"]) == 2
    assert "is not in profile 'embroidery-basic'" in capsys.readouterr().err


def test_only_takes_a_list(capsys):
    assert main(["fix", str(BAD), "--only", "geometry.canvas_size,path.closed"]) == 1
    out = capsys.readouterr().out
    assert "✅ fixed    geometry.canvas_size" in out
    assert "⏭ skipped  color.max_count" in out
    assert "not selected" in out


def test_one_destination_at_a_time(capsys):
    with pytest.raises(SystemExit):  # argparse rejects it outright
        main(["fix", str(BAD), "-o", "a.svg", "--in-place"])
    assert main(["fix", str(BAD), "--json", "--stdout"]) == 2
    assert "both claim stdout" in capsys.readouterr().err


def test_one_input_for_one_output(capsys):
    assert main(["fix", str(BAD), str(GOOD), "-o", "out.svg"]) == 2
    assert "use --in-place for several files" in capsys.readouterr().err


def test_missing_file_and_unknown_profile(capsys):
    assert main(["fix", "does-not-exist.svg"]) == 2
    assert "no such file" in capsys.readouterr().err
    assert main(["fix", str(BAD), "-p", "nope"]) == 2
    assert "unknown profile" in capsys.readouterr().err


# -- machine readable -------------------------------------------------------

def test_json_output(capsys):
    assert main(["fix", str(BAD), "--allow", "lossy", "--json", "--diff"]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)[0]  # nothing else may land on stdout

    assert payload["ok"] is True and payload["changed"] is True
    assert "geometry.canvas_size" in payload["fixed_rules"]
    applied = {fix["rule"]: fix for fix in payload["applied"]}
    assert applied["color.max_count"]["risk"] == "lossy"
    assert applied["geometry.canvas_size"]["changes"][0]["location"] == "/svg"
    skipped = {skip["rule"]: skip for skip in payload["skipped"]}
    assert "element.forbidden" in skipped
    assert payload["before"]["counts"]["error"] == 9
    assert payload["after"]["counts"]["error"] == 4
    assert payload["diff"].startswith("--- a/bad-design.svg")


# -- the destructive path ---------------------------------------------------

@needs_backend
def test_destructive_fix_clears_the_thin_detail_gate(tmp_path, capsys):
    target = tmp_path / "thinner.svg"
    code = main(
        [
            "fix", str(THIN),
            "-p", "embroidery-strict",
            "--allow", "destructive",
            "--only", "geometry.min_feature_size",
            "-o", str(target),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "❌ FAIL → ✅ PASS   3 → 0 error(s)" in out
    assert "[destructive]" in out
    assert main(["check", str(target), "-p", "embroidery-strict"]) == 0


# -- a capability you don't have is a measurement you don't get -------------

def test_an_unmeasured_check_is_reported_by_both_commands(monkeypatch, capsys):
    """The whole point of A6's skip reporting: it names the missing package."""
    monkeypatch.setenv("SVGEMB_NO_GEOMETRY", "1")

    assert main(["fix", str(GOOD), "-p", "embroidery-strict"]) == 0
    out = capsys.readouterr().out
    assert "ℹ️ not measured  geometry.min_feature_size" in out
    assert "svg-for-embroidery[geometry]" in out

    # ...and it is visible from a plain check too, without -v.
    main(["check", str(GOOD), "-p", "embroidery-strict"])
    out = capsys.readouterr().out
    assert "1 check(s) not measured" in out
    assert "geometry.min_feature_size" in out


def test_a_verification_failure_is_its_own_exit_code(monkeypatch, capsys):
    """Exit 3 means svgemb caught its own output, not that the file is bad."""
    original = FixEngine.fix_source

    def sabotage(self, source, path=None):
        report = original(self, source, path=path)
        report.verification_error = "a safe fix changed the rendered image"
        return report

    monkeypatch.setattr(FixEngine, "fix_source", sabotage)
    target = Path("should-not-be-written.svg")
    assert main(["fix", str(BAD), "-o", str(target)]) == 3
    assert not target.exists()
    out = capsys.readouterr().out
    assert "verification FAILED" in out
    assert "bug in svgemb, not in your file" in out


# -- A7: answering questions from the command line --------------------------

def test_an_open_question_prints_the_command_that_answers_it(capsys):
    assert main(["fix", str(BAD)]) == 1
    out = capsys.readouterr().out
    assert "❓ your call  structure.color_layers" in out
    assert "* colors" in out  # the recommended answer is starred
    assert "answer with: --choose structure.color_layers=colors" in out
    # A question whose answers all cost something names the flag as well.
    assert "answer with: --allow lossy --choose color.no_gradients=average|first" in out


def test_choose_takes_the_recommended_answer_when_left_empty(capsys):
    assert main(["fix", str(BAD), "--choose", "structure.color_layers="]) == 1
    out = capsys.readouterr().out
    assert "✅ fixed    structure.color_layers  [safe]" in out


def test_choose_wants_a_rule_and_an_option(capsys):
    assert main(["fix", str(BAD), "--choose", "nonsense"]) == 2
    assert "--choose wants RULE=OPTION" in capsys.readouterr().err


def test_interactive_puts_the_question_to_the_terminal(monkeypatch, capsys):
    asked = []

    def answer(prompt):
        asked.append(prompt)
        return "colors"

    monkeypatch.setattr("builtins.input", answer)
    assert main(["fix", str(BAD), "-I"]) == 1
    out = capsys.readouterr().out
    assert asked and "choose (" in asked[0]
    assert "✅ fixed    structure.color_layers" in out


def test_interactive_accepts_a_refusal(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt: "skip")
    assert main(["fix", str(BAD), "-I"]) == 1
    out = capsys.readouterr().out
    assert "✅ fixed    structure.color_layers" not in out
    assert "❓ your call  structure.color_layers" in out  # still open, still shown


def test_questions_reach_the_json_payload(capsys):
    assert main(["fix", str(BAD), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)[0]
    pending = {ask["rule"]: ask for ask in payload["pending"]}
    layers = pending["structure.color_layers"]
    assert layers["options"][0]["key"] == "colors"
    assert layers["options"][0]["recommended"] is True
    assert layers["context"].startswith("4 colours")
