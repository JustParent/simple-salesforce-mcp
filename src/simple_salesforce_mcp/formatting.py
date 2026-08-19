"""Response shaping: strip API noise and keep payloads inside model-context budgets."""

from __future__ import annotations

import json

# Soft budget for a record list within one response; the server applies a hard
# cap on the final text as a backstop.
MAX_RESULT_CHARS = 60_000


def strip_attributes(value):
    """Recursively drop Salesforce ``attributes`` keys; nested subquery results
    are rewritten to a compact ``{total_size, done, records}`` shell."""
    if isinstance(value, dict):
        if "records" in value and "done" in value:
            shell = {
                "total_size": value.get("totalSize"),
                "done": value.get("done"),
                "records": [strip_attributes(r) for r in value.get("records") or []],
            }
            if value.get("nextRecordsUrl"):
                shell["next_url"] = value["nextRecordsUrl"]
            return shell
        return {k: strip_attributes(v) for k, v in value.items() if k != "attributes"}
    if isinstance(value, list):
        return [strip_attributes(v) for v in value]
    return value


def to_compact_json(value) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)


def to_pretty_json(value) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def cap_records(records: list, max_chars: int = MAX_RESULT_CHARS) -> tuple[list, int]:
    """Trim a record list so its serialized size stays under ``max_chars``.

    Returns ``(kept_records, dropped_count)``.
    """
    total = 0
    kept = []
    for record in records:
        total += len(to_compact_json(record)) + 1
        if total > max_chars:
            break
        kept.append(record)
    return kept, len(records) - len(kept)
