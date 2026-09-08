from __future__ import annotations

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
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
            raise ValueError("窗口参数必须是整数")
        return value


class SummarySelectionPayload(_WebPayload):
    boundaries: list[BoundaryPayload] = Field(
        validation_alias=AliasChoices("boundaries", "selection")
    )


class SummaryParticipationPayload(SummarySelectionPayload):
    selected: StrictBool = True


class SummaryExportPayload(SummarySelectionPayload):
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
