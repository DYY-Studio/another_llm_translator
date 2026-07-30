from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .execution import Scope, choose_running_run
from .errors import AppError
from .logging_utils import attach_project_log, configure_cli_logging, get_logger
from .project import init_project, resolve_project, sync_global_templates
from .stages import (
    export_project,
    inspect_full,
    run_all,
    run_apply,
    run_review,
    run_terminology,
    run_translation,
)


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
        ("proofread", "生成校对建议"),
        ("polish", "生成润色建议"),
        ("run-all", "运行完整建议流程"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("project")
        command.add_argument("--dry-run", action="store_true")
        command.add_argument("--force", action="store_true")
        selectors = command.add_mutually_exclusive_group()
        selectors.add_argument("--from-file")
        selectors.add_argument("--only-file")
        selectors.add_argument("--only-segment")
        if name != "run-all":
            run_choice = command.add_mutually_exclusive_group()
            run_choice.add_argument(
                "--resume-run",
                action="store_true",
                help="续用最近的同阶段未完成 Run",
            )
            run_choice.add_argument(
                "--decline-run",
                action="store_true",
                help="拒绝最近的同阶段未完成 Run 并开始新 Run",
            )

    apply = subparsers.add_parser("apply", help="应用校对或润色建议")
    apply.add_argument("project")
    apply.add_argument("--stage", choices=("proofreading", "polishing"), required=True)
    apply.add_argument("--all", action="store_true")
    apply.add_argument("--allow-outdated-base", action="store_true")
    apply.add_argument("--dry-run", action="store_true")
    apply_selectors = apply.add_mutually_exclusive_group()
    apply_selectors.add_argument("--from-file")
    apply_selectors.add_argument("--only-file")
    apply_selectors.add_argument("--only-segment")

    export = subparsers.add_parser("export", help="导出 TXT")
    export.add_argument("project")
    export.add_argument(
        "--stage", choices=("translated", "proofread", "polished"), required=True
    )
    export.add_argument("--bilingual", action="store_true")
    export.add_argument("--allow-missing", action="store_true")
    return parser


def run(argv: list[str] | None = None) -> int:
    configure_cli_logging()
    logger = get_logger()
    parser = build_parser()
    args = parser.parse_args(argv)
    logger.info("command start command=%s", args.command)
    if args.command == "init":
        path, summary = init_project(
            args.inputs,
            name=args.name,
            recursive=args.recursive,
            dry_run=args.dry_run,
        )
        if path is not None:
            attach_project_log(path)
            summary["project_path"] = str(path)
            logger.info(
                "project initialized project=%s files=%d segments=%d",
                path.name,
                summary["file_count"],
                summary["segment_count"],
            )
        for warning in summary.get("warnings", []):
            logger.warning("%s", warning)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        logger.info("command complete command=init")
        return 0
    if args.command == "inspect":
        project = resolve_project(args.project)
        attach_project_log(project)
        warnings = sync_global_templates(project, dry_run=args.dry_run)
        for warning in warnings:
            logger.warning("%s", warning)
        summary = inspect_full(project, dry_run=args.dry_run)
        summary["warnings"] = warnings
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        logger.info(
            "command complete command=inspect files=%d segments=%d",
            summary["files"],
            summary["segments"],
        )
        return 0
    if args.command in {
        "terminology",
        "translate",
        "proofread",
        "polish",
        "run-all",
    }:
        project = resolve_project(args.project)
        attach_project_log(project)
        warnings = sync_global_templates(project, dry_run=args.dry_run)
        for warning in warnings:
            logger.warning("%s", warning)
        scope = Scope(
            from_file=args.from_file,
            only_file=args.only_file,
            only_segment=args.only_segment,
            force=args.force,
            dry_run=args.dry_run,
        )
        resume_run_id = None
        run_warnings: list[str] = []
        if args.command != "run-all":
            stage = {
                "terminology": "terminology",
                "translate": "translation",
                "proofread": "proofreading",
                "polish": "polishing",
            }[args.command]
            action = (
                "resume"
                if args.resume_run
                else "decline" if args.decline_run else None
            )
            resume_run_id, run_warnings = choose_running_run(
                project,
                stage,
                action=action,
                dry_run=args.dry_run,
            )
            for warning in run_warnings:
                logger.warning("%s", warning)
        if args.command == "terminology":
            summary = asyncio.run(
                run_terminology(project, scope, resume_run_id=resume_run_id)
            )
        elif args.command == "translate":
            summary = asyncio.run(
                run_translation(project, scope, resume_run_id=resume_run_id)
            )
        elif args.command == "proofread":
            summary = asyncio.run(
                run_review(
                    project,
                    "proofreading",
                    scope,
                    resume_run_id=resume_run_id,
                )
            )
        elif args.command == "polish":
            summary = asyncio.run(
                run_review(
                    project,
                    "polishing",
                    scope,
                    resume_run_id=resume_run_id,
                )
            )
        else:
            summary = asyncio.run(run_all(project, scope))
        summary.setdefault("warnings", [])
        summary["warnings"] = [
            *warnings,
            *run_warnings,
            *summary["warnings"],
        ]
        for warning in summary["warnings"]:
            if warning not in warnings and warning not in run_warnings:
                logger.warning("%s", warning)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        logger.info(
            "command complete command=%s completed=%s failed=%s pending=%s",
            args.command,
            summary.get("completed", "-"),
            summary.get("failed", "-"),
            summary.get("pending", "-"),
        )
        return 5 if summary.get("failed") or summary.get("pending") else 0
    if args.command == "apply":
        project = resolve_project(args.project)
        attach_project_log(project)
        warnings = sync_global_templates(project, dry_run=args.dry_run)
        for warning in warnings:
            logger.warning("%s", warning)
        summary = run_apply(
            project,
            args.stage,
            Scope(
                from_file=args.from_file,
                only_file=args.only_file,
                only_segment=args.only_segment,
                dry_run=args.dry_run,
            ),
            allow_outdated_base=args.allow_outdated_base,
            confirmed_all=args.all,
        )
        summary["warnings"] = [*warnings, *summary["warnings"]]
        for warning in summary["warnings"]:
            if warning not in warnings:
                logger.warning("%s", warning)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        logger.info(
            "command complete command=apply stage=%s completed=%d",
            args.stage,
            summary["completed"],
        )
        return 0
    if args.command == "export":
        project = resolve_project(args.project)
        attach_project_log(project)
        warnings = sync_global_templates(project)
        for warning in warnings:
            logger.warning("%s", warning)
        summary = export_project(
            project,
            args.stage,
            bilingual=args.bilingual,
            allow_missing=args.allow_missing,
        )
        summary["warnings"] = warnings
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        logger.info(
            "command complete command=export stage=%s files=%d",
            args.stage,
            summary["files"],
        )
        return 0
    parser.error("unknown command")
    return 2


def main() -> None:
    try:
        raise SystemExit(run())
    except KeyboardInterrupt:
        get_logger().warning("command interrupted")
        raise SystemExit(130) from None
    except AppError as exc:
        if not get_logger().logger.handlers:
            print(f"error: {exc}", file=sys.stderr)
        else:
            get_logger().error("%s", exc)
        raise SystemExit(exc.exit_code) from exc


if __name__ == "__main__":
    main()
