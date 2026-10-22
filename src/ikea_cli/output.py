"""Human-readable rendering and the JSON envelope."""

from __future__ import annotations

import json
import shutil
import sys
import unicodedata
from collections.abc import Sequence
from typing import Any, TextIO

from .markets import PriceFormat


def envelope_ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def envelope_error(error: dict[str, Any]) -> dict[str, Any]:
    return {"ok": False, "error": error}


def dump_json(value: Any, stream: TextIO | None = None) -> None:
    stream = stream or sys.stdout
    json.dump(value, stream, indent=2, ensure_ascii=False)
    stream.write("\n")


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 0 if unicodedata.combining(c) else 1 for c in text)


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.2f}"
    return " ".join(str(value).split())


def _truncate(text: str, width: int) -> str:
    if _width(text) <= width:
        return text
    out = ""
    for ch in text:
        if _width(out + ch) > width - 1:
            break
        out += ch
    return out + "…"


def table(rows: Sequence[Sequence[Any]], headers: Sequence[str], *, align_right: Sequence[int] = (), max_width: int | None = None) -> str:
    """Render a plain fixed-width table. The widest text column shrinks to fit the terminal."""

    cells = [[_cell(v) for v in row] for row in rows]
    heads = [str(h) for h in headers]
    widths = [_width(h) for h in heads]
    for row in cells:
        for i, c in enumerate(row):
            widths[i] = max(widths[i], _width(c))
    limit = max_width or shutil.get_terminal_size((120, 24)).columns
    total = sum(widths) + 2 * (len(widths) - 1)
    while total > limit:
        widest = max(range(len(widths)), key=lambda i: widths[i])
        if widths[widest] <= 12:
            break
        widths[widest] -= 1
        total -= 1

    def fmt(row: Sequence[str]) -> str:
        parts = []
        for i, c in enumerate(row):
            c = _truncate(c, widths[i])
            pad = " " * (widths[i] - _width(c))
            parts.append(pad + c if i in align_right else c + pad)
        return "  ".join(parts).rstrip()

    lines = [fmt(heads), fmt(["-" * w for w in widths])]
    lines.extend(fmt(r) for r in cells)
    return "\n".join(lines)


def money(value: Any, currency: str | None = None, fmt: PriceFormat | None = None) -> str:
    """Format a price. With ``fmt`` (the market's price format) it looks like prices on ikea.com."""

    if value is None:
        return "-"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return str(value)
    if fmt is not None:
        return fmt.format(amount, currency)
    return f"{amount:,.2f} {currency}" if currency else f"{amount:,.2f}"


def kv(pairs: Sequence[tuple[str, Any]]) -> str:
    pairs = [(k, v) for k, v in pairs if v not in (None, "", [], {})]
    if not pairs:
        return ""
    width = max(len(k) for k, _ in pairs)
    return "\n".join(f"{k.ljust(width)}  {_cell(v)}" for k, v in pairs)
