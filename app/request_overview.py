"""Canonical request evidence for diagnostics, independent of provider envelopes."""
from __future__ import annotations

import json
from typing import Any


def llm_overview(messages: list[dict[str, str]], segment_id_map: dict[str, str] | None = None,
                 content: str | None = None) -> dict[str, Any]:
    payload = None
    for message in messages:
        if message.get("role") != "user":
            continue
        try:
            value = json.loads(message["content"])
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict) and any(key in value for key in ("segments", "source_segments", "terms", "summaries")):
            payload = value
            break
    return {"request_kind": "llm", "input": payload, "segment_id_map": dict(segment_id_map or {}),
            "response_content": content}
