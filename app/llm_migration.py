"""Explicit migration of global LLM resources; Run snapshots are never rewritten."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any

from .errors import ConfigError, ExternalError
from .llm_adapter import load_json_adapter, _parse_json_pointer, _resolve_json_pointer
from .llm_preset import load_llm_preset
from .project import APP_ROOT
from .sqlite_storage import atomic_write_json
from .user_config import user_root

# Exact shipped schema-3 definitions. Modified user adapters require manual review.
_LEGACY_ADAPTER_HASHES = {
    "anthropic": "3216c30c7a11090846272b0f02671d341e840c47e376713e2bae24067b2e700e",
    "openai-compatible": "91cda3eda0093cf3fe8781996421d45d840c25a64b497bd1c453c2ca692291db",
    "chatgpt-plan": "1761c0b070d46b1a14fa45cd24ff861b6816793a16363b2baacd7c66608e3c3c",
    "openai-responses": "25b6fef25741aed1023a9adf8f114fc614195329779be5986840b37b9d27bb3e",
    "google-gemini": "dbd2dd88030cd1a8855fb511ec66ce6880c6c7441bfa0435f858b18eb839cadb",
}


def _remove_pointer(body: dict[str, Any], pointer: str) -> Any:
    value = _resolve_json_pointer(body, pointer)
    parents: list[tuple[dict[str, Any], str]] = []
    parent = body
    tokens = _parse_json_pointer(pointer)
    for token in tokens[:-1]:
        if not isinstance(parent, dict) or not isinstance(parent[token], dict):
            raise ConfigError("Effort 迁移路径必须经过 JSON 对象")
        parents.append((parent, token))
        parent = parent[token]
    del parent[tokens[-1]]
    for ancestor, key in reversed(parents):
        if ancestor[key]:
            break
        del ancestor[key]
    return value


def migrate_llm_resources(
    *, root: Path | None = None, builtin_root: Path = APP_ROOT, apply: bool = False,
) -> list[dict[str, str]]:
    root = root if root is not None else user_root()
    changes: dict[Path, dict[str, Any]] = {}
    reports: list[dict[str, str]] = []
    for directory in ("llm_adapters", "llm_presets"):
        for path in sorted((root / directory).glob("*.json")):
            try:
                raw = path.read_bytes()
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ConfigError("资源顶层必须是对象")
                if directory == "llm_adapters":
                    target = builtin_root / directory / path.name
                    if value.get("schema_version") == 4:
                        load_json_adapter(path)
                        continue
                    if not target.is_file() or hashlib.sha256(raw).hexdigest() != _LEGACY_ADAPTER_HASHES.get(path.stem):
                        raise ConfigError("修改过的内建副本或自定义 Adapter，请人工升级协议")
                    upgraded = json.loads(target.read_text(encoding="utf-8"))
                else:
                    if value.get("schema_version") == 8:
                        load_llm_preset(path)
                        continue
                    if value.get("schema_version") != 7:
                        raise ConfigError("自动迁移仅支持 schema 7 Preset，请人工升级")
                    upgraded = deepcopy(value)
                    upgraded.update(schema_version=8, send_temperature=value["credential"]["kind"] != "chatgpt", reasoning_effort=None)
                    upgraded.setdefault("extra_headers", {})
                    relative = Path("llm_adapters") / (str(value["adapter_id"]) + ".json")
                    override = root / relative
                    adapter_path = override if override.is_file() and override not in changes else builtin_root / relative
                    adapter = load_json_adapter(adapter_path)
                    upgraded["send_temperature"] = adapter.capabilities["temperature"] and value["credential"]["kind"] != "chatgpt"
                    effort = adapter.definition.get("reasoning_effort")
                    if effort:
                        found = []
                        for pointer in [effort["body_pointer"], *effort["aliases"]]:
                            try:
                                found.append(_remove_pointer(upgraded["extra_body"], pointer))
                            except ExternalError:
                                continue
                        if found:
                            if any(item != found[0] for item in found) or found[0] not in effort["values"]:
                                raise ConfigError("Extra Body 中 Effort 冲突或不在声明等级中")
                            upgraded["reasoning_effort"] = found[0]
                    adapter.validate_preset(upgraded)
                    adapter.build_request(api_key="migration", model=upgraded["model"], messages=[], temperature=0.2,
                                          max_output_tokens=upgraded["max_output_tokens"], stream=upgraded["stream"],
                                          send_temperature=upgraded["send_temperature"], reasoning_effort=upgraded["reasoning_effort"],
                                          extra_body=upgraded["extra_body"], extra_headers=upgraded["extra_headers"],
                                          session_id="MIGRATION", request_id="MIGRATION")
                changes[path] = upgraded
                reports.append({"path": str(path), "status": "preview"})
            except (ConfigError, ExternalError, OSError, ValueError, KeyError, TypeError) as exc:
                reports.append({"path": str(path), "status": "manual", "detail": str(exc)})
    if apply:
        for report in reports:
            path = Path(report["path"])
            if path not in changes:
                continue
            try:
                backup = path.with_suffix(path.suffix + ".pre-contract-upgrade.bak")
                if backup.exists():
                    raise ConfigError(f"迁移备份已存在：{backup}")
                shutil.copy2(path, backup)
                atomic_write_json(path, changes[path])
                report.update(status="updated", backup=str(backup))
            except (OSError, ConfigError) as exc:
                report.update(status="manual", detail=str(exc))
    return reports


def main() -> int:
    parser = argparse.ArgumentParser(description="预览全局 LLM 协议迁移，不修改历史 Run")
    parser.add_argument("--root", type=Path, help="用户数据根目录")
    parser.add_argument("--apply", action="store_true", help="备份后执行迁移")
    args = parser.parse_args()
    reports = migrate_llm_resources(root=args.root, apply=args.apply)
    print(json.dumps(reports, ensure_ascii=False, indent=2))
    return int(any(item["status"] == "manual" for item in reports))


if __name__ == "__main__":
    raise SystemExit(main())
