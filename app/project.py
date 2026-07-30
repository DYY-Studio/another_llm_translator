from __future__ import annotations

import codecs
import hashlib
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import chardet

from .config import load_config
from .errors import ConfigError, ProjectError, UsageError
from .llm_adapter import load_json_adapter
from .storage import (
    atomic_write_json,
    new_record_id,
    read_json,
    read_jsonl,
    record_header,
    utc_now,
    write_jsonl,
)

_SOURCE_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = (
    _SOURCE_ROOT
    if (_SOURCE_ROOT / "config" / "config.toml").is_file()
    else Path(sys.prefix)
)
GLOBAL_CONFIG = APP_ROOT / "config" / "config.toml"
GLOBAL_PROMPTS = APP_ROOT / "prompts"
PROJECTS_ROOT = APP_ROOT / "projects"
PROMPT_NAMES = (
    "terminology.middle.txt",
    "translation.middle.txt",
    "proofreading.middle.txt",
    "polishing.middle.txt",
)


@dataclass(frozen=True)
class InputFile:
    path: Path
    original_name: str


def _natural_key(value: str) -> list[tuple[int, int | str]]:
    parts = re.split(r"(\d+)", value.casefold())
    return [(0, int(part)) if part.isdigit() else (1, part) for part in parts]


def _directory_txt_files(root: Path, recursive: bool) -> list[InputFile]:
    candidates: list[Path] = []
    if recursive:
        for current, dirs, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            dirs[:] = sorted(
                [name for name in dirs if not (current_path / name).is_symlink()],
                key=_natural_key,
            )
            for name in files:
                path = current_path / name
                if path.is_symlink() or path.suffix.casefold() != ".txt":
                    continue
                candidates.append(path)
    else:
        candidates = [
            path
            for path in root.iterdir()
            if path.is_file()
            and not path.is_symlink()
            and path.suffix.casefold() == ".txt"
        ]
    candidates.sort(key=lambda path: _natural_key(path.relative_to(root).as_posix()))
    return [
        InputFile(path=path, original_name=path.relative_to(root).as_posix())
        for path in candidates
    ]


def discover_inputs(values: Iterable[str], recursive: bool) -> list[InputFile]:
    discovered: list[InputFile] = []
    for raw_value in values:
        path = Path(raw_value)
        if path.is_symlink():
            raise UsageError(f"不接受显式符号链接输入：{path}")
        if path.is_dir():
            discovered.extend(_directory_txt_files(path, recursive))
        elif path.is_file() and path.suffix.casefold() == ".txt":
            discovered.append(InputFile(path=path, original_name=path.name))
        else:
            raise UsageError(f"输入不是有效 TXT 文件或目录：{path}")
    if not discovered:
        raise UsageError("没有发现 TXT 输入文件")
    names_by_key: dict[str, list[str]] = {}
    for item in discovered:
        names_by_key.setdefault(item.original_name.casefold(), []).append(
            item.original_name
        )
    duplicates = sorted(
        {
            name
            for values in names_by_key.values()
            if len(values) > 1
            for name in values
        }
    )
    if duplicates:
        raise UsageError(f"重复导出相对路径：{', '.join(duplicates)}")
    return discovered


def decode_txt(
    data: bytes,
    *,
    confidence_threshold: float,
    fallback_encoding: str,
) -> tuple[str, str, str, float, list[str]]:
    warnings: list[str] = []
    detected = ""
    confidence = 1.0
    encoding: str
    if data.startswith(codecs.BOM_UTF32_LE) or data.startswith(codecs.BOM_UTF32_BE):
        detected = encoding = "utf-32"
    elif data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE):
        detected = encoding = "utf-16"
    elif data.startswith(codecs.BOM_UTF8):
        detected = encoding = "utf-8-sig"
    else:
        result = chardet.detect(data)
        detected = str(result.get("encoding") or "utf-8")
        confidence = float(result.get("confidence") or 0.0)
        normalized = detected.casefold().replace("_", "-")
        if normalized in {"gb2312", "gbk"}:
            encoding = "gb18030"
        elif normalized == "ascii":
            encoding = "utf-8"
        else:
            encoding = detected
        if confidence < confidence_threshold:
            warnings.append(
                f"编码探测置信度较低：{detected} ({confidence:.2f})"
            )
    try:
        text = data.decode(encoding, errors="strict")
        used = encoding
    except (LookupError, UnicodeDecodeError):
        try:
            text = data.decode(fallback_encoding, errors="strict")
            used = fallback_encoding
            warnings.append(f"首选编码失败，使用 fallback：{fallback_encoding}")
        except (LookupError, UnicodeDecodeError) as exc:
            raise ProjectError(
                f"无法使用 {encoding} 或 {fallback_encoding} 严格解码"
            ) from exc
    return text, detected, used, confidence, warnings


