"""Keeping every response inside the output-size limit.

Responses are built to fit: each tool measures the fixed part of its answer, sets aside a
reserve for what is added afterwards, and spends the rest through a `Budget`. The reserves
are all named here, so a change to what a response carries has one place to be accounted for.
`server.respond` is the backstop if an estimate is ever wrong.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, TypeVar

T = TypeVar("T")

SEPARATOR_BYTES = 2  # the ", " between two items of a JSON list

# Reserves: bytes held back from a budget for parts of the response that are written later.
TOOL_NAME_BYTES = 60  # the "tool" key the server adds to every response
ENVELOPE_RESERVE_BYTES = 3_500  # listings and process maps: everything other than the items
SCAN_RESERVE_BYTES = 2_800  # searches: scan status, explanation, cursor and warnings
MIN_ROWS_BYTES = 400  # searches: rows get at least this much, however long the request echo
HEADER_NOTES_RESERVE_BYTES = 700  # inspect_csv_headers: offsets, truncation and sample notes
COLUMN_SEARCH_RESERVE_BYTES = 600  # search_column_names: counts, flags and suggestions
COMPARE_STATUS_RESERVE_BYTES = 1_200  # compare_security_records: status and explanation
PAIR_NOTES_BYTES = 400  # one compared pair: its truncation notes
FIT_NOTES_BYTES = 260  # one over-wide row: the notes saying that values were left out


def size(obj: Any) -> int:
    """Bytes `obj` takes up in a response."""
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8", "replace"))


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}...[{len(text)} characters]"


class Budget:
    """The bytes still free in one part of a response."""

    def __init__(self, room: int):
        self.room = room

    def take(self, cost: int, *, force: bool = False) -> bool:
        """Spend `cost` bytes if they are available; with `force`, spend them regardless."""
        if cost > self.room and not force:
            return False
        self.room -= cost
        return True

    def add(self, item: Any, *, force: bool = False) -> bool:
        """Spend what `item` costs as one more entry of a list."""
        return self.take(size(item) + SEPARATOR_BYTES, force=force)

    def give_back(self, cost: int) -> None:
        self.room += cost

    def leading(self, items: Iterable[T]) -> list[T]:
        """As many leading items as fit; callers report the full count separately."""
        out: list[T] = []
        for item in items:
            if not self.add(item):
                break
            out.append(item)
        return out


def some(names: list[str], max_bytes: int) -> list[str]:
    """As many leading names as make a list of at most `max_bytes`."""
    return Budget(max_bytes - SEPARATOR_BYTES).leading(names)
