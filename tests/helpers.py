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


def write_term_library(project: Path, path: Path, value: dict[str, Any]) -> None:
    """Build a current-format library fixture with explicit unsourced explanations."""
    from copy import deepcopy
    from app.sqlite_storage import write_json
    from app.term_knowledge import add_unsourced, empty_knowledge, entity_members, save_items
    target = value
    value = deepcopy(value)
    if "knowledge" not in value:
        knowledge = empty_knowledge()
        value["knowledge"] = knowledge
        refs = {term["normalized"]: add_unsourced(knowledge, term["normalized"], term["description"], "manual")
                for term in value["terms"] if term.get("description")}
        for root, members in entity_members(value).items():
            save_items(project, value, root, [{"text": knowledge["fragments"][refs[key]]["text"], "fragment_ids": [refs[key]]}
                                             for key in members if key in refs])
    for root, members in entity_members(value).items():
        if root not in value["knowledge"]["entities"]:
            save_items(project, value, members[0], [])
    from app.term_knowledge import project_descriptions
    project_descriptions(project, value)
    write_json(project, path, value)
    target.clear()
    target.update(value)
