"""Independent second-layer observation scrubber (§2.4)."""

from __future__ import annotations

from typing import Any

from collectors.shared.text_rules_l2 import scrub_record as _shared_scrub_record


def scrub_record(record: dict[str, Any]) -> dict[str, Any] | None:
    """Drop secret-like records, mask PII, and recompute code-point length."""
    return _shared_scrub_record(record)
