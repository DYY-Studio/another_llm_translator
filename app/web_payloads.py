from __future__ import annotations

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    field_validator,
)


class _WebPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")


class BoundaryPayload(_WebPayload):
    file_id: str = Field(min_length=1)
    part_id: str = Field(min_length=1)


class SegmentFilterPayload(_WebPayload):
    file_id: str | None = None
    part_id: str | None = None
    status: str | None = None
    q: str | None = None
    stage: str = "translation"


class SegmentQueryPayload(SegmentFilterPayload):
    offset: int = 0
    limit: int = 100

    @field_validator("offset", "limit", mode="before")
    @classmethod
    def reject_boolean_window(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("窗口参数必须是整数")  # noqa: TRY004
        try:
            return int(value)
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError("窗口参数必须是整数") from exc


class _SummaryBoundariesPayload(_WebPayload):
    boundaries: list[BoundaryPayload] = Field(
        validation_alias=AliasChoices("boundaries", "selection"),
        description="边界数组；也接受兼容输入别名 selection",
        json_schema_extra={"x-input-aliases": ["selection"]},
    )


class SummarySelectionPayload(_SummaryBoundariesPayload):
    language: str | None = None


class SummaryParticipationPayload(_SummaryBoundariesPayload):
    selected: StrictBool = True


class SummaryExportPayload(_SummaryBoundariesPayload):
    path: str = "summary.md"


class TaskStartPayload(_WebPayload):
    stage: str
    language: str | None = None
    from_file: str | None = None
    only_file: str | None = None
    only_segment: str | None = None
    force: StrictBool = False
    replace_draft: StrictBool = False
    acknowledge_manual_review: StrictBool = False
    include_summaries: StrictBool = False
    reuse_mixed_fingerprints: StrictBool = False
    run_action: str | None = None
    summary_selection: list[BoundaryPayload] = Field(default_factory=list)


class StorageConfirmPayload(_WebPayload):
    confirm: StrictBool


class StorageOutputClearPayload(StorageConfirmPayload):
    path: StrictStr = Field(min_length=1)
