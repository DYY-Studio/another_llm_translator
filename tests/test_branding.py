from __future__ import annotations

from importlib.metadata import metadata

from app.main import build_parser
from app.plugins import load_plugins
from app.web import create_app


def test_release_branding_is_used_by_public_metadata_and_runtime(tmp_path) -> None:
    assert metadata("another-llm-translator")["Name"] == "another-llm-translator"
    assert build_parser().prog == "another-llm-translator"
    assert create_app(projects_root=tmp_path / "projects").title == (
        "Another LLM Translator"
    )
    descriptors = load_plugins()
    assert {item.plugin_id for item in descriptors} >= {
        "srt-documents",
        "term-validation",
    }
