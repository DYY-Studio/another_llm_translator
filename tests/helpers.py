from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any


def llm_jsonl(records: Iterable[dict[str, Any]]) -> str:
    values = [*records, {"type": "end"}]
    return "\n".join(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        for value in values
    )
