"""What a search found, and how that is put to the caller.

Scanning lives in `service`; this module holds the plain data a scan produces and the
wording of the answer: outcome, completeness, and the explanation of both.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .csvscan import Record
from .errors import ToolFailure
from .output import clip, size

MATCH_TYPE = "exact: whole field value, case-sensitive, compared as text"

STOP_EXPLANATIONS = {
    "end_of_file": "the scan reached the end of the file",
    "row_limit": "the row limit for one response was reached",
    "output_size_limit": "the output-size limit for one response was reached",
    "time_limit": "the scan time limit was reached before the end of the file",
    "cancelled": "the scan was cancelled before the end of the file",
    "too_many_matches": "more matching records were found than one comparison can hold",
}

class Hints:
    """Things seen during a scan that are NOT exact matches but explain a miss.

    Only records containing the search text somewhere are ever examined, so every such
    record in the scanned part of the file is covered.
    """

    def __init__(self, identifier: str):
        self.identifier = identifier
        self.trimmed = identifier.strip()
        self.padded = 0
        self.longer = 0
        self.elsewhere: dict[str, int] = {}
        self.spanning = 0
        self.spanning_examples: list[dict[str, Any]] = []
        self.padded_examples: list[dict[str, Any]] = []
        self.longer_examples: list[dict[str, Any]] = []

    def value(self, record: Record, label: str, value: str) -> None:
        """A value in a searched column that is not equal to the identifier."""
        if value.strip() == self.trimmed:
            self.padded += 1
            examples = self.padded_examples
        elif self.identifier in value:
            self.longer += 1
            examples = self.longer_examples
        else:
            return
        if len(examples) < 2:
            examples.append({"record_number": record.number, "column": clip(label, 60), "value": clip(value, 80)})

    def multi_line(self, record: Record, label: str, value: str) -> None:
        """The identifier inside a value that spans lines, in a column that was not searched."""
        self.spanning += 1
        if len(self.spanning_examples) < 2:
            at = value.find(self.identifier)
            self.spanning_examples.append({"record_number": record.number, "column": clip(label, 60),
                                           "value_excerpt": value[max(at - 30, 0): at + len(self.identifier) + 30]})

    def other_column(self, label: str) -> None:
        """The exact identifier, in a column other than the one searched."""
        key = clip(label, 60)
        if key in self.elsewhere or len(self.elsewhere) < 5:
            self.elsewhere[key] = self.elsewhere.get(key, 0) + 1

    def __bool__(self) -> bool:
        return bool(self.padded or self.longer or self.elsewhere or self.spanning)

    def sentence(self) -> str:
        parts = []
        if self.elsewhere:
            parts.append("the exact value appears in other column(s): " + ", ".join(self.elsewhere))
        if self.padded:
            parts.append(f"{self.padded} value(s) equal it apart from surrounding whitespace")
        if self.longer:
            parts.append(f"{self.longer} value(s) contain it inside a longer value")
        if self.spanning:
            parts.append(f"{self.spanning} multi-line value(s) in other columns contain it, which can mean rows "
                         "swallowed by an unclosed quote")
        return "; ".join(parts)

    def as_dict(self, examples: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {"note": "Seen in the part of the file scanned. These are NOT exact matches "
                                       "and are not counted or returned as matches."}
        if self.elsewhere:
            out["exact_value_in_other_columns"] = self.elsewhere
        if self.padded:
            out["same_value_with_surrounding_whitespace"] = {"count": self.padded}
            if examples:
                out["same_value_with_surrounding_whitespace"]["examples"] = self.padded_examples
        if self.longer:
            out["identifier_inside_a_longer_value"] = {"count": self.longer}
            if examples:
                out["identifier_inside_a_longer_value"]["examples"] = self.longer_examples
        if self.spanning:
            out["inside_a_multi_line_value_in_another_column"] = {
                "count": self.spanning,
                "meaning": "Whole rows may be hidden inside a quoted value whose closing quote is missing."}
            if examples:
                out["inside_a_multi_line_value_in_another_column"]["examples"] = self.spanning_examples
        return out


@dataclass
class SearchRequest:
    """The parts of a resolved search request that the answer refers to."""

    identifier: str
    column: str | None  # label of the column searched; None when every column is searched
    count_only: bool
    resumed: bool  # continued from a cursor
    start_offset: int
    matches_before: int  # matches reported by earlier responses in the cursor chain
    file_size: int


@dataclass
class Findings:
    """What one scan produced."""

    rows: list[dict[str, Any]]
    found: int  # matching records seen by this scan
    stop_reason: str | None  # None when the scan failed with `error`
    resume: tuple[int, int] | None  # where a later scan may start: (byte offset, records before it)
    error: ToolFailure | None
    warnings: list[str]
    sound: bool  # nothing about the file casts doubt on a complete scan of it
    changed: bool  # the file was modified during the scan
    hints: Hints
    stopped_at_byte: int
    records_seen: int  # records in the file, header included; only meaningful at end of file
    elapsed_seconds: float

    @property
    def reached_end(self) -> bool:
        return self.stop_reason == "end_of_file"

    @property
    def can_resume(self) -> bool:
        return self.error is None and not self.reached_end and self.resume is not None and not self.changed


def report_search(result: dict[str, Any], request: SearchRequest, found: Findings, next_cursor: str | None) -> None:
    """Add the scan status, outcome and explanation of a search to `result`."""
    total = request.matches_before + found.found
    trustworthy = found.reached_end and found.sound
    result["scan"] = {
        "complete": found.reached_end,
        "stop_reason": "error" if found.error else found.stop_reason,
        "started_at_byte": request.start_offset,
        "stopped_at_byte": found.stopped_at_byte,
        "file_size_bytes": request.file_size,
        "elapsed_seconds": found.elapsed_seconds,
    }
    records_after_header = None
    if found.reached_end:
        records_after_header = max(found.records_seen - 1, 0)
        result["scan"]["records_after_header"] = records_after_header
    if request.resumed and found.reached_end:
        result["scan"]["note"] = "Together with the earlier responses in this cursor chain, the whole file has been scanned."

    if total:
        outcome = "match_found"
    elif trustworthy:
        outcome = "confirmed_absent"
    else:
        outcome = "inconclusive"
    result["outcome"] = outcome

    wording = _Wording(
        shown_id=clip(request.identifier, 60),
        where=f"column '{clip(request.column, 80)}'" if request.column is not None else "any column",
        why=_why_incomplete(found),
        aside=_aside(found, total, records_after_header),
        resumable=next_cursor is not None,
    )
    if request.count_only:
        _report_count(result, found, total, trustworthy, wording)
    else:
        _report_rows(result, request, found, total, outcome, wording)
    result["next_cursor"] = next_cursor
    if found.error:
        result["error"] = {"type": found.error.kind, "message": found.error.message, **found.error.details}
    if found.warnings:
        result["warnings"] = found.warnings


def add_extras(result: dict[str, Any], hints: Hints, duplicates: dict[str, Any] | None, limit: int) -> None:
    """Optional detail, added only while the response still fits in `limit` bytes."""
    extras: list[tuple[str, Any]] = []
    if hints:
        extras.append(("near_misses", hints.as_dict()))
        extras.append(("near_misses", hints.as_dict(examples=False)))  # the fallback if the examples do not fit
    if duplicates:
        extras.append(("duplicate_column_names", duplicates))
    for key, value in extras:
        if key not in result and size(result) + size(value) + len(key) + 6 <= limit:
            result[key] = value


@dataclass
class _Wording:
    shown_id: str
    where: str  # "column 'X'" or "any column"
    why: str  # why the scan is not a complete, reliable one
    aside: str  # near misses and empty-file remarks, for answers without a match
    resumable: bool


def _why_incomplete(found: Findings) -> str:
    if found.reached_end:
        return "the file could not be read reliably (see warnings)"
    if found.error:
        return found.error.message
    return STOP_EXPLANATIONS.get(found.stop_reason or "", "the scan stopped early")


def _aside(found: Findings, total: int, records_after_header: int | None) -> str:
    aside = ""
    if not total and found.hints:
        aside = f" Not exact matches, but worth checking: {found.hints.sentence()} (see near_misses)."
    elif not total and found.reached_end:
        aside = " No near misses were seen either."
    if records_after_header == 0:
        aside += " Note: the file has no data records, only a header."
    return aside


def _report_count(result: dict[str, Any], found: Findings, total: int, trustworthy: bool, wording: _Wording) -> None:
    exact = trustworthy and found.error is None
    result["count"] = total if exact else None
    result["count_is_exact"] = exact
    if exact:
        result["explanation"] = (f"Complete scan: exactly {total} record(s) have '{wording.shown_id}' in "
                                 f"{wording.where}." + wording.aside)
        return
    result["matches_seen_before_interruption"] = total
    result["explanation"] = (
        f"A complete count is NOT available because {wording.why}. {total} matching record(s) were seen in the "
        "part scanned; this is a lower bound, not the count."
        + (" Pass next_cursor to continue counting from where this scan stopped." if wording.resumable else "")
    )


def _report_rows(result: dict[str, Any], request: SearchRequest, found: Findings, total: int, outcome: str,
                 wording: _Wording) -> None:
    rows = found.rows
    if found.reached_end and found.error is None:
        more = "none" if found.sound else "unknown"
    else:
        more = "yes" if found.stop_reason in ("row_limit", "output_size_limit") else "unknown"
    result["matches_returned"] = len(rows)
    result["matches"] = rows
    result["more_matches"] = more
    result["results_are_partial"] = more != "none"
    if outcome == "match_found":
        if more == "none":
            text = (f"Complete scan: all matching records have been returned ({total} in total"
                    + (", including earlier responses" if request.matches_before else "") + ").")
        elif more == "yes":
            text = (f"PARTIAL RESULTS: {len(rows)} matching record(s) returned and at least one more exists, "
                    f"because {wording.why}. Pass next_cursor to retrieve further matches.")
        else:
            text = (f"PARTIAL RESULTS: the scan is incomplete or unreliable because {wording.why}. The {len(rows)} "
                    "record(s) returned are only the matches that could be read; there may be more."
                    + (" Pass next_cursor to continue." if wording.resumable else ""))
    elif outcome == "confirmed_absent":
        text = f"Complete scan: no record has '{wording.shown_id}' as the exact value of {wording.where}." + wording.aside
    else:
        text = (f"INCONCLUSIVE: no match was found, but absence is not confirmed because {wording.why}."
                + (" Pass next_cursor to continue the scan." if wording.resumable else "") + wording.aside)
    result["explanation"] = text
