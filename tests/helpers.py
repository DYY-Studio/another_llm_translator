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
    monkeypatch: Any,
    **changes: Any,
) -> None:
    root = tmp_path / "runtime-global"
    presets = root / "llm_presets"
    presets.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).parents[1] / "llm_presets" / "default.json"
    definition = json.loads(source.read_text(encoding="utf-8"))
    definition.update(changes)
    (presets / "default.json").write_text(
        json.dumps(definition, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr("app.config.APP_ROOT", root)