def bundle_hash(app_root: Path = APP_ROOT) -> str:
    config = load_config(app_root / "config" / "config.toml")
    adapter_id = str(config["llm"]["adapter"])
    adapter = load_json_adapter(
        app_root / "llm_adapters" / f"{adapter_id}.json"
    )
    if adapter.adapter_id != adapter_id:
        raise ConfigError(
            "全局 LLM Adapter 文件中的 adapter_id 与配置不一致"
        )
    paths = [app_root / "config" / "config.toml"] + [
        app_root / "prompts" / name for name in PROMPT_NAMES
    ] + [app_root / "llm_adapters" / f"{adapter_id}.json"]
    digest = hashlib.sha256()
    for path in paths:
        if not path.is_file():
            raise ConfigError(f"全局模板缺失：{path}")
        relative = path.relative_to(app_root).as_posix().encode()
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return f"sha256:{digest.hexdigest()}"


def _copy_bundle(source_root: Path, target: Path) -> None:
    shutil.copy2(source_root / "config" / "config.toml", target / "config.toml")
    prompt_target = target / "prompts"
    prompt_target.mkdir(parents=True, exist_ok=True)
    for name in PROMPT_NAMES:
        shutil.copy2(source_root / "prompts" / name, prompt_target / name)
    config = load_config(source_root / "config" / "config.toml")
    adapter_id = str(config["llm"]["adapter"])
    adapter_source = source_root / "llm_adapters" / f"{adapter_id}.json"
    if not adapter_source.is_file():
        raise ConfigError(f"全局 LLM Adapter 缺失：{adapter_source}")
    adapter_target = target / "llm_adapters"
    adapter_target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(adapter_source, adapter_target / adapter_source.name)


def init_project(
    inputs: list[str],
    *,
    name: str,
    recursive: bool = False,
    dry_run: bool = False,
    app_root: Path = APP_ROOT,
    projects_root: Path | None = None,
) -> tuple[Path | None, dict[str, object]]:
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or "\0" in name
    ):
        raise UsageError("项目名不能为空，也不能包含路径分隔符")
    projects_root = projects_root or app_root / "projects"
    global_config = load_config(app_root / "config" / "config.toml")
    global_hash = bundle_hash(app_root)
    files = discover_inputs(inputs, recursive)
    decoded: list[tuple[InputFile, str, str, str, float, list[str]]] = []
    for item in files:
        text, detected, used, confidence, warnings = decode_txt(
            item.path.read_bytes(),
            confidence_threshold=global_config["input"][
                "encoding_confidence_threshold"
            ],
            fallback_encoding=global_config["input"]["fallback_encoding"],
        )
        decoded.append((item, text, detected, used, confidence, warnings))

    summary: dict[str, object] = {
        "project_name": name,
        "file_count": len(decoded),
        "segment_count": sum(
            len(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
            for _, text, *_ in decoded
        ),
        "warnings": [
            f"{item.original_name}: {warning}"
            for item, _, _, _, _, warnings in decoded
            for warning in warnings
        ],
    }
    if dry_run:
        return None, summary

    projects_root.mkdir(parents=True, exist_ok=True)
    target = projects_root / name
    if target.exists():
        raise UsageError(f"项目已存在：{target}")
    project_id = new_record_id("PRJ")
    temp = Path(tempfile.mkdtemp(prefix=f".{name}.", dir=projects_root))
    try:
        _copy_bundle(app_root, temp)
        file_records: list[dict[str, object]] = []
        segment_records: list[dict[str, object]] = []
        for file_order, (item, text, detected, used, confidence, _) in enumerate(
            decoded, start=1
        ):
            file_id = f"F{file_order:04d}"
            relative = Path(item.original_name)
            stored_name = relative.parent / f"{file_id}__{relative.name}"
            stored_path = temp / "input" / stored_name
            stored_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item.path, stored_path)

            normalized = text.replace("\r\n", "\n").replace("\r", "\n")
            segments = normalized.split("\n")
            file_records.append(
                record_header(
                    "source_file",
                    project_id,
                    record_id=f"FILE-{file_id}",
                    file_id=file_id,
                    file_order=file_order,
                    original_name=item.original_name,
                    stored_name=stored_name.as_posix(),
                    encoding_detected=detected,
                    encoding_confidence=confidence,
                    encoding_used=used,
                    segment_count=len(segments),
                )
            )
            for line_index, source in enumerate(segments):
                segment_id = f"{file_id}-S{line_index + 1:06d}"
                segment_records.append(
                    record_header(
                        "source_segment",
                        project_id,
                        record_id=segment_id,
                        segment_id=segment_id,
                        file_id=file_id,
                        line_index=line_index,
                        source=source,
                        is_empty=source == "" or source.isspace(),
                    )
                )

        write_jsonl(temp / "source" / "files.jsonl", file_records)
        write_jsonl(temp / "source" / "segments.jsonl", segment_records)
        (temp / "terminology").mkdir(parents=True, exist_ok=True)
        (temp / "stages").mkdir(parents=True, exist_ok=True)
        (temp / "runs").mkdir(parents=True, exist_ok=True)
        (temp / "logs").mkdir(parents=True, exist_ok=True)
        (temp / "output").mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            temp / "terminology" / "overrides.json",
            record_header(
                "terminology_overrides",
                project_id,
                record_id="TERMINOLOGY-OVERRIDES",
                overrides=[],
            ),
        )
        atomic_write_json(
            temp / "project.json",
            record_header(
                "project",
                project_id,
                record_id=project_id,
                name=name,
                global_bundle_hash_seen=global_hash,
                file_count=len(file_records),
                segment_count=len(segment_records),
                status="active",
            ),
        )
        os.replace(temp, target)
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    return target, summary


