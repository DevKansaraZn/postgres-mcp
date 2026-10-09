"""Rendering query results as compact, readable text for the model."""

from __future__ import annotations

import datetime as _dt
import decimal
import json
import uuid
from typing import Any

from .db import QueryResult


def cell(value: Any, max_chars: int) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, decimal.Decimal, uuid.UUID)):
        text = str(value)
    elif isinstance(value, (_dt.datetime, _dt.date, _dt.time, _dt.timedelta)):
        text = value.isoformat() if not isinstance(value, _dt.timedelta) else str(value)
    elif isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
    elif isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return f"<{len(raw)} bytes: 0x{raw[:16].hex()}{'...' if len(raw) > 16 else ''}>"
    else:
        text = str(value)

    text = text.replace("\r\n", "\n").replace("\n", "\\n").replace("|", "\\|")
    if len(text) > max_chars:
        text = text[:max_chars] + f"... (+{len(text) - max_chars} chars)"
    return text


def table(result: QueryResult, *, max_cell_chars: int = 500) -> str:
    """Render as a markdown table."""
    if not result.columns:
        return "(statement returned no result set)"
    if not result.rows:
        return f"(0 rows)\n\nColumns: {', '.join(result.columns)}"

    header = list(result.columns)
    body = [[cell(v, max_cell_chars) for v in row] for row in result.rows]

    widths = [len(h) for h in header]
    for row in body:
        for i, text in enumerate(row):
            if i < len(widths):
                widths[i] = min(max(widths[i], len(text)), max_cell_chars)

    def line(values: list[str]) -> str:
        return "| " + " | ".join(v.ljust(widths[i]) for i, v in enumerate(values)) + " |"

    out = [line(header), "|" + "|".join("-" * (w + 2) for w in widths) + "|"]
    out.extend(line(row) for row in body)
    return "\n".join(out)


def summarize(result: QueryResult, *, max_cell_chars: int, max_rows: int) -> str:
    """Table plus a one-line footer with row count, timing and truncation."""
    parts = [table(result, max_cell_chars=max_cell_chars)]
    footer = f"\n{len(result.rows)} row(s) in {result.duration_ms:.0f} ms"
    if result.truncated:
        footer += (
            f" — TRUNCATED at the {max_rows}-row limit. "
            "Add LIMIT/WHERE, or raise `max_rows`, to see more."
        )
    parts.append(footer)
    if result.notices:
        parts.append("Notices: " + "; ".join(result.notices))
    return "\n".join(parts)
