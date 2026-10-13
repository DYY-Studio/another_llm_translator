"""One-time conversion of scalar explanations; never infer source citations."""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.errors import UsageError
from app.locking import project_write_lock
from app.sqlite_storage import database_path, list_runs, read_json, record_exists, write_terminology_publication
from app.term_knowledge import add_unsourced, empty_knowledge, entity_members, save_items, evidence, project_descriptions


def migrate(project: Path) -> Path:
    with project_write_lock(project):
        path = project / "terminology" / "terms.json"
        if not record_exists(project, path):
            raise UsageError("没有旧术语库需要迁移")
        library = read_json(project, path)
        if "knowledge" in library:
            raise UsageError("术语库已采用实体说明结构")
        active_path = project / "terminology" / "active_task.json"
        if record_exists(project, active_path) and read_json(project, active_path).get("status") in {"active", "partial_published"}:
            raise UsageError("请先结束旧活动扫描")
        if any(r.get("stage") in {"terminology", "terminology_decision"}
               and r.get("status") == "running"
               for r in list_runs(project)):
            raise UsageError("请先结束旧术语 Run")
        if any(r.get("decision_status") in {"pending", "generating"} for r in list_runs(project, stage="terminology_decision")):
            raise UsageError("请先应用或丢弃旧决策草案")
        overrides = read_json(project, project / "terminology" / "overrides.json")
        backup = project / "backups" / f"term-knowledge-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.sqlite"
        backup.parent.mkdir(exist_ok=True)
        with sqlite3.connect(database_path(project)) as source, sqlite3.connect(backup) as target:
            source.backup(target)
        knowledge = empty_knowledge()
        library["knowledge"] = knowledge
        texts = {term["normalized"]: term.get("description", "") for term in library["terms"]}
        for override in overrides.get("overrides", []):
            if "description" in override:
                texts[override["normalized"]] = override.pop("description")
        refs = {owner: add_unsourced(knowledge, owner, text, "legacy") for owner, text in texts.items() if text}
        known = evidence(project, library)
        for root, members in entity_members(library).items():
            save_items(project, library, root, [
                {"text": knowledge["fragments"][refs[key]]["text"], "fragment_ids": [refs[key]]}
                for key in members if key in refs
            ], all_evidence=known)
        active = {term["normalized"] for term in library["terms"]}
        for owner in refs.keys() - active:
            archived = {**library, "terms": [*library["terms"], {"normalized": owner, "group_primary": None}]}
            save_items(project, archived, owner, [{"text": texts[owner], "fragment_ids": [refs[owner]]}], all_evidence=known)
        project_descriptions(project, library, all_evidence=known)
        library["terms_revision"] += 1
        write_terminology_publication(project, terms=library, overrides=overrides)
        return backup


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    args = parser.parse_args()
    try:
        backup_path = migrate(args.project.resolve())
    except UsageError as exc:
        parser.exit(1, str(exc) + "\n")
    print(f"迁移完成，SQLite 备份：{backup_path}")
