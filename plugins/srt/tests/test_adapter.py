from __future__ import annotations

from pathlib import Path

import pytest
from minimal_llm_translator_srt.adapter import SRTDocumentAdapter

from app.errors import IncompleteError, ProjectError, UsageError


def _config(*, fallback_encoding: str = "utf-8") -> dict[str, object]:
    return {
        "input": {
            "encoding_confidence_threshold": 0.6,
            "fallback_encoding": fallback_encoding,
        }
    }


def _import_one(adapter: SRTDocumentAdapter, path: Path, *, recursive: bool = False):
    result = adapter.import_sources(
        [str(path)],
        recursive=recursive,
        config=_config(),
        options={},
    )
    assert len(result.files) == 1
    return result.files[0]


def _segments(item: object) -> list[dict[str, object]]:
    imported = item
    return [
        {
            "segment_id": f"F0001-S{index + 1:06d}",
            "line_index": index,
            "source": source,
            "is_empty": False,
        }
        for index, source in enumerate(imported.segments)  # type: ignore[attr-defined]
    ]


def test_imports_bom_crlf_multiline_and_non_contiguous_cues(tmp_path: Path) -> None:
    source = tmp_path / "episode.srt"
    source.write_bytes(
        (
            "\ufeff3\r\n"
            "00:00:01,005  -->  00:00:03,250\r\n"
            "第一行\r\n"
            "第二行\r\n"
            "\r\n"
            "7\r\n"
            "01:02:03,000 --> 01:02:04,001\r\n"
            "last"
        ).encode("utf-8")
    )

    imported = _import_one(SRTDocumentAdapter(), source)

    assert imported.segments == ("第一行\n第二行", "last")
    assert imported.segment_part_ids == ("document", "document")
    assert imported.encoding_detected == "utf-8-sig"
    assert imported.encoding_used == "utf-8-sig"
    assert imported.opaque_state == {
        "schema_version": 1,
        "cues": [
            {
                "sequence": "3",
                "timing": "00:00:01,005  -->  00:00:03,250",
            },
            {
                "sequence": "7",
                "timing": "01:02:03,000 --> 01:02:04,001",
            },
        ],
    }


def test_imports_utf16_bom(tmp_path: Path) -> None:
    source = tmp_path / "utf16.srt"
    source.write_text("1\n00:00:00,000 --> 00:00:01,000\n字幕\n", encoding="utf-16")

    imported = _import_one(SRTDocumentAdapter(), source)

    assert imported.segments == ("字幕",)
    assert imported.encoding_detected == "utf-16"
    assert imported.encoding_used == "utf-16"


def test_decode_uses_configured_fallback_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "fallback.srt"
    source.write_bytes("1\n00:00:00,000 --> 00:00:01,000\né\n".encode("latin-1"))
    monkeypatch.setattr(
        "minimal_llm_translator_srt.adapter.chardet.detect",
        lambda _data: {"encoding": "utf-8", "confidence": 1.0},
    )
    adapter = SRTDocumentAdapter()
    result = adapter.import_sources(
        [str(source)],
        recursive=False,
        config=_config(fallback_encoding="latin-1"),
        options={},
    )

    assert result.files[0].encoding_detected == "utf-8"
    assert result.files[0].encoding_used == "latin-1"
    assert result.files[0].segments == ("é",)
    assert result.warnings == ("fallback.srt: 首选编码失败，使用 fallback：latin-1",)


@pytest.mark.parametrize(
    "content",
    [
        "0\n00:00:00,000 --> 00:00:01,000\ntext\n",
        "1\n00:00:00,000 --> 00:00:01,000\ntext\n\n1\n00:00:02,000 --> 00:00:03,000\nother\n",
        "1\n00:60:00,000 --> 00:00:01,000\ntext\n",
        "1\n00:00:02,000 --> 00:00:01,000\ntext\n",
        "1\n00:00:00.000 --> 00:00:01.000\ntext\n",
        "00:00:00,000 --> 00:00:01,000\ntext\n",
        "1\n00:00:00,000 --> 00:00:01,000\n\n",
        "1\n00:00:00,000 --> 00:00:01,000\ntext\n\nnot-a-cue\n",
    ],
)
def test_import_rejects_invalid_core_srt(tmp_path: Path, content: str) -> None:
    source = tmp_path / "broken.srt"
    source.write_text(content, encoding="utf-8")

    with pytest.raises(UsageError):
        _import_one(SRTDocumentAdapter(), source)


def test_import_rejects_wrong_extension_and_symlink(tmp_path: Path) -> None:
    adapter = SRTDocumentAdapter()
    wrong = tmp_path / "input.txt"
    wrong.write_text("text", encoding="utf-8")
    with pytest.raises(UsageError, match="扩展名"):
        _import_one(adapter, wrong)

    link = tmp_path / "link.srt"
    try:
        link.symlink_to(wrong)
    except OSError:
        pytest.skip("当前文件系统不支持符号链接")
    with pytest.raises(UsageError, match="符号链接"):
        _import_one(adapter, link)


