import json

import pytest

from svg_embroidery import check_file, load_profile
from svg_embroidery.checker import Checker
from svg_embroidery.cli import main
from svg_embroidery.profiles import ProfileError, list_profiles


def test_builtin_profiles_all_load():
    names = {profile.name for profile in list_profiles()}
    assert {"embroidery-basic", "embroidery-strict", "plotter-vinyl", "example-shop"} <= names
    for profile in list_profiles():
        assert profile.build_rules()  # every rule id and parameter resolves


def test_profile_inheritance_overrides_and_disables():
    basic = load_profile("embroidery-basic")
    strict = load_profile("embroidery-strict")

    basic_rules = {spec.id: spec for spec in basic.rules}
    strict_rules = {spec.id: spec for spec in strict.rules}

    assert basic_rules["color.max_count"].params["max_colors"] == 3
    assert strict_rules["color.max_count"].params["max_colors"] == 2
    assert "stroke.min_width" in basic_rules  # inherited...
    assert "stroke.min_width" not in strict_rules  # ...and disabled
    assert "stroke.forbidden" in strict_rules  # added by the child
    assert strict_rules["color.no_transparency"].severity == "error"


def test_custom_profile_from_path(tmp_path):
    profile_file = tmp_path / "myshop.yaml"
    profile_file.write_text(
        "name: myshop\n"
        "extends: embroidery-basic\n"
        "rules:\n"
        "  - id: color.max_count\n"
        "    params:\n"
        "      max_colors: 1\n"
        "disable:\n"
        "  - structure.color_layers\n",
        encoding="utf-8",
    )
    profile = load_profile(str(profile_file))
    ids = {spec.id: spec for spec in profile.rules}
    assert ids["color.max_count"].params["max_colors"] == 1
    assert "structure.color_layers" not in ids


def test_profile_discovery_via_env(tmp_path, monkeypatch):
    (tmp_path / "shop-x.yaml").write_text(
        "name: shop-x\nrules:\n  - id: path.closed\n", encoding="utf-8"
    )
    monkeypatch.setenv("SVG_EMBROIDERY_PROFILE_PATH", str(tmp_path))
    assert load_profile("shop-x").rules[0].id == "path.closed"
    assert "shop-x" in {p.name for p in list_profiles()}


def test_unknown_profile():
    with pytest.raises(ProfileError):
        load_profile("no-such-shop")


def test_circular_inheritance(tmp_path, monkeypatch):
    (tmp_path / "a.yaml").write_text("name: a\nextends: b\nrules: []\n", encoding="utf-8")
    (tmp_path / "b.yaml").write_text("name: b\nextends: a\nrules: []\n", encoding="utf-8")
    monkeypatch.setenv("SVG_EMBROIDERY_PROFILE_PATH", str(tmp_path))
    with pytest.raises(ProfileError, match="circular"):
        load_profile("a")


def test_bad_profile_reports_rule_error(tmp_path):
    profile_file = tmp_path / "broken.yaml"
    profile_file.write_text(
        "name: broken\nrules:\n  - id: color.max_count\n    params:\n      maxcolors: 2\n",
        encoding="utf-8",
    )
    with pytest.raises(ProfileError, match="unknown parameter"):
        load_profile(str(profile_file)).build_rules()


def test_examples_against_profiles(examples_dir):
    good = check_file(examples_dir / "good-design.svg", profile="embroidery-basic")
    assert good.passed()
    assert not good.warnings

    bad = check_file(examples_dir / "bad-design.svg", profile="embroidery-basic")
    assert not bad.passed()
    failed_rules = {finding.rule_id for finding in bad.errors}
    assert {
        "geometry.canvas_size",
        "color.max_count",
        "color.no_gradients",
        "element.forbidden",
        "path.closed",
        "stroke.min_width",
    } <= failed_rules

    # Two colours are fine for embroidery, but not for a single-colour cutter.
    assert not check_file(examples_dir / "good-design.svg", profile="plotter-vinyl").passed()


def test_unparsable_file_becomes_a_finding(tmp_path):
    broken = tmp_path / "broken.svg"
    broken.write_text("<svg><oops>", encoding="utf-8")
    report = check_file(broken)
    assert not report.passed()
    assert report.errors[0].rule_id == "file.parse"


def test_strict_mode_fails_on_warnings(make_doc):
    checker = Checker.from_profile_name("embroidery-basic")
    doc = make_doc('<path d="M0 0 Z" fill="#000"/>', attrs='width="12cm" height="12cm"')
    report = checker.check_document(doc)
    assert report.passed() is True
    assert report.warnings  # missing viewBox
    assert report.passed(strict=True) is False


def test_cli_check_exit_codes(examples_dir, capsys):
    assert main(["check", str(examples_dir / "good-design.svg")]) == 0
    assert main(["check", str(examples_dir / "bad-design.svg")]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out


def test_cli_json_output(examples_dir, capsys):
    main(["check", str(examples_dir / "bad-design.svg"), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["passed"] is False
    assert payload[0]["profile"] == "embroidery-basic"
    assert payload[0]["counts"]["error"] > 0


def test_cli_directory_and_summary(examples_dir, capsys):
    assert main(["check", str(examples_dir)]) == 1
    out = capsys.readouterr().out
    assert "file(s) passed" in out


def test_cli_no_color_output(examples_dir, capsys):
    main(["check", str(examples_dir / "bad-design.svg"), "--no-color"])
    out = capsys.readouterr().out
    assert "❌" not in out and "[error]" in out


def test_cli_profiles_and_rules(capsys):
    assert main(["profiles"]) == 0
    assert "embroidery-basic" in capsys.readouterr().out

    assert main(["profiles", "embroidery-strict"]) == 0
    assert "stroke.forbidden" in capsys.readouterr().out

    assert main(["rules", "--json"]) == 0
    rules = json.loads(capsys.readouterr().out)
    assert any(rule["id"] == "stroke.min_width" for rule in rules)


def test_cli_unknown_profile(examples_dir, capsys):
    assert main(["check", str(examples_dir / "good-design.svg"), "-p", "nope"]) == 2
    assert "unknown profile" in capsys.readouterr().err


def test_cli_missing_file(capsys):
    assert main(["check", "does-not-exist.svg"]) == 2
    assert "no such file" in capsys.readouterr().err
