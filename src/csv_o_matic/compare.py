"""Comparing one security's records between two files.

`service` finds the records; this module decides how they line up (`ComparisonReport`)
and writes the comparison into the response, inside its size budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .csvscan import Record
from .output import PAIR_NOTES_BYTES, Budget, clip, size, some
from .rows import Header, RowFormatter
from .search import MATCH_TYPE

MAX_LISTED_COLUMNS = 50  # columns shown for records that are listed rather than compared
_LISTS = ("comparisons", "ambiguous_keys", "unmatched_records_in_file_a", "unmatched_records_in_file_b")

Key = tuple[str | None, ...]  # a record's values in the match_keys columns


@dataclass
class Pairing:
    """Which columns of file A go with which columns of file B. All values are column indices."""

    id_a: int
    id_b: int
    pairs: list[tuple[int, int]]  # columns whose values are compared
    keys: list[tuple[int, int]]  # match_keys: columns that pair up records
    only_a: list[str]  # labels of file A columns left without a counterpart
    explicit: bool  # the caller named the columns to compare


@dataclass
class SideScan:
    """The search for the security in one of the two files."""

    records: list[Record]
    presence: dict[str, Any]
    warnings: list[str]
    interrupted: bool  # cancelled or out of time: nothing further should be scanned

    @property
    def complete(self) -> bool:
        return bool(self.presence["scan_complete"])

    @classmethod
    def not_scanned(cls, explanation: str) -> SideScan:
        return cls([], {"outcome": "inconclusive", "matching_records_found": 0, "scan_complete": False,
                        "stop_reason": "not_scanned", "explanation": explanation}, [], interrupted=True)


def describe_pairing(result: dict[str, Any], pairing: Pairing, head_a: Header, head_b: Header,
                     identifier_a: str, identifier_b: str) -> None:
    """Echo the request as it was resolved: identifiers, key columns and compared columns."""
    label_a, label_b = head_a.labels, head_b.labels
    search: dict[str, Any] = {
        "identifier_a": identifier_a, "identifier_column_a": label_a[pairing.id_a],
        "identifier_b": identifier_b, "identifier_column_b": label_b[pairing.id_b],
        "match_type": MATCH_TYPE,
        "match_keys": [{"a": label_a[a], "b": label_b[b]} for a, b in pairing.keys],
    }
    if pairing.explicit:
        listed = Budget(1200).leading({"a": label_a[a], "b": label_b[b]} for a, b in pairing.pairs)
        search["compared_columns"] = listed
        if len(listed) < len(pairing.pairs):
            search["compared_column_count"] = len(pairing.pairs)
    else:
        search["compared_columns"] = f"all {len(pairing.pairs)} columns of file A with exactly one counterpart in file B"
        used_b = {b for _, b in pairing.pairs}
        only_b = [label for i, label in enumerate(label_b) if i not in used_b]
        result["columns_not_compared"] = {
            "only_in_file_a_count": len(pairing.only_a), "only_in_file_a": some(pairing.only_a, 500),
            "only_in_file_b_count": len(only_b), "only_in_file_b": some(only_b, 500),
            "note": "Columns are paired by identical name unless column_mapping says otherwise; "
                    "repeated names are not paired.",
        }
    result["search"] = search


class ComparisonReport:
    """Lines up the records found in two files and writes the comparison into `result`.

    Everything is added only while it fits in `budget`; what is left out is counted.
    """

    def __init__(self, result: dict[str, Any], budget: Budget, rows: RowFormatter,
                 head_a: Header, head_b: Header, pairing: Pairing):
        self.result = result
        self.budget = budget
        self.rows = rows
        self.head_a, self.head_b = head_a, head_b
        self.pairs, self.keys = pairing.pairs, pairing.keys
        self.omitted = dict.fromkeys(_LISTS, 0)

    def write(self, recs_a: list[Record], recs_b: list[Record], incomplete: bool) -> None:
        status, text = self._classify(recs_a, recs_b, incomplete)
        self.result["comparison_status"] = status
        self.result["explanation"] = text

    def _classify(self, recs_a: list[Record], recs_b: list[Record], incomplete: bool) -> tuple[str, str]:
        shown_a = [a for a, _ in self.pairs][:MAX_LISTED_COLUMNS]
        shown_b = [b for _, b in self.pairs][:MAX_LISTED_COLUMNS]
        if incomplete:
            return "inconclusive", (
                "INCONCLUSIVE: at least one file was not scanned completely or could not be read reliably, so "
                "presence and record alignment cannot be established. Any record counts above are partial. "
                "No comparison was made.")
        if not recs_a and not recs_b:
            return "absent_from_both", "Complete scans: the security is in neither file."
        if not recs_a or not recs_b:
            there, missing = ("A", "B") if recs_a else ("B", "A")
            if recs_a:
                self._list_records("records_in_file_a", recs_a, self.head_a, shown_a)
            else:
                self._list_records("records_in_file_b", recs_b, self.head_b, shown_b)
            return "present_in_one_file_only", (
                f"Complete scans: the security is in file {there} ({len(recs_a or recs_b)} record(s)) and absent "
                f"from file {missing}.")
        if not self.keys and (len(recs_a) > 1 or len(recs_b) > 1):
            differ = {"file_a": _varying(recs_a, self.head_a), "file_b": _varying(recs_b, self.head_b)}
            if self.budget.take(size(differ) + 50):
                self.result["columns_that_differ_between_matches"] = differ
            self._list_records("records_in_file_a", recs_a, self.head_a, shown_a)
            self._list_records("records_in_file_b", recs_b, self.head_b, shown_b)
            return "ambiguous_alignment", (
                f"AMBIGUOUS: file A has {len(recs_a)} matching record(s) and file B has {len(recs_b)}, and no match_keys "
                "were given, so records cannot be paired reliably. No comparison was made. Supply match_keys that "
                "identify one record per file (see columns_that_differ_between_matches), or compare specific records "
                "using find_security_in_file.")
        return self._align(recs_a, recs_b)

    def _align(self, recs_a: list[Record], recs_b: list[Record]) -> tuple[str, str]:
        """Pair records by their key values and compare each pair that is unambiguous."""
        key_a, key_b = [a for a, _ in self.keys], [b for _, b in self.keys]
        groups_a, groups_b = _group(recs_a, key_a), _group(recs_b, key_b)
        compared = differing = ambiguous = only_in_a = only_in_b = 0
        for key_values, group in groups_a.items():
            other = groups_b.get(key_values)
            described = _describe_key(self.head_a.labels, key_a, key_values)
            if other is None:
                for record in group:
                    only_in_a += 1
                    self._add("unmatched_records_in_file_a", {"record_number": record.number, "key": described})
            elif len(group) > 1 or len(other) > 1:
                ambiguous += 1
                self._add("ambiguous_keys", {"key": described, "records_in_file_a": [r.number for r in group],
                                             "records_in_file_b": [r.number for r in other]})
            else:
                compared += 1
                differing += any(_cell(group[0], a) != _cell(other[0], b) for a, b in self.pairs)
                self._compare_pair(group[0], other[0], described if self.keys else None)
        for key_values, group in groups_b.items():
            if key_values not in groups_a:
                described = _describe_key(self.head_b.labels, key_b, key_values)
                for record in group:
                    only_in_b += 1
                    self._add("unmatched_records_in_file_b", {"record_number": record.number, "key": described})
        left = {name: n for name, n in self.omitted.items() if n}
        if left:
            self.result["omitted_for_size"] = left

        if ambiguous and not compared:
            status = "ambiguous_alignment"
            text = ("AMBIGUOUS: the match_keys do not identify a single record in each file (see ambiguous_keys), "
                    "so no records could be paired. Add further match_keys.")
        elif ambiguous or only_in_a or only_in_b:
            status = "partially_compared"
            text = (f"{compared} record pair(s) compared; {differing} have at least one differing value. Some "
                    f"records could not be paired: {ambiguous} ambiguous key(s), {only_in_a} record(s) only in "
                    f"file A, {only_in_b} only in file B.")
        else:
            status = "compared"
            text = (f"Complete scans: {compared} record pair(s) compared; {differing} pair(s) have at "
                    "least one differing value.")
        if left:
            text += " Some detail was left out to fit the output-size limit (see omitted_for_size)."
        return status, text

    def _compare_pair(self, rec_a: Record, rec_b: Record, key: dict[str, Any] | None) -> None:
        label_a, label_b = self.head_a.labels, self.head_b.labels
        changed: list[dict[str, Any]] = []
        unchanged: list[dict[str, Any]] = []
        for a, b in self.pairs:
            va, vb = _cell(rec_a, a), _cell(rec_b, b)
            entry: dict[str, Any] = {"column_a": label_a[a]}
            if label_b[b] != label_a[a]:
                entry["column_b"] = label_b[b]
            if va == vb:
                entry["value"] = self._show(va)
                unchanged.append(entry)
                continue
            entry["value_a"], entry["value_b"] = self._show(va), self._show(vb)
            note = _difference_note(va, vb)
            if note:
                entry["note"] = note
            changed.append(entry)
        out: dict[str, Any] = {} if key is None else {"key": key}
        out.update({
            "record_a": {"record_number": rec_a.number, "byte_offset": rec_a.offset},
            "record_b": {"record_number": rec_b.number, "byte_offset": rec_b.offset},
            "changed_count": len(changed), "unchanged_count": len(unchanged),
            "changed": [], "unchanged": [],
        })
        if not self.budget.take(size(out) + PAIR_NOTES_BYTES):
            self.result.setdefault("comparisons", [])
            self.omitted["comparisons"] += 1
            return
        left_out: list[str] = []
        for source, target in ((changed, "changed"), (unchanged, "unchanged")):
            for entry in source:
                if self.budget.add(entry):
                    out[target].append(entry)
                else:
                    left_out.append(entry["column_a"])
        if left_out:
            out["truncated"] = True
            out["columns_omitted_for_size_count"] = len(left_out)
            out["columns_omitted_for_size"] = some(left_out, 250)
        else:
            self.budget.give_back(PAIR_NOTES_BYTES)
        self.result.setdefault("comparisons", []).append(out)

    def _add(self, name: str, item: dict[str, Any]) -> None:
        """Append `item` to the list `name` if it fits; otherwise count it as omitted."""
        entries = self.result.setdefault(name, [])
        if self.budget.add(item):
            entries.append(item)
        else:
            self.omitted[name] += 1

    def _list_records(self, name: str, recs: list[Record], header: Header, indices: list[int]) -> None:
        rows = []
        for record in recs:
            row = self.rows.fit(self.rows.row(header, record, indices), max(min(self.budget.room, 2000), 300))
            if not self.budget.add(row):
                break
            rows.append(row)
        self.result[name] = rows
        if len(rows) < len(recs):
            self.result[name + "_omitted_for_size"] = len(recs) - len(rows)

    def _show(self, value: str | None) -> str | None:
        return None if value is None else self.rows.cell(value)[0]


def _cell(record: Record, index: int) -> str | None:
    return record.fields[index] if index < len(record.fields) else None


def _group(records: list[Record], key_columns: list[int]) -> dict[Key, list[Record]]:
    groups: dict[Key, list[Record]] = {}
    for record in records:
        groups.setdefault(tuple(_cell(record, i) for i in key_columns), []).append(record)
    return groups


def _varying(recs: list[Record], header: Header) -> list[str]:
    """Columns whose value is different in every one of `recs`: candidates for match_keys."""
    names = [label for i, label in enumerate(header.labels) if len({_cell(r, i) for r in recs}) == len(recs)]
    return some(names[:20], 500)


def _describe_key(labels: list[str], indices: list[int], values: Key) -> dict[str, Any]:
    return {clip(labels[i], 60): (None if v is None else clip(v, 80)) for i, v in zip(indices, values, strict=True)}


def _difference_note(a: str | None, b: str | None) -> str | None:
    """Describe a difference that is only one of representation. The values still count as changed."""
    if a is None or b is None:
        return "the record has no field at this column position in one file (short record)"
    if a.strip() == b.strip():
        return "equal after trimming whitespace"
    if a.casefold() == b.casefold():
        return "equal ignoring letter case"
    try:
        if Decimal(a.strip()) == Decimal(b.strip()):
            return "numerically equal; the text formatting differs"
    except (InvalidOperation, ValueError):
        pass
    return None
