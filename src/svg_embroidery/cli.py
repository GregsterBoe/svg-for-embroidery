"""Command line interface: ``svgemb``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from .checker import Checker
from .document import SvgParseError
from .profiles import ProfileError, list_profiles, load_profile
from .report import render_json, render_summary, render_text
from .rules import RuleConfigError, available_rules

DEFAULT_PROFILE = "embroidery-basic"


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

    profiles = subparsers.add_parser("profiles", help="list available profiles")
    profiles.add_argument("--json", action="store_true", help="machine readable output")
    profiles.add_argument("name", nargs="?", help="show the rules of one profile")

    rules = subparsers.add_parser("rules", help="list available rules and their parameters")
    rules.add_argument("--json", action="store_true", help="machine readable output")

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
            print(render_summary(reports, strict=args.strict))

    failed = any(not report.passed(strict=args.strict) for report in reports)
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
        if rule.params:
            for key, value in sorted(rule.params.items()):
                print(f"    - {key}: {value!r}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "check":
        return _cmd_check(args)
    if args.command == "profiles":
        return _cmd_profiles(args)
    if args.command == "rules":
        return _cmd_rules(args)
    parser.print_help()
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
