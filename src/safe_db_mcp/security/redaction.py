from __future__ import annotations

import hashlib
import json


def apply_redaction(result: dict, policies: dict[str, str]) -> dict:
    """Redact result column values before the result is persisted or returned.

    Matching is case-insensitive in both directions and errs on the side of
    over-redaction: a dialect may report column names in any case.
    """
    lowered = {name.casefold(): policy for name, policy in policies.items()}
    if not lowered:
        return result
    rows = result["rows"]
    redacted_rows = []
    byte_count = 0
    for row in rows:
        if isinstance(row, dict):
            row = {
                key: (
                    _redact_value(value, lowered[key.casefold()])
                    if key.casefold() in lowered else value
                )
                for key, value in row.items()
            }
        redacted_rows.append(row)
        byte_count += len(
            json.dumps(row, ensure_ascii=False, default=str).encode("utf-8")
        )
    return {**result, "rows": redacted_rows, "result_bytes": byte_count}


def _redact_value(value, policy: str):
    if value is None:
        return None
    if policy == "null":
        return None
    text = str(value)
    if policy == "mask":
        return "*" * len(text)
    if policy == "hash":
        # Deterministic so analysts can still compare/join on equality.
        # Short or low-entropy values remain dictionary-attackable: hash
        # policies are not protection for values drawn from a small domain.
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    if policy == "redact":
        return "[REDACTED]"
    raise ValueError(f"Unknown redaction policy: {policy}")
