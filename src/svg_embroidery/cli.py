"""Command line interface: ``svgemb``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Sequence, Set

from .checker import Checker
from .document import SvgParseError, load_svg
from .fixes import (
    FixEngine,
    Risk,
    answer_from_mapping,
    fixer_classes_for,
    risks_up_to,
)
from .profiles import ProfileError, list_profiles, load_profile
from .report import (
    _render_decision,
    render_fix_json,
    render_fix_summary,
    render_fix_text,
    render_json,
    render_summary,
    render_text,
)
from .rules import RuleConfigError, available_rules
from .writer import WriterError

if TYPE_CHECKING:  # pragma: no cover - imported for annotations only
    from .fixes import Decision

DEFAULT_PROFILE = "embroidery-basic"

#: ``svgemb fix`` returns this when the engine caught its own output misbehaving
#: — a different thing from "your file still fails", and nothing is written.
EXIT_VERIFICATION_FAILED = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="svgemb",
        description="Check SVG files against the upload rules of embroidery and print shops.",
    )
    subparsers = parser.add_subparsers(dest="command")

    check = subparsers.add_parser("check", help="check one or more SVG files")
    check.add_argument("files", nargs="+", type=Path, help="SVG file(s) or directories")
    check.add_argument(
        "-p",
        "--profile",
        default=DEFAULT_PROFILE,
        help=f"profile name or path to a YAML ruleset (default: {DEFAULT_PROFILE})",
    )
    check.add_argument("--json", action="store_true", help="machine readable output")
    check.add_argument("-v", "--verbose", action="store_true", help="also show passing checks")
    check.add_argument("--strict", action="store_true", help="treat warnings as failures")
    check.add_argument("--no-color", action="store_true", help="plain ASCII output")
    check.add_argument(
        "-r", "--recursive", action="store_true", help="descend into directories"
    )

    fix = subparsers.add_parser(
        "fix",
        help="repair what can be repaired, then re-check",
        description=(
            "Apply the fixes a profile's rules have, re-check the result, and report "
            "both what changed and what could not be. Nothing is written unless you "
            "say where: -o, --in-place or --stdout."
        ),
        epilog=(
            "Exit codes: 0 the file passes, 1 it still has errors, "
            "2 a usage or file error, 3 svgemb caught its own output misbehaving."
        ),
    )
    fix.add_argument("files", nargs="+", type=Path, help="SVG file(s) or directories")
    fix.add_argument(
        "-p",
        "--profile",
        default=DEFAULT_PROFILE,
        help=f"profile name or path to a YAML ruleset (default: {DEFAULT_PROFILE})",
    )
    # One destination at most: writing to two places at once is always a typo.
    destination = fix.add_mutually_exclusive_group()
    destination.add_argument(
        "-o", "--output", type=Path, metavar="FILE", help="write the result here (one input)"
    )
    destination.add_argument(
        "-i", "--in-place", action="store_true", help="overwrite each input file"
    )
    destination.add_argument("--stdout", action="store_true", help="write the result to stdout")
    fix.add_argument(
        "--no-backup",
        action="store_true",
        help="with --in-place, do not keep the original as FILE.bak",
    )
    fix.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="show the diff and write nothing, even with -o",
    )
    fix.add_argument("--diff", action="store_true", help="also show a unified diff")
    fix.add_argument(
        "--allow",
        default=Risk.SAFE.value,
        choices=[risk.value for risk in Risk],
        help="riskiest change allowed; each level includes the safer ones "
        f"(default: {Risk.SAFE.value})",
    )
    fix.add_argument(
        "--only",
        action="append",
        metavar="RULE",
        help="fix only these rules (repeatable, or comma separated)",
    )
    fix.add_argument(
        "--choose",
        action="append",
        metavar="RULE=OPTION",
        help="answer a repair's question up front (repeatable); "
        "RULE= on its own takes the recommended answer",
    )
    fix.add_argument(
        "-I",
        "--interactive",
        action="store_true",
        help="ask the questions at the terminal instead of printing them",
    )
    fix.add_argument("--json", action="store_true", help="machine readable output")
    fix.add_argument("--strict", action="store_true", help="treat warnings as failures")
    fix.add_argument("--no-color", action="store_true", help="plain ASCII output")
    fix.add_argument(
        "--no-verify",
        action="store_true",
        help="skip the before/after render check (faster, unproven)",
    )
    fix.add_argument(
        "-r", "--recursive", action="store_true", help="descend into directories"
    )

    profiles = subparsers.add_parser("profiles", help="list available profiles")
    profiles.add_argument("--json", action="store_true", help="machine readable output")
    profiles.add_argument("name", nargs="?", help="show the rules of one profile")

    rules = subparsers.add_parser("rules", help="list available rules and their parameters")
    rules.add_argument("--json", action="store_true", help="machine readable output")

    roundtrip = subparsers.add_parser(
        "roundtrip",
        help="check that reading and re-writing a file changes nothing (A0 gate)",
    )
    roundtrip.add_argument("files", nargs="+", type=Path, help="SVG file(s) or directories")
    roundtrip.add_argument(
        "-r", "--recursive", action="store_true", help="descend into directories"
    )
    roundtrip.add_argument(
        "--write-normalised",
        metavar="DIR",
        type=Path,
        help="also write each re-serialised file into DIR for inspection",
    )

    doctor = subparsers.add_parser(
        "doctor", help="show which capabilities are available on this machine"
    )
    doctor.add_argument("--json", action="store_true", help="machine readable output")

    bench = subparsers.add_parser(
        "bench",
        help="measure the image corpus and compare against the saved baseline (B1)",
        description="Measure every image in the benchmark corpus against the profile it is "
        "aimed at. With a baseline recorded, also report which numbers got better and "
        "which got worse — the point of the whole exercise.",
    )
    bench.add_argument("--corpus", metavar="DIR", type=Path, help="corpus directory")
    bench.add_argument(
        "--only",
        action="append",
        metavar="NAME",
        help="measure just this image (repeatable)",
    )
    bench.add_argument("--baseline", metavar="FILE", type=Path, help="baseline JSON to compare against")
    bench.add_argument(
        "--save", action="store_true", help="write this run to the baseline afterwards"
    )
    bench.add_argument(
        "--no-compare", action="store_true", help="skip the baseline comparison"
    )
    bench.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="exit non-zero when any metric got worse (for CI)",
    )
    bench.add_argument(
        "--work-side",
        type=int,
        metavar="N",
        help="force the resolution the metrics run at; by default each image is "
        "measured at whatever resolution makes its profile's minimum feature "
        "a whole kernel wide",
    )
    bench.add_argument(
        "--tracer",
        metavar="NAME",
        help="which tracer fills in paths/nodes/fit: potrace, potracer, vtracer, "
        "or 'none'; by default the best one installed here",
    )
    bench.add_argument(
        "--tracers",
        action="store_true",
        help="run the corpus once per installed tracer and compare them (B0)",
    )
    bench.add_argument(
        "--preprocess",
        action="store_true",
        help="clean each image through B3's pipeline before measuring it; the "
        "numbers then describe the cleaned image, so such a run is never "
        "diffed against a baseline taken without it",
    )
    bench.add_argument(
        "--overlap",
        type=int,
        metavar="N",
        default=None,
        help="working pixels each colour is grown under the ones stitched after "
        "it, so the seams between layers cannot show as bare fabric (B4); "
        "0 traces butt joints, which is what the 'gaps' column measures",
    )
    bench.add_argument(
        "--cleanup",
        action="store_true",
        help="run B5's cleanup over each traced document before measuring it: "
        "the profile's own repairs, which is where shapes too small to sew "
        "come out; the 'passes' column says whether the result satisfies the "
        "profile either way",
    )
    bench.add_argument(
        "--convert",
        action="store_true",
        help="run B6's whole loop on each image — preprocess, trace, clean up, "
        "check, and adjust a setting and try again when it still fails; implies "
        "--preprocess and --cleanup, and fills the 'tries' column",
    )
    bench.add_argument(
        "--tries",
        type=int,
        metavar="N",
        help="with --convert, how many attempts each image gets",
    )
    bench.add_argument(
        "-p",
        "--profile",
        metavar="NAME",
        help="aim every image at this profile instead of the one its manifest "
        "names; the profile sets the colour budget and what counts as too fine, "
        "so such a run is never diffed against a baseline taken at another",
    )
    bench.add_argument("--explain", action="store_true", help="describe the columns and exit")
    bench.add_argument("--json", action="store_true", help="machine readable output")
    bench.add_argument("--no-color", action="store_true", help="plain text, no icons")

    assess = subparsers.add_parser(
        "assess",
        help="say whether an image is worth converting to embroidery at all (B2)",
        description=(
            "Measure an image against the profile it is aimed at and grade it good, "
            "marginal or hopeless — before anyone waits on a conversion. The numbers "
            "are exactly the ones 'svgemb bench' prints; this command turns them into "
            "a verdict and a reason."
        ),
        epilog=(
            "Exit codes: 0 good or marginal, 1 hopeless (or marginal with --strict), "
            "2 nothing could be read."
        ),
    )
    # "*" rather than "+" so --explain works on its own; the check is below.
    assess.add_argument("files", nargs="*", type=Path, help="image file(s) or directories")
    assess.add_argument(
        "-p",
        "--profile",
        default=DEFAULT_PROFILE,
        help=f"profile name or path to a YAML ruleset (default: {DEFAULT_PROFILE})",
    )
    assess.add_argument(
        "-v", "--verbose", action="store_true", help="show every reading, not just the deciding one"
    )
    assess.add_argument(
        "--strict", action="store_true", help="treat 'marginal' as a failure too"
    )
    assess.add_argument(
        "--work-side",
        type=int,
        metavar="N",
        help="force the resolution the metrics run at (default: from the profile)",
    )
    assess.add_argument(
        "--explain", action="store_true", help="describe the bands and their thresholds, then exit"
    )
    assess.add_argument("--json", action="store_true", help="machine readable output")
    assess.add_argument("--no-color", action="store_true", help="plain ASCII output")
    assess.add_argument(
        "-r", "--recursive", action="store_true", help="descend into directories"
    )

    # Imported here rather than at the top so that the number in the help text
    # and the number the loop uses cannot drift apart.
    from .convert import DEFAULT_TRIES as CONVERT_TRIES

    convert = subparsers.add_parser(
        "convert",
        help="turn an image into an embroidery-ready SVG (B6)",
        description=(
            "Preprocess, trace, clean up and check — and when the result still fails, "
            "change one setting and try again. Nothing is written unless you say "
            "where: -o or --stdout."
        ),
        epilog=(
            "Exit codes: 0 the SVG passes its profile, 1 it does not, 2 a usage or "
            "file error, 3 svgemb caught its own output misbehaving."
        ),
    )
    convert.add_argument("files", nargs="+", type=Path, help="image file(s) or directories")
    convert.add_argument(
        "-p",
        "--profile",
        default=DEFAULT_PROFILE,
        help=f"profile name or path to a YAML ruleset (default: {DEFAULT_PROFILE})",
    )
    destination = convert.add_mutually_exclusive_group()
    destination.add_argument(
        "-o", "--output", type=Path, metavar="FILE", help="write the SVG here (one input)"
    )
    destination.add_argument(
        "--stdout", action="store_true", help="write the SVG to stdout"
    )
    convert.add_argument(
        "--tracer",
        metavar="NAME",
        help="which tracer draws the paths: potrace, potracer, vtracer; "
        "by default the best one installed here",
    )
    convert.add_argument(
        "--tries",
        type=int,
        default=None,
        metavar="N",
        help="how many times a failing conversion may adjust a setting and trace "
        f"again (default: {CONVERT_TRIES})",
    )
    convert.add_argument(
        "--no-retry",
        action="store_true",
        help="convert once and report the verdict, without adjusting anything",
    )
    convert.add_argument(
        "-v", "--verbose", action="store_true", help="show what every stage did"
    )
    convert.add_argument("--json", action="store_true", help="machine readable output")
    convert.add_argument("--no-color", action="store_true", help="plain ASCII output")
    convert.add_argument(
        "-r", "--recursive", action="store_true", help="descend into directories"
    )

    serve = subparsers.add_parser(
        "serve", help="run the local web UI — check, fix and convert (phone friendly)"
    )
    serve.add_argument("-P", "--port", type=int, default=8000, help="port (default: 8000)")
    serve.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address; use 0.0.0.0 to reach it from other devices on your network "
        "(default: 127.0.0.1)",
    )
    serve.add_argument("-v", "--verbose", action="store_true", help="log every request")

    return parser


def _collect_files(paths: Sequence[Path], recursive: bool) -> List[Path]:
    files: List[Path] = []
    for path in paths:
        if path.is_dir():
            pattern = "**/*.svg" if recursive else "*.svg"
            files.extend(sorted(path.glob(pattern)))
        else:
            files.append(path)
    return files


def _cmd_check(args: argparse.Namespace) -> int:
    try:
        checker = Checker(load_profile(args.profile))
    except (ProfileError, RuleConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    files = _collect_files(args.files, args.recursive)
    if not files:
        print("error: no SVG files found", file=sys.stderr)
        return 2

    missing = [path for path in files if not path.exists()]
    for path in missing:
        print(f"error: no such file: {path}", file=sys.stderr)
    files = [path for path in files if path.exists()]
    if not files:
        return 2

    reports = []
    for path in files:
        try:
            reports.append(checker.check_file(path))
        except SvgParseError as exc:  # pragma: no cover - check_file catches these
            print(f"error: {path}: {exc}", file=sys.stderr)
            return 2

    if args.json:
        print(render_json(reports, strict=args.strict))
    else:
        for index, report in enumerate(reports):
            if index:
                print()
            print(
                render_text(
                    report, strict=args.strict, verbose=args.verbose, color=not args.no_color
                )
            )
        if len(reports) > 1:
            print()
            print(render_summary(reports, strict=args.strict, color=not args.no_color))

    failed = any(not report.passed(strict=args.strict) for report in reports)
    return 1 if failed else 0


def _selected_rules(args: argparse.Namespace, profile) -> Optional[Set[str]]:
    """``--only`` as a set, refusing ids that would silently match nothing."""
    if not args.only:
        return None
    wanted: Set[str] = set()
    for value in args.only:
        wanted.update(part.strip() for part in value.split(",") if part.strip())
    known = {rule.id for rule in available_rules()}
    in_profile = {spec.id for spec in profile.rules}
    for rule_id in sorted(wanted):
        if rule_id not in known:
            raise ValueError(f"unknown rule '{rule_id}' (see: svgemb rules)")
        if rule_id not in in_profile:
            raise ValueError(f"rule '{rule_id}' is not in profile '{profile.name}'")
    return wanted


def _parse_choices(values: Optional[Sequence[str]]) -> Dict[str, str]:
    """``--choose rule.id=option`` pairs, as a mapping."""
    answers: Dict[str, str] = {}
    for value in values or ():
        rule_id, separator, option = value.partition("=")
        if not separator or not rule_id.strip():
            raise ValueError(f"--choose wants RULE=OPTION, got '{value}'")
        answers[rule_id.strip()] = option.strip()
    return answers


def _ask_at_the_terminal(stream) -> Callable[["Decision"], Optional[str]]:
    """Put each question to whoever is sitting there, once."""

    def decide(decision: "Decision") -> Optional[str]:
        print(file=stream)
        print("\n".join(_render_decision(decision)[:-1]), file=stream)
        default = decision.default
        suffix = f" [{default.key}]" if default else ""
        keys = {option.key for option in decision.options}
        while True:
            try:
                answer = input(f"       choose ({'/'.join(sorted(keys))}, or skip){suffix}: ")
            except EOFError:  # not a terminal after all — leave it open
                return None
            answer = answer.strip()
            if answer in ("skip", "none", "no"):
                return None
            if not answer:
                return default.key if default else None
            if answer in keys:
                return answer
            print(f"       '{answer}' is not one of them.", file=stream)

    return decide


def _destructive_rules(profile, selected: Optional[Set[str]]) -> List[str]:
    """Rules in play that have a destructive repair."""
    return sorted(
        spec.id
        for spec in profile.rules
        if (selected is None or spec.id in selected)
        and any(cls.risk is Risk.DESTRUCTIVE for cls in fixer_classes_for(spec.id))
    )


def _write_fixed(args: argparse.Namespace, report, stream) -> Optional[Path]:
    """Put the result where the flags say, or nowhere. Returns the path written."""
    if args.dry_run or not report.ok:
        return None
    if args.stdout:
        print(report.source_after, end="")
        return None
    destination = args.output or (report.file if args.in_place else None)
    if destination is None:
        return None
    if args.in_place and not report.changed:
        return None  # nothing to say, and no reason to touch the mtime

    note = ""
    if args.in_place and not args.no_backup:
        # Overwriting the only copy of someone's artwork is the one irreversible
        # thing this tool does, so it keeps the original next to it.
        backup = destination.with_suffix(destination.suffix + ".bak")
        backup.write_text(report.source_before, encoding="utf-8")
        note = f"  (original kept as {backup.name})"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(report.source_after, encoding="utf-8")
    print(f"wrote {destination}{note}", file=stream)
    return destination


def _cmd_fix(args: argparse.Namespace) -> int:
    try:
        profile = load_profile(args.profile)
        Checker(profile)  # surfaces a bad parameter before anything is touched
        selected = _selected_rules(args, profile)
        answers = _parse_choices(args.choose)
    except (ProfileError, RuleConfigError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    allow = risks_up_to(args.allow)
    if Risk.DESTRUCTIVE in allow:
        # A destructive repair deletes or reshapes artwork, so it is asked for
        # one rule at a time. "--allow destructive" alone is too broad a wish to
        # honour: it would mean "do anything at all to make this pass". Naming
        # the rule counts either way — --only selects it, --choose answers its
        # question, and both are the user saying the rule out loud.
        # Only what this run could actually reach: --only has already narrowed
        # the field, so naming a rule there is naming it here too.
        candidates = _destructive_rules(profile, selected)
        named = set(selected or ()) | set(answers)
        unnamed = [rule_id for rule_id in candidates if rule_id not in named]
        if not candidates:
            print(
                f"error: no rule in profile '{profile.name}' has a destructive fix",
                file=sys.stderr,
            )
            return 2
        if unnamed:
            listed = " ".join(f"--only {rule_id}" for rule_id in unnamed)
            print(
                "error: a destructive fix changes what the design means, so it has to "
                f"be asked for by name: {listed}",
                file=sys.stderr,
            )
            return 2

    if args.json and args.stdout:
        print("error: --json and --stdout both claim stdout; pick one", file=sys.stderr)
        return 2

    files = _collect_files(args.files, args.recursive)
    missing = [path for path in files if not path.exists()]
    for path in missing:
        print(f"error: no such file: {path}", file=sys.stderr)
    files = [path for path in files if path.exists()]
    if not files:
        if not missing:
            print("error: no SVG files found", file=sys.stderr)
        return 2
    if len(files) > 1 and (args.output or args.stdout):
        print(
            "error: -o/--stdout take a single input; use --in-place for several files",
            file=sys.stderr,
        )
        return 2

    # Whatever claims stdout — the fixed SVG, or the JSON — the report moves aside.
    stream = sys.stderr if (args.stdout or args.json) else sys.stdout

    # An explicit --choose always wins; -I only asks about what it leaves open.
    decide = answer_from_mapping(answers) if answers else None
    if args.interactive:
        ask = _ask_at_the_terminal(stream)
        given = decide
        decide = (lambda d: (given(d) if given else None) or ask(d)) if given else ask

    engine = FixEngine(
        profile, allow=allow, only=selected, verify=not args.no_verify, decide=decide
    )

    reports = []
    for path in files:
        try:
            reports.append(engine.fix_file(path))
        except (SvgParseError, WriterError, OSError) as exc:
            print(f"error: {path}: {exc}", file=sys.stderr)
            return 2

    if args.json:
        for report in reports:
            _write_fixed(args, report, stream)
        print(render_fix_json(reports, strict=args.strict, diff=args.diff or args.dry_run))
    else:
        for index, report in enumerate(reports):
            if index:
                print(file=stream)
            print(
                render_fix_text(
                    report,
                    strict=args.strict,
                    color=not args.no_color,
                    diff=args.diff or args.dry_run,
                ),
                file=stream,
            )
            written = _write_fixed(args, report, stream)
            if written is None and report.ok and report.changed and not args.stdout:
                note = (
                    "(dry run: nothing was written)"
                    if args.dry_run
                    else "(nothing written: pass -o FILE, --in-place or --stdout to keep it)"
                )
                print(note, file=stream)
        if len(reports) > 1:
            print(file=stream)
            print(
                render_fix_summary(reports, strict=args.strict, color=not args.no_color),
                file=stream,
            )

    if any(not report.ok for report in reports):
        return EXIT_VERIFICATION_FAILED
    failed = any(
        not (report.after or report.before).passed(strict=args.strict) for report in reports
    )
    return 1 if failed else 0


def _cmd_profiles(args: argparse.Namespace) -> int:
    if args.name:
        try:
            profile = load_profile(args.name)
        except ProfileError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if args.json:
            import json

            print(json.dumps(profile.to_dict(), indent=2, ensure_ascii=False))
            return 0
        print(f"{profile.name} — {profile.title or '(no title)'}")
        if profile.description:
            print(f"\n{profile.description.strip()}\n")
        if profile.url:
            print(f"Requirements: {profile.url}\n")
        print(f"Source: {profile.source}")
        print(f"Rules ({len(profile.rules)}):")
        for spec in profile.rules:
            params = (
                "  " + ", ".join(f"{k}={v!r}" for k, v in sorted(spec.params.items()))
                if spec.params
                else ""
            )
            severity = f" [{spec.severity}]" if spec.severity else ""
            print(f"  - {spec.id}{severity}{params}")
        return 0

    profiles = list_profiles()
    if args.json:
        import json

        print(json.dumps([p.to_dict() for p in profiles], indent=2, ensure_ascii=False))
        return 0
    if not profiles:
        print("No profiles found.")
        return 0
    width = max(len(p.name) for p in profiles)
    for profile in profiles:
        print(f"{profile.name:<{width}}  {profile.title or ''}")
    return 0


def _cmd_rules(args: argparse.Namespace) -> int:
    rules = available_rules()
    if args.json:
        import json

        print(
            json.dumps(
                [
                    {
                        "id": rule.id,
                        "summary": rule.summary,
                        "params": rule.params,
                        "default_severity": rule.default_severity.value,
                        "fixes": [
                            {"risk": fixer.risk.value, "summary": fixer.summary}
                            for fixer in fixer_classes_for(rule.id)
                        ],
                    }
                    for rule in rules
                ],
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0
    for rule in rules:
        print(f"{rule.id}  [{rule.default_severity.value}]")
        print(f"    {rule.summary}")
        for fixer in fixer_classes_for(rule.id):
            print(f"    fix [{fixer.risk.value}]: {fixer.summary}")
        if rule.params:
            for key, value in sorted(rule.params.items()):
                print(f"    - {key}: {value!r}")
    return 0


def _cmd_roundtrip(args: argparse.Namespace) -> int:
    from .roundtrip import check_file as roundtrip_file
    from .visual import default_renderer
    from .writer import serialize

    files = _collect_files(args.files, args.recursive)
    files = [path for path in files if path.exists()]
    if not files:
        print("error: no SVG files found", file=sys.stderr)
        return 2

    renderer = default_renderer()
    if renderer is None:
        print("note: no SVG renderer found, so images are not compared\n")
    else:
        print(f"note: comparing rendered output with {renderer.name}\n")

    failed = byte_identical = 0
    for path in files:
        try:
            result = roundtrip_file(path)
        except (SvgParseError, WriterError) as exc:
            print(f"FAIL  {path.name}: {exc}")
            failed += 1
            continue
        print(result.summary())
        failed += 0 if result.ok else 1
        byte_identical += 1 if result.byte_identical else 0

        if args.write_normalised:
            args.write_normalised.mkdir(parents=True, exist_ok=True)
            out = args.write_normalised / path.name
            out.write_text(serialize(load_svg(path)), encoding="utf-8")

    print(
        f"\n{len(files) - failed}/{len(files)} file(s) round-trip safely; "
        f"{byte_identical} byte-identical."
    )
    return 1 if failed else 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    from .capabilities import describe_platform, report

    statuses = report()
    if args.json:
        import json

        print(
            json.dumps(
                {"platform": describe_platform(), "capabilities": [s.to_dict() for s in statuses]},
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    print(f"svgemb capabilities on {describe_platform()}\n")
    for status in statuses:
        mark = "yes" if status.available else "no "
        print(f"[{mark}] {status.capability.title:<18} {status.capability.enables}")
        if status.found:
            for label, version in status.found.items():
                print(f"       using {label} ({version})")
        if not status.available:
            if status.missing:
                print(f"       missing: {', '.join(status.missing)}")
            if status.hint:
                print(f"       install: {status.hint}")
        print()

    unavailable = [s for s in statuses if not s.available]
    if unavailable:
        print(
            "Checking and fixing SVGs never needs any of the above. For heavy work on a\n"
            "device that cannot install it, run 'svgemb serve --host 0.0.0.0' on a machine\n"
            "that can, and use it from the browser."
        )
    else:
        print("Everything is available on this machine.")
    return 0


def _cmd_bench(args: argparse.Namespace) -> int:
    from . import bench as bench_module

    if args.explain:
        print(bench_module.render_metric_help())
        return 0

    color = not args.no_color
    try:
        entries = bench_module.load_corpus(args.corpus)
    except bench_module.BenchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.only:
        wanted = set(args.only)
        unknown = wanted - {entry.name for entry in entries}
        if unknown:
            print(f"error: no such image(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        entries = [entry for entry in entries if entry.name in wanted]

    overlap = bench_module.DEFAULT_OVERLAP if args.overlap is None else args.overlap
    if overlap < 0:
        print("error: --overlap cannot be negative", file=sys.stderr)
        return 2
    if args.tries is not None and args.tries < 1:
        print("error: --tries needs at least one attempt", file=sys.stderr)
        return 2
    if args.convert and args.tracers:
        # The loop adjusts settings per image until the profile is satisfied, so
        # two tracers would be compared at whatever settings each of them needed
        # — which is not a comparison of the tracers.
        print(
            "error: --tracers compares one pass per tracer; --convert changes the "
            "settings per image, so the two answer different questions",
            file=sys.stderr,
        )
        return 2
    if args.profile:
        try:
            load_profile(args.profile)  # a bad name is a usage error, not 20 empty rows
        except ProfileError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        entries = bench_module.with_profile(entries, args.profile)
    # The loop *is* preprocessing, tracing and cleanup, so asking for it asks
    # for those: a row where the columns describe one pipeline and the document
    # came from another is a row nobody can read.
    preprocess = args.preprocess or args.convert
    cleanup = args.cleanup or args.convert

    if args.tracers:
        runs = bench_module.compare_tracers(
            entries,
            work_side=args.work_side,
            corpus=str(args.corpus or bench_module.DEFAULT_CORPUS),
            preprocess=preprocess,
            overlap=overlap,
            cleanup=cleanup,
        )
        print(bench_module.render_tracer_comparison(runs, color=color))
        return 0

    try:
        backend = bench_module.resolve_backend(args.tracer)
    except bench_module.BenchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    result = bench_module.run(
        entries,
        work_side=args.work_side,
        corpus=str(args.corpus or bench_module.DEFAULT_CORPUS),
        backend=backend,
        preprocess=preprocess,
        overlap=overlap,
        cleanup=cleanup,
        convert=args.convert,
        tries=args.tries,
        profile=args.profile or "",
    )

    # A subset run must never overwrite a whole-corpus baseline with three rows.
    baseline_path = args.baseline or bench_module.DEFAULT_BASELINE
    changes = None
    blocked: List[str] = []
    if not args.no_compare and Path(baseline_path).is_file():
        try:
            baseline = bench_module.load_baseline(baseline_path)
        except bench_module.BenchError as exc:
            # A baseline this build cannot read is a reason to stop — unless the
            # run was already going to replace it. The message says "re-record
            # with --save", so --save has to work when it is the only way out.
            if not args.save:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            blocked = [str(exc)]
            baseline = None
        if baseline is not None:
            blocked = bench_module.incomparable(baseline, result)
        if not blocked:
            changes = bench_module.compare(baseline, result)
        if args.only and changes is not None:  # only measured rows can have moved
            changes = [change for change in changes if change.name in {e.name for e in entries}]

    if args.json:
        print(bench_module.render_json(result, changes))
    else:
        print(bench_module.render_table(result, color=color))
        if changes is not None:
            print()
            print(bench_module.render_changes(changes, color=color))
        elif blocked:
            print(
                "\nnot compared against the baseline — it was taken under different "
                "conditions, so a diff would measure those rather than the code:"
            )
            for reason in blocked:
                print(f"  · {reason}")
            print("  re-record with 'svgemb bench --save' to make this run the baseline.")
        elif not args.no_compare:
            print(f"\nno baseline at {baseline_path} — record one with 'svgemb bench --save'")

    if args.save:
        if args.only:
            print(
                "error: refusing to save a baseline from a partial run; drop --only",
                file=sys.stderr,
            )
            return 2
        Path(baseline_path).parent.mkdir(parents=True, exist_ok=True)
        Path(baseline_path).write_text(
            bench_module.render_json(result) + "\n", encoding="utf-8"
        )
        print(f"\nbaseline written to {baseline_path}", file=sys.stderr)

    if args.fail_on_regression and changes:
        if any(change.verdict == "worse" for change in changes):
            return 1
    return 0


def _collect_images(paths: Sequence[Path], recursive: bool) -> List[Path]:
    """Like :func:`_collect_files`, but for whatever this machine can decode.

    Asking :mod:`svg_embroidery.raster` rather than hard-coding a list: without
    Pillow a directory of JPEGs is genuinely empty to us, and globbing them in
    only to report twenty identical "needs Pillow" lines helps nobody.
    """
    from .raster import readable_suffixes

    suffixes = readable_suffixes()
    files: List[Path] = []
    for path in paths:
        if path.is_dir():
            found = path.rglob("*") if recursive else path.glob("*")
            files.extend(
                sorted(item for item in found if item.suffix.lower() in suffixes)
            )
        else:
            files.append(path)
    return files


def _cmd_assess(args: argparse.Namespace) -> int:
    from . import triage
    from .bench import BenchError, measure_file

    if args.explain:
        print(triage.render_thresholds())
        return 0
    if not args.files:
        print("error: assess needs at least one image (or --explain)", file=sys.stderr)
        return 2

    try:
        load_profile(args.profile)  # a bad profile is a usage error, not a verdict
    except ProfileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    files = _collect_images(args.files, args.recursive)
    missing = [path for path in files if not path.exists()]
    for path in missing:
        print(f"error: no such file: {path}", file=sys.stderr)
    files = [path for path in files if path.exists()]
    # Triage answers "should this image be converted at all"; an SVG is already
    # past that question, and 'cannot identify image file' would not say so.
    vectors = [path for path in files if path.suffix.lower() == ".svg"]
    if vectors:
        print(
            f"error: {vectors[0]} is already a vector — 'svgemb check' is the one that "
            "grades SVG files; assess takes the image you want converted",
            file=sys.stderr,
        )
        return 2
    if not files:
        if not missing:
            print("error: no images found", file=sys.stderr)
        return 2

    assessments = []
    for path in files:
        try:
            row = measure_file(path, profile=args.profile, work_side=args.work_side)
        except BenchError as exc:  # pragma: no cover - measure_file reports inside the row
            print(f"error: {path}: {exc}", file=sys.stderr)
            return 2
        assessments.append(triage.assess(row, name=str(path)))

    if args.json:
        print(triage.render_json(assessments))
    else:
        for index, assessment in enumerate(assessments):
            if index:
                print()
            print(
                triage.render_assessment(
                    assessment, verbose=args.verbose, color=not args.no_color
                )
            )
        if len(assessments) > 1:
            print()
            print(triage.render_summary(assessments, color=not args.no_color))

    # An image this machine cannot decode is not a verdict of "bad": it is a
    # measurement we do not have. It only fails the run when *nothing* was read.
    verdicts = [a.verdict for a in assessments if a.verdict is not None]
    if not verdicts:
        return 2
    bad = {triage.Band.HOPELESS}
    if args.strict:
        bad.add(triage.Band.MARGINAL)
    return 1 if any(verdict in bad for verdict in verdicts) else 0


def _cmd_convert(args: argparse.Namespace) -> int:
    from .convert import ConvertError, convert_file, render_conversion, render_json

    try:
        profile = load_profile(args.profile)
        Checker(profile)  # a bad parameter is a usage error, before any work
    except (ProfileError, RuleConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json and args.stdout:
        print("error: --json and --stdout both claim stdout; pick one", file=sys.stderr)
        return 2
    tries = 1 if args.no_retry else (args.tries if args.tries is not None else None)
    if tries is not None and tries < 1:
        print("error: --tries needs at least one attempt", file=sys.stderr)
        return 2

    files = _collect_images(args.files, args.recursive)
    missing = [path for path in files if not path.exists()]
    for path in missing:
        print(f"error: no such file: {path}", file=sys.stderr)
    files = [path for path in files if path.exists()]
    # An SVG is what this command *produces*; handing it one is a different job.
    vectors = [path for path in files if path.suffix.lower() == ".svg"]
    if vectors:
        print(
            f"error: {vectors[0]} is already a vector — 'svgemb fix' is the one that "
            "repairs SVG files; convert takes the image you want turned into one",
            file=sys.stderr,
        )
        return 2
    if not files:
        if not missing:
            print("error: no images found", file=sys.stderr)
        return 2
    if len(files) > 1 and (args.output or args.stdout):
        print(
            "error: -o/--stdout take a single input; convert one image at a time",
            file=sys.stderr,
        )
        return 2

    # Whatever claims stdout — the SVG, or the JSON — the report moves aside.
    stream = sys.stderr if (args.stdout or args.json) else sys.stdout

    from .tracer import TracerError, backend_named

    try:
        backend = None
        if args.tracer:
            backend = backend_named(args.tracer)
            if not backend.available():
                raise TracerError(
                    f"tracer {args.tracer} is not available here — {backend.install}"
                )
        conversions = [
            convert_file(
                path,
                profile,
                backend=backend,
                **({} if tries is None else {"tries": tries}),
            )
            for path in files
        ]
    except (ConvertError, TracerError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(render_json(conversions))
    else:
        for index, conversion in enumerate(conversions):
            if index:
                print(file=stream)
            print(
                render_conversion(
                    conversion, verbose=args.verbose, color=not args.no_color
                ),
                file=stream,
            )

    written = _write_converted(args, conversions[0], stream)
    if written is None and not args.stdout and not args.json:
        print(
            "(nothing written: pass -o FILE or --stdout to keep the SVG)"
            if len(conversions) == 1
            else "(nothing written: -o takes one image at a time)",
            file=stream,
        )

    if any(not conversion.ok for conversion in conversions):
        return EXIT_VERIFICATION_FAILED
    return 0 if all(conversion.passes for conversion in conversions) else 1


def _write_converted(args: argparse.Namespace, conversion, stream) -> Optional[Path]:
    """Put the SVG where the flags say, or nowhere.

    Nothing is written without being asked, which is the same rule ``svgemb
    fix`` follows. It matters less here — the input is an image and the output
    is a new file — but a bare ``svgemb convert`` is then a preview of what you
    would get, and the flag is the difference between looking and keeping.
    """
    if not conversion.ok:
        return None
    if args.stdout:
        print(conversion.svg, end="")
        return None
    if args.output is None:
        return None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(conversion.svg, encoding="utf-8")
    print(f"wrote {args.output}", file=stream)
    return args.output


def _cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve  # imported lazily: only the CLI needs http.server

    try:
        serve(host=args.host, port=args.port, verbose=args.verbose)
    except OSError as exc:
        print(f"error: cannot serve on {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 2
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "check":
        return _cmd_check(args)
    if args.command == "fix":
        return _cmd_fix(args)
    if args.command == "profiles":
        return _cmd_profiles(args)
    if args.command == "rules":
        return _cmd_rules(args)
    if args.command == "roundtrip":
        return _cmd_roundtrip(args)
    if args.command == "doctor":
        return _cmd_doctor(args)
    if args.command == "bench":
        return _cmd_bench(args)
    if args.command == "assess":
        return _cmd_assess(args)
    if args.command == "convert":
        return _cmd_convert(args)
    if args.command == "serve":
        return _cmd_serve(args)
    parser.print_help()
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