def resolve_project(value: str, projects_root: Path = PROJECTS_ROOT) -> Path:
    direct = Path(value)
    path = direct if direct.is_dir() else projects_root / value
    if not (path / "project.json").is_file():
        raise ProjectError(f"项目不存在或无效：{value}")
    return path


def sync_global_templates(
    project: Path,
    *,
    dry_run: bool = False,
    app_root: Path = APP_ROOT,
    interactive: bool | None = None,
    choice: str | None = None,
) -> list[str]:
    metadata = read_json(project / "project.json")
    try:
        load_config(app_root / "config" / "config.toml")
        current_hash = bundle_hash(app_root)
    except ConfigError as exc:
        return [f"全局模板无效，继续使用项目副本：{exc}"]
    if metadata.get("global_bundle_hash_seen") == current_hash:
        return []
    global_config = load_config(app_root / "config" / "config.toml")
    adapter_id = str(global_config["llm"]["adapter"])
    bundle_files = [Path("config.toml")] + [
        Path("prompts") / name for name in PROMPT_NAMES
    ] + [Path("llm_adapters") / f"{adapter_id}.json"]
    changed = []
    for relative in bundle_files:
        global_path = (
            app_root / "config" / "config.toml"
            if relative == Path("config.toml")
            else app_root / relative
        )
        project_path = project / relative
        if (
            not project_path.is_file()
            or project_path.read_bytes() != global_path.read_bytes()
        ):
            changed.append(relative.as_posix())
    warnings = [f"发现新的全局模板：{', '.join(changed) or '内容版本变化'}"]
    if dry_run:
        return warnings
    interactive = os.isatty(0) if interactive is None else interactive
    if not interactive:
        warnings.append("非交互环境保留项目副本；未更新 seen Hash")
        return warnings
    if choice is None:
        print(
            f"{warnings[0]}\n更新项目模板？[u]pdate/[k]eep: ",
            end="",
            file=sys.stderr,
            flush=True,
        )
        answer = input().strip().casefold()
        choice = "update" if answer in {"u", "update"} else "keep"
    if choice not in {"update", "keep"}:
        raise UsageError("模板选择必须是 update 或 keep")
    if choice == "update":
        load_config(app_root / "config" / "config.toml")
        timestamp = utc_now().replace(":", "").replace("-", "")
        backup = project / "snapshots" / "template_updates" / timestamp
        backup.mkdir(parents=True, exist_ok=False)
        shutil.copy2(project / "config.toml", backup / "config.toml")
        shutil.copytree(project / "prompts", backup / "prompts")
        if (project / "llm_adapters").exists():
            shutil.copytree(
                project / "llm_adapters", backup / "llm_adapters"
            )
        _copy_bundle(app_root, project)
        warnings.append(f"已更新项目模板；备份位于 {backup}")
    else:
        warnings.append("已保留项目模板")
    metadata["global_bundle_hash_seen"] = current_hash
    atomic_write_json(project / "project.json", metadata)
    return warnings


def load_source_files(
    project: Path, *, repair_tail: bool = True
) -> list[dict[str, object]]:
    return read_jsonl(project / "source" / "files.jsonl", repair_tail=repair_tail)


def load_segments(
    project: Path, *, repair_tail: bool = True
) -> list[dict[str, object]]:
    return read_jsonl(project / "source" / "segments.jsonl", repair_tail=repair_tail)
