from __future__ import annotations

from importlib.metadata import entry_points, metadata

from app.main import build_parser
from app.plugins import PLUGIN_ENTRY_POINT
from app.web import create_app


def test_release_branding_is_used_by_public_metadata_and_runtime(tmp_path) -> None:
    assert metadata("another-llm-translator")["Name"] == "another-llm-translator"
    assert build_parser().prog == "another-llm-translator"
    assert create_app(projects_root=tmp_path / "projects").title == (
        "Another LLM Translator"
    )
    assert sorted([
        (entry.name, entry.value)
        for entry in entry_points(group=PLUGIN_ENTRY_POINT)
        if entry.name in {"srt", "term-validation"}
    ]) == [
        ("srt", "another_llm_translator_srt.plugin:descriptor"),
        (
            "term-validation",
            "another_llm_translator_term_validation.plugin:descriptor",
        ),
    ]
