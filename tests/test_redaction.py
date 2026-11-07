import json

import pytest

from safe_db_mcp.security.redaction import apply_redaction


def base_result(rows):
    return {
        "rows": rows, "returned_rows": len(rows), "result_bytes": 0,
        "truncated": False, "duration_ms": 1, "scanned_rows": None,
    }


def row_bytes(row):
    return len(json.dumps(row, ensure_ascii=False, default=str).encode("utf-8"))


def test_mask_replaces_every_character():
    result = apply_redaction(
        base_result([{"payment_token": "abc123"}]), {"payment_token": "mask"}
    )
    assert result["rows"] == [{"payment_token": "******"}]


def test_hash_is_deterministic_and_truncated():
    result = apply_redaction(
        base_result([{"payment_token": "abc123"}, {"payment_token": "abc123"}]),
        {"payment_token": "hash"},
    )
    first, second = (row["payment_token"] for row in result["rows"])
    assert first == second
    assert len(first) == 12


def test_redact_and_null_policies():
    result = apply_redaction(
        base_result([{"a": "x", "b": "y"}]), {"a": "redact", "b": "null"}
    )
    assert result["rows"] == [{"a": "[REDACTED]", "b": None}]


def test_matching_is_case_insensitive_in_both_directions():
    result = apply_redaction(
        base_result([{"PAYMENT_TOKEN": "v"}]), {"payment_token": "mask"}
    )
    assert result["rows"] == [{"PAYMENT_TOKEN": "*"}]
    result = apply_redaction(
        base_result([{"payment_token": "v"}]), {"Payment_Token": "mask"}
    )
    assert result["rows"] == [{"payment_token": "*"}]


def test_non_matching_columns_untouched():
    result = apply_redaction(
        base_result([{"id": 1, "payment_token": "v"}]), {"payment_token": "mask"}
    )
    assert result["rows"] == [{"id": 1, "payment_token": "*"}]


def test_result_bytes_recomputed_after_redaction():
    result = apply_redaction(
        base_result([{"payment_token": "long-secret-value"}]),
        {"payment_token": "redact"},
    )
    assert result["result_bytes"] == row_bytes({"payment_token": "[REDACTED]"})


def test_empty_policies_return_result_unchanged():
    original = base_result([{"id": 1}])
    assert apply_redaction(original, {}) is original


def test_null_values_stay_null():
    result = apply_redaction(
        base_result([{"payment_token": None}]), {"payment_token": "mask"}
    )
    assert result["rows"] == [{"payment_token": None}]


def test_unknown_policy_raises():
    with pytest.raises(ValueError, match="Unknown redaction policy"):
        apply_redaction(
            base_result([{"payment_token": "v"}]), {"payment_token": "plain"}
        )
