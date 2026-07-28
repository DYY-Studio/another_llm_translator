from __future__ import annotations

import argparse
import json
import sys

from .errors import AppError
from .project import init_project, inspect_project, resolve_project, sync_global_templates


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

