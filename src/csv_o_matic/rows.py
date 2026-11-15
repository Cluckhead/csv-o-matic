"""Headers and rows as the tools show them: unique column labels, bounded cells, rows cut to fit."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .csvscan import Record
from .output import FIT_NOTES_BYTES, clip, size, some

LABEL_MAX_CHARS = 200  # longer column names are shown shortened


@dataclass
class Header:
    columns: list[str]  # original names, original order
    labels: list[str]  # unique, bounded-length labels used as keys in returned rows
    data_offset: int  # byte offset of the first data record

    @classmethod
    def build(cls, columns: list[str], data_offset: int) -> Header:
        seen: dict[str, int] = {}
        for name in columns:
            seen[name] = seen.get(name, 0) + 1
        labels: list[str] = []
        for i, name in enumerate(columns):
            if len(name) > LABEL_MAX_CHARS:
                labels.append(f"{name[:LABEL_MAX_CHARS]}...[name shortened; {len(name)} characters] [col {i + 1}]")
            elif seen[name] > 1:
                labels.append(f"{name} [col {i + 1}]")
            else:
                labels.append(name)
        # A generated label can coincide with a real column name; labels must still be unique.
        taken: dict[str, int] = {}
        for label in labels:
            taken[label] = taken.get(label, 0) + 1
        used: set[str] = set()
        for i, label in enumerate(labels):
            if taken[label] > 1:
                label = f"{label} [col {i + 1}]"
            while label in used:
                label += "'"
            used.add(label)
            labels[i] = label
        return cls(columns, labels, data_offset)

    def suspect_delimiter(self, delimiter: str) -> str | None:
        """Another delimiter that the header suggests the file really uses, if any."""
        if len(self.columns) != 1:
            return None
        counts = {d: self.columns[0].count(d) for d in (";", "\t", "|", ",") if d != delimiter}
        best = max(counts, key=lambda d: counts[d])
        return best if counts[best] >= 2 else None

    def label(self, index: int) -> str:
        return self.labels[index] if index < len(self.labels) else f"[extra field {index + 1}]"

    def display(self, index: int) -> str:
        """The original name, unless it is too long to show."""
        name = self.columns[index]
        return name if len(name) <= LABEL_MAX_CHARS else self.labels[index]

    def duplicates(self) -> dict[str, Any] | None:
        """A bounded summary of repeated column names."""
        positions: dict[str, list[int]] = {}
        for index, name in enumerate(self.columns):
            positions.setdefault(name, []).append(index + 1)
        repeated = {name: where for name, where in positions.items() if len(where) > 1}
        if not repeated:
            return None
        examples = {}
        for name, where in list(repeated.items())[:8]:
            examples[clip(name, 80)] = {"occurrences": len(where), "first_positions": where[:8]}
        return {
            "repeated_names": len(repeated),
            "examples": examples,
            "note": "Rows label repeated names as 'name [col N]'; select one as '#N'.",
        }


class RowFormatter:
    """Turns parsed records into JSON-ready rows, cutting what is too long to show."""

    def __init__(self, max_cell_chars: int):
        self.max_cell_chars = max_cell_chars

    def cell(self, value: str) -> tuple[str, bool]:
        limit = self.max_cell_chars
        if len(value) <= limit:
            return value, False
        return f"{value[:limit]}...[cell truncated; {len(value)} characters in total]", True

    def row(self, header: Header, record: Record, wanted: list[int] | None,
             matched: list[int] | None = None) -> dict[str, Any]:
        fields = record.fields
        indices = list(dict.fromkeys([*wanted, *(matched or [])])) if wanted is not None else range(len(fields))
        values: dict[str, str] = {}
        cut: list[str] = []
        for i in indices:
            if i >= len(fields):
                continue
            label = header.label(i)
            values[label], was_cut = self.cell(fields[i])
            if was_cut:
                cut.append(label)
        row: dict[str, Any] = {"record_number": record.number, "byte_offset": record.offset}
        if matched is not None:
            names = [header.label(i) for i in matched]
            row["matched_columns"] = some(names, 1200)
            if len(row["matched_columns"]) < len(names):
                row["matched_column_count"] = len(names)
        row["values"] = values
        if cut:
            row["truncated_cells"] = some(cut, 600)
            if len(row["truncated_cells"]) < len(cut):
                row["truncated_cell_count"] = len(cut)
        if len(fields) != len(header.columns):
            row["field_count_mismatch"] = {"header_columns": len(header.columns), "record_fields": len(fields)}
        return row

    @staticmethod
    def fit(row: dict[str, Any], budget: int) -> dict[str, Any]:
        """Drop trailing values so a single over-wide row still fits; say so in the row."""
        if size(row) <= budget:
            return row
        values = row["values"]
        kept: dict[str, str] = {}
        used = size({k: v for k, v in row.items() if k != "values"}) + FIT_NOTES_BYTES
        for label, value in values.items():
            cost = size(label) + size(value) + 4
            if used + cost > budget:
                room = (budget - used - size(label) - 80) // 4
                if not kept and room > 40:  # show at least the start of the first value
                    kept[label] = f"{value[:room]}...[cut to fit; {len(value)} characters in total]"
                break
            used += cost
            kept[label] = value
        row["values"] = kept
        row["values_omitted_for_size"] = len(values) - len(kept)
        row["size_note"] = ("This record is too wide for the output-size limit; trailing columns were left out. "
                            "Request specific columns to see them.")
        return row
