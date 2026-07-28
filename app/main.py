from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .execution import Scope
from .errors import AppError
from .project import init_project, inspect_project, resolve_project, sync_global_templates
from .stages import run_terminology, run_translation


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minimal-llm-translator")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="创建翻译项目")
    init.add_argument("inputs", nargs="+")
    init.add_argument("--name", required=True)
    init.add_argument("--recursive", action="store_true")
    init.add_argument("--dry-run", action="store_true")

    inspect = subparsers.add_parser("inspect", help="检查项目状态")
    inspect.add_argument("project")
    inspect.add_argument("--dry-run", action="store_true")

    for name, help_text in (
        ("terminology", "提取并发布术语"),
        ("translate", "翻译项目"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("project")
        command.add_argument("--dry-run", action="store_true")
        command.add_argument("--force", action="store_true")
        selectors = command.add_mutually_exclusive_group()
        selectors.add_argument("--from-file")
        selectors.add_argument("--only-file")
        selectors.add_argument("--only-segment")
    return parser


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "init":
        path, summary = init_project(
            args.inputs,
            name=args.name,
            recursive=args.recursive,
            dry_run=args.dry_run,
        )
        if path is not None:
            summary["project_path"] = str(path)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    if args.command == "inspect":
        project = resolve_project(args.project)
        warnings = sync_global_templates(project, dry_run=args.dry_run)
        summary = inspect_project(project, repair_tail=not args.dry_run)
        summary["warnings"] = warnings
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    if args.command in {"terminology", "translate"}:
        project = resolve_project(args.project)
        warnings = sync_global_templates(project, dry_run=args.dry_run)
        scope = Scope(
            from_file=args.from_file,
            only_file=args.only_file,
            only_segment=args.only_segment,
            force=args.force,
            dry_run=args.dry_run,
        )
        if args.command == "terminology":
            summary = asyncio.run(run_terminology(project, scope))
        else:
            summary = asyncio.run(run_translation(project, scope))
        summary.setdefault("warnings", [])
        summary["warnings"] = [*warnings, *summary["warnings"]]
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 5 if summary.get("failed") or summary.get("pending") else 0
    parser.error("unknown command")
    return 2


def main() -> None:
    try:
        raise SystemExit(run())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except AppError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(exc.exit_code) from exc


if __name__ == "__main__":
    main()
