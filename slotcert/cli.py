"""Command line interface for slotcert."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .cert import certify
from .core import analyze_path, patch_tree, rollback


def _scan(path: Path):
    files = [path] if path.is_file() else sorted(path.rglob("*.py"))
    reports = []
    for file in files:
        if any(part in {"__pycache__", ".git", ".venv"} for part in file.parts):
            continue
        try:
            reports.extend(analyze_path(file))
        except SyntaxError as exc:
            print(f"skip {file}: {exc}")
    return reports


def _cmd_scan(args) -> int:
    reports = _scan(Path(args.path))
    if args.json:
        print(json.dumps([r.as_dict() for r in reports], indent=2))
        return 0
    safe = [r for r in reports if r.safe]
    print(f"{'class':<28} {'line':>5} {'safe':<5} slots / reason")
    for report in reports:
        detail = ", ".join(report.slots) if report.safe else "; ".join(report.reasons)
        print(f"{report.name:<28} {report.lineno:>5} {str(report.safe):<5} {detail}")
    print(f"\n{len(safe)}/{len(reports)} classes safe to slot")
    return 0


def _cmd_patch(args) -> int:
    classes = set(args.classes.split(",")) if args.classes else None
    manifest = patch_tree(Path(args.path), classes, Path(args.state) if args.state else None)
    print(f"patched {len(manifest['files'])} file(s):")
    for entry in manifest["files"]:
        print(f"  {entry['path']}: {', '.join(entry['classes'])} -> {entry['slots']}")
    print(f"rollback: slotcert rollback --state {args.state or Path(args.path) / '.slotcert'}")
    return 0


def _cmd_rollback(args) -> int:
    result = rollback(Path(args.state))
    print(f"restored {len(result['restored'])} file(s) under {result['root']}")
    return 0


def _cmd_certify(args) -> int:
    cert = certify(
        package=Path(args.package),
        module=args.module,
        samples=args.samples,
        classes=args.classes.split(",") if args.classes else None,
        n=args.n,
        seed=args.seed,
        out=Path(args.out),
        repeats=args.repeats,
    )
    print(f"{'class':<24} {'B/inst before':>14} {'B/inst after':>13} {'mem drop':>9} "
          f"{'construct':>10} {'access':>8} {'state':>7}")
    for entry in cert["entries"]:
        print(f"{entry['class']:<24} {entry['bytes_before']:>14.2f} {entry['bytes_after']:>13.2f} "
              f"{entry['memory_reduction_pct']:>8.2f}% {entry['construct_ratio']:>9.3f}x "
              f"{entry['access_ratio']:>7.3f}x {str(entry['state_equal']):>7}")
    print("\ngates: " + ", ".join(f"{k}={v}" for k, v in cert["gates"].items()))
    print(f"passed={cert['passed']}  certificate={Path(args.out) / 'certificate.json'}")
    return 0 if cert["passed"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="slotcert",
        description="Find safe __slots__ candidates, patch with rollback, and certify the result.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="report safe and unsafe classes")
    scan.add_argument("path")
    scan.add_argument("--json", action="store_true")
    scan.set_defaults(func=_cmd_scan)

    patch = sub.add_parser("patch", help="rewrite safe classes in place with backups")
    patch.add_argument("path")
    patch.add_argument("--classes", help="comma-separated class allow-list")
    patch.add_argument("--state", help="state directory (default: <path>/.slotcert)")
    patch.set_defaults(func=_cmd_patch)

    back = sub.add_parser("rollback", help="restore files from a patch manifest")
    back.add_argument("--state", required=True)
    back.set_defaults(func=_cmd_rollback)

    cert = sub.add_parser("certify", help="patch a copy and measure memory/throughput/pickle")
    cert.add_argument("--package", required=True)
    cert.add_argument("--module", required=True)
    cert.add_argument("--samples", required=True, help="module:attribute of {class: factory}")
    cert.add_argument("--classes")
    cert.add_argument("--n", type=int, default=200_000)
    cert.add_argument("--seed", type=int, default=20261119)
    cert.add_argument("--repeats", type=int, default=5)
    cert.add_argument("--out", required=True)
    cert.set_defaults(func=_cmd_certify)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