def test_imports_directory_recursively_in_natural_order(tmp_path: Path) -> None:
    root = tmp_path / "subs"
    (root / "nested").mkdir(parents=True)
    for relative in ("part10.srt", "part2.srt", "nested/part1.srt"):
        (root / relative).write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n" + relative,
            encoding="utf-8",
        )

    result = SRTDocumentAdapter().import_sources(
        [str(root)],
        recursive=True,
        config=_config(),
        options={},
    )

    assert [item.original_name for item in result.files] == [
        "nested/part1.srt",
        "part2.srt",
        "part10.srt",
    ]


def test_normalize_model_output_rejects_empty_separator_lines() -> None:
    adapter = SRTDocumentAdapter()
    segment = {"segment_id": "F0001-S000001"}
    assert (
        adapter.normalize_model_output(
            segment=segment, text="第一行\r\n第二行", stage="translation"
        )
        == "第一行\n第二行"
    )
    with pytest.raises(IncompleteError, match="空白分隔行"):
        adapter.normalize_model_output(
            segment=segment, text="第一行\n\n第二行", stage="translation"
        )
    with pytest.raises(IncompleteError, match="为空"):
        adapter.normalize_model_output(segment=segment, text="  ", stage="translation")


def test_export_preserves_cue_metadata_and_supports_bilingual(
    tmp_path: Path,
) -> None:
    adapter = SRTDocumentAdapter()
    source = tmp_path / "episode.srt"
    source.write_text(
        "3\n00:00:01,000 --> 00:00:02,000\n源一\n\n"
        "7\n00:00:03,000 --> 00:00:04,000\n源二\n",
        encoding="utf-8",
    )
    imported = _import_one(adapter, source)
    segments = _segments(imported)
    output = tmp_path / "staging"
    written = adapter.export_sources(
        project=tmp_path,
        staging_dir=output,
        file={"original_name": "episode.srt"},
        segments=segments,
        output_text={
            "F0001-S000001": "译一",
            "F0001-S000002": "译二\n第二行",
        },
        bilingual=True,
        output_encoding="utf-8-sig",
        target_language="简体中文",
        target_language_tag="zh-Hans",
        opaque_state=imported.opaque_state,
    )

    assert written == [Path("episode.srt")]
    assert (output / "episode.srt").read_text(encoding="utf-8-sig") == (
        "3\n00:00:01,000 --> 00:00:02,000\n源一\n译一\n\n"
        "7\n00:00:03,000 --> 00:00:04,000\n源二\n译二\n第二行\n"
    )


def test_export_rejects_corrupt_state_and_missing_output(tmp_path: Path) -> None:
    adapter = SRTDocumentAdapter()
    segments = [
        {
            "segment_id": "F0001-S000001",
            "line_index": 0,
            "source": "源",
            "is_empty": False,
        }
    ]
    with pytest.raises(IncompleteError, match="状态"):
        adapter.export_sources(
            project=tmp_path,
            staging_dir=tmp_path / "staging",
            file={"original_name": "broken.srt"},
            segments=segments,
            output_text={},
            bilingual=False,
            output_encoding="utf-8",
            target_language="简体中文",
            target_language_tag="zh-Hans",
            opaque_state={"schema_version": 1, "cues": []},
        )

    with pytest.raises(IncompleteError, match="Segment 译文"):
        adapter.export_sources(
            project=tmp_path,
            staging_dir=tmp_path / "staging",
            file={"original_name": "missing.srt"},
            segments=segments,
            output_text={},
            bilingual=False,
            output_encoding="utf-8",
            target_language="简体中文",
            target_language_tag="zh-Hans",
            opaque_state={
                "schema_version": 1,
                "cues": [
                    {
                        "sequence": "1",
                        "timing": "00:00:00,000 --> 00:00:01,000",
                    }
                ],
            },
        )


def test_export_rejects_non_contiguous_segment_lines(tmp_path: Path) -> None:
    with pytest.raises(IncompleteError, match="行号不连续"):
        SRTDocumentAdapter().export_sources(
            project=tmp_path,
            staging_dir=tmp_path / "staging",
            file={"original_name": "broken.srt"},
            segments=[
                {
                    "segment_id": "F0001-S000002",
                    "line_index": 1,
                    "source": "源",
                    "is_empty": False,
                }
            ],
            output_text={"F0001-S000002": "译"},
            bilingual=False,
            output_encoding="utf-8",
            target_language="简体中文",
            target_language_tag="zh-Hans",
            opaque_state={
                "schema_version": 1,
                "cues": [
                    {
                        "sequence": "1",
                        "timing": "00:00:00,000 --> 00:00:01,000",
                    }
                ],
            },
        )


def test_export_rejects_unsafe_output_name(tmp_path: Path) -> None:
    with pytest.raises(IncompleteError, match="相对路径无效"):
        SRTDocumentAdapter().export_sources(
            project=tmp_path,
            staging_dir=tmp_path / "staging",
            file={"original_name": "../outside.srt"},
            segments=[],
            output_text={},
            bilingual=False,
            output_encoding="utf-8",
            target_language="简体中文",
            target_language_tag="zh-Hans",
            opaque_state={"schema_version": 1, "cues": []},
        )


def test_import_rejects_invalid_input_config(tmp_path: Path) -> None:
    source = tmp_path / "episode.srt"
    source.write_text("1\n00:00:00,000 --> 00:00:01,000\ntext\n", encoding="utf-8")
    with pytest.raises(ProjectError, match="input 配置"):
        SRTDocumentAdapter().import_sources(
            [str(source)], recursive=False, config={}, options={}
        )
