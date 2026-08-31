from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def llm_jsonl(records: Iterable[dict[str, Any]]) -> str:
    values = [*records, {"type": "end"}]
    return "\n".join(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        for value in values
    )


def use_llm_preset(
    tmp_path: Path,
    **changes: Any,
) -> None:
    root = tmp_path / "runtime-global"
    preset_path = root / "llm_presets" / "default.json"
    definition = json.loads(preset_path.read_text(encoding="utf-8"))
    definition.update(changes)
    preset_path.write_text(
        json.dumps(definition, ensure_ascii=False), encoding="utf-8"
    )
