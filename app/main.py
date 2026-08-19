from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from .credentials import migrate_legacy_credentials
from .errors import AppError, UsageError
from .execution import Scope, choose_running_run
from .i18n import cli_language
from .llm_migration import migrate_llm_resources
from .locking import project_write_lock
from .logging_utils import attach_project_log, configure_cli_logging, get_logger
from .project import (
    add_project_files,
    init_project,
    remove_project_files,
    resolve_project,
    resolve_project_parent,
    sync_global_templates,
)
from .sqlite_storage import compact_project_database
from .stages import (
    export_project,
    export_terms,
    import_terms,
    inspect_full,
    publish_partial_terms,
    run_all,
    run_apply,
    run_review,
    run_terminology,
    run_translation,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="another-llm-translator")
    parser.add_argument(
        "--language",
        choices=("system", "zh-CN", "en"),
        default="system",
        help="界面和 CLI 语言（默认：system）",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="创建翻译项目")
    init.add_argument("inputs", nargs="*")
    init.add_argument("--name", required=True)
    init.add_argument("--recursive", action="store_true")
    init.add_argument(
        "--empty",
        action="store_true",
        help="创建不包含源文件的空项目",
    )
    init.add_argument(
        "--document-adapter",
        default="txt",
        help="输入输出格式 Adapter ID（默认：txt）",
    )
    init.add_argument(
        "--adapter-option",
        dest="adapter_options",
        action="append",
        metavar="ADAPTER.OPTION=VALUE",
        help="Document Adapter 选项；可重复使用",
    )
    init.add_argument("--dry-run", action="store_true")
    init.add_argument(
        "--parent-dir",
        help="在指定父目录中创建项目（默认：内置 projects 目录）",
    )

    files_add = subparsers.add_parser("files-add", help="向项目追加源文件")
    files_add.add_argument("project")
    files_add.add_argument("inputs", nargs="+")
    files_add.add_argument("--recursive", action="store_true")
    files_add.add_argument(
        "--document-adapter",
        help="显式指定 Adapter；省略时按已安装 Adapter 的扩展名识别",
    )
    files_add.add_argument(
        "--adapter-option",
        dest="adapter_options",
        action="append",
        metavar="ADAPTER.OPTION=VALUE",
        help="Document Adapter 选项；可重复使用",
    )

    files_remove = subparsers.add_parser(
        "files-remove", help="从项目活动范围移除源文件"
    )
    files_remove.add_argument("project")
    files_remove.add_argument("file_ids", nargs="+")

    inspect = subparsers.add_parser("inspect", help="检查项目状态")
    inspect.add_argument("project")
    inspect.add_argument("--dry-run", action="store_true")

    subparsers.add_parser("optimize", help="压缩项目 SQLite 存储").add_argument(
        "project"
    )

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
        result_choice = command.add_mutually_exclusive_group()
        result_choice.add_argument("--force", action="store_true")
        result_choice.add_argument(
            "--reuse-mixed-fingerprints",
            action="store_true",
            help="显式复用设置指纹不同的已完成结果",
        )
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

    export = subparsers.add_parser("export", help="按项目文档格式导出")
    export.add_argument("project")
    export.add_argument(
        "--stage", choices=("translated", "proofread", "polished"), required=True
    )
    export.add_argument("--bilingual", action="store_true")
    export.add_argument("--allow-missing", action="store_true")
    export.add_argument(
        "--format", choices=("original", "txt"), default="original"
    )
    export.add_argument(
        "--file",
        dest="file_ids",
        action="append",
        help="仅导出指定 File ID；可重复使用",
    )

    terms_import = subparsers.add_parser(
        "terms-import", help="合并导入 JSON 或 CSV 术语表"
    )
    terms_import.add_argument("project")
    terms_import.add_argument("file")
    terms_import.add_argument("--dry-run", action="store_true")

    terms_export = subparsers.add_parser(
        "terms-export", help="导出 JSON 或 CSV 术语表"
    )
    terms_export.add_argument("project")
    terms_export.add_argument("output")
    terms_export.add_argument("--include-disabled", action="store_true")
    terms_export.add_argument(
        "--source", choices=("published", "scanned"), default="published"
    )
    terms_partial = subparsers.add_parser(
        "terms-publish-partial", help="发布当前活动扫描中已有的候选术语"
    )
    terms_partial.add_argument("project")
    return parser


def parse_adapter_option_args(values: list[str]) -> dict[str, dict[str, str]]:
    resolved: dict[str, dict[str, str]] = {}
    for value in values:
        key, separator, option_value = value.partition("=")
        adapter_id, _, option_id = key.partition(".")
        if (
            not separator
            or not adapter_id
            or not option_id
            or "." in adapter_id
            or "." in option_id
        ):
            raise UsageError(f"Adapter 选项格式无效：{value}")
        if option_id in resolved.setdefault(adapter_id, {}):
            raise UsageError(f"Adapter 选项重复：{value}")
        resolved[adapter_id][option_id] = option_value
    return resolved


def _resolve_project(args: argparse.Namespace) -> Path:
    project = resolve_project(args.project)
    attach_project_log(project)
    return project


def emit_summary(summary: dict[str, Any]) -> None:
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def run(argv: list[str] | None = None) -> int:
    migrate_legacy_credentials()
    migrate_llm_resources()
    configure_cli_logging()
    logger = get_logger()
    parser = build_parser()
    args = parser.parse_args(argv)
    cli_language(None if args.language == "system" else args.language)
    logger.info("command start command=%s", args.command)
    if args.command == "init":
        projects_root = (
            resolve_project_parent(args.parent_dir)
            if args.parent_dir
            else None
        )
        path, summary = init_project(
            args.inputs,
            name=args.name,
            recursive=args.recursive,
            document_adapter_id=args.document_adapter,
            adapter_options=(
                parse_adapter_option_args(args.adapter_options)
                if args.adapter_options
                else None
            ),
            empty=args.empty,
            dry_run=args.dry_run,
            projects_root=projects_root,
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
        emit_summary(summary)
        logger.info("command complete command=init")
        return 0
    if args.command == "files-add":
        project = _resolve_project(args)
        with project_write_lock(project):
            summary = add_project_files(
                project,
                args.inputs,
                recursive=args.recursive,
                document_adapter_id=args.document_adapter,
                adapter_options=(
                    parse_adapter_option_args(args.adapter_options)
                    if args.adapter_options
                    else None
                ),
            )
        for warning in summary.get("warnings", []):
            logger.warning("%s", warning)
        emit_summary(summary)
        logger.info(
            "command complete command=files-add files=%d segments=%d",
            summary["added_files"],
            summary["added_segments"],
        )
        return 0
    if args.command == "files-remove":
        project = _resolve_project(args)
        with project_write_lock(project):
            summary = remove_project_files(project, args.file_ids)
        emit_summary(summary)
        logger.info(
            "command complete command=files-remove files=%d segments=%d",
            summary["removed_files"],
            summary["removed_segments"],
        )
        return 0
    if args.command == "inspect":
        project = _resolve_project(args)
        warnings = sync_global_templates(project, dry_run=args.dry_run)
        for warning in warnings:
            logger.warning("%s", warning)
        summary = inspect_full(project, dry_run=args.dry_run)
        summary["warnings"] = warnings
        emit_summary(summary)
        logger.info(
            "command complete command=inspect files=%d segments=%d",
            summary["files"],
            summary["segments"],
        )
        return 0
    if args.command == "optimize":
        project = _resolve_project(args)
        with project_write_lock(project):
            summary = compact_project_database(project)
        emit_summary(summary)
        logger.info(
            "command complete command=optimize reclaimed_bytes=%d",
            summary["reclaimed_bytes"],
        )
        return 0
    if args.command in {
        "terminology",
        "translate",
        "proofread",
        "polish",
        "run-all",
    }:
        project = _resolve_project(args)
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
        lock = nullcontext() if args.dry_run else project_write_lock(project)
        with lock:
            if args.command == "terminology":
                summary = asyncio.run(
                    run_terminology(
                        project,
                        scope,
                        resume_run_id=resume_run_id,
                        reuse_mixed_fingerprints=args.reuse_mixed_fingerprints,
                    )
                )
            elif args.command == "translate":
                summary = asyncio.run(
                    run_translation(
                        project,
                        scope,
                        resume_run_id=resume_run_id,
                        reuse_mixed_fingerprints=args.reuse_mixed_fingerprints,
                    )
                )
            elif args.command == "proofread":
                summary = asyncio.run(
                    run_review(
                        project,
                        "proofreading",
                        scope,
                        resume_run_id=resume_run_id,
                        reuse_mixed_fingerprints=args.reuse_mixed_fingerprints,
                    )
                )
            elif args.command == "polish":
                summary = asyncio.run(
                    run_review(
                        project,
                        "polishing",
                        scope,
                        resume_run_id=resume_run_id,
                        reuse_mixed_fingerprints=args.reuse_mixed_fingerprints,
                    )
                )
            else:
                summary = asyncio.run(
                    run_all(
                        project,
                        scope,
                        reuse_mixed_fingerprints=args.reuse_mixed_fingerprints,
                    )
                )
        summary.setdefault("warnings", [])
        summary["warnings"] = [
            *warnings,
            *run_warnings,
            *summary["warnings"],
        ]
        for warning in summary["warnings"]:
            if warning not in warnings and warning not in run_warnings:
                logger.warning("%s", warning)
        emit_summary(summary)
        logger.info(
            "command complete command=%s completed=%s failed=%s pending=%s",
            args.command,
            summary.get("completed", "-"),
            summary.get("failed", "-"),
            summary.get("pending", "-"),
        )
        return 5 if summary.get("failed") or summary.get("pending") else 0
    if args.command == "apply":
        project = _resolve_project(args)
        warnings = sync_global_templates(project, dry_run=args.dry_run)
        for warning in warnings:
            logger.warning("%s", warning)
        lock = nullcontext() if args.dry_run else project_write_lock(project)
        with lock:
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
        emit_summary(summary)
        logger.info(
            "command complete command=apply stage=%s completed=%d",
            args.stage,
            summary["completed"],
        )
        return 0
    if args.command == "export":
        project = _resolve_project(args)
        warnings = sync_global_templates(project)
        for warning in warnings:
            logger.warning("%s", warning)
        with project_write_lock(project):
            summary = export_project(
                project,
                args.stage,
                bilingual=args.bilingual,
                allow_missing=args.allow_missing,
                output_format=args.format,
                file_ids=args.file_ids,
            )
        summary["warnings"] = warnings
        emit_summary(summary)
        logger.info(
            "command complete command=export stage=%s files=%d",
            args.stage,
            summary["files"],
        )
        return 0
    if args.command == "terms-import":
        project = _resolve_project(args)
        warnings = sync_global_templates(project, dry_run=args.dry_run)
        lock = nullcontext() if args.dry_run else project_write_lock(project)
        with lock:
            summary = import_terms(
                project,
                Path(args.file),
                dry_run=args.dry_run,
            )
        summary["warnings"] = [*warnings, *summary["warnings"]]
        emit_summary(summary)
        return 0
    if args.command == "terms-export":
        project = _resolve_project(args)
        summary = export_terms(
            project,
            Path(args.output),
            include_disabled=args.include_disabled,
            source=args.source,
        )
        emit_summary(summary)
        return 0
    if args.command == "terms-publish-partial":
        project = _resolve_project(args)
        with project_write_lock(project):
            summary = publish_partial_terms(project)
        emit_summary(summary)
        return 0


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
