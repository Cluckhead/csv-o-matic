"""Output schemas: the typed shape of every tool's response.

Each tool publishes one of these as its MCP `outputSchema`, and returns the response both as
`structuredContent` (the object described here) and as JSON text holding the very same object.

Conventions, the same for every tool:

- A field that is not listed as required may be absent. Absent means "does not apply to this
  response" - for example `next_offset` on a listing that was not truncated.
- A field whose type allows null is present and null when the value is unavailable - for example
  `count` after an interrupted scan, or `next_cursor` when there is nothing to continue.
- A request that could not be answered carries `error` and `request` (and, for the four search tools,
  `outcome: "inconclusive"`); the tool's other fields are then absent. A scan that failed part-way
  carries `error` alongside whatever was established before the failure.

The schemas are open: a consumer must ignore fields it does not know.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict
from typing_extensions import NotRequired, TypedDict

Outcome = Literal["match_found", "confirmed_absent", "inconclusive"]
StopReason = Literal["end_of_file", "row_limit", "output_size_limit", "time_limit", "cancelled",
                     "too_many_matches", "error", "not_scanned"]
FolderKind = Literal["mapped_root", "subfolder"]


# ------------------------------------------------------------------ shared pieces

class ErrorInfo(TypedDict):
    """Why a request could not be answered, or why a scan stopped. Further keys give type-specific detail."""

    __pydantic_config__ = ConfigDict(extra="allow")  # type: ignore[misc]

    type: str
    message: str


class LoadError(TypedDict):
    file: str
    error: str


class FileInfo(TypedDict):
    """Identity of the file a response is about."""

    folder: str
    mapped_root: str  # the process-map folder that authorises access; equal to `folder` unless it is a subfolder
    filename: str
    size_bytes: int
    filesystem_modified_time: str


class ColumnValue(TypedDict):
    """One record-key filter: the row must hold exactly `value` in `column`."""

    column: str
    value: str


class IdentifierColumn(TypedDict):
    column: str
    identifier_type: NotRequired[str]


class FieldCountMismatch(TypedDict):
    header_columns: int
    record_fields: int


class Row(TypedDict):
    """One CSV record. `values` maps column label -> text exactly as in the file."""

    record_number: int
    byte_offset: int
    values: dict[str, str]
    matched_columns: NotRequired[list[str]]
    matched_column_count: NotRequired[int]
    truncated_cells: NotRequired[list[str]]
    truncated_cell_count: NotRequired[int]
    field_count_mismatch: NotRequired[FieldCountMismatch]
    values_omitted_for_size: NotRequired[int]
    size_note: NotRequired[str]


class DuplicateName(TypedDict):
    occurrences: int
    first_positions: list[int]


class DuplicateColumnNames(TypedDict):
    repeated_names: int
    examples: dict[str, DuplicateName]
    note: str


class Scan(TypedDict):
    """How far a scan got. `complete` is true only when the end of the file was reached."""

    complete: bool
    stop_reason: StopReason | None
    started_at_byte: int
    stopped_at_byte: int
    file_size_bytes: int
    elapsed_seconds: float
    records_after_header: NotRequired[int]
    note: NotRequired[str]


class ValueExample(TypedDict):
    record_number: int
    column: str
    value: str


class ExcerptExample(TypedDict):
    record_number: int
    column: str
    value_excerpt: str


class FilterMissExample(TypedDict):
    record_number: int
    values: dict[str, str | None]


class CountedValues(TypedDict):
    count: int
    examples: NotRequired[list[ValueExample]]


class CountedExcerpts(TypedDict):
    count: int
    meaning: str
    examples: NotRequired[list[ExcerptExample]]


class CountedFilterMisses(TypedDict):
    count: int
    meaning: str
    examples: NotRequired[list[FilterMissExample]]


class NearMisses(TypedDict):
    """Things that are NOT exact matches but may explain a miss."""

    note: str
    identifier_matched_but_filters_did_not: NotRequired[CountedFilterMisses]
    exact_value_in_other_columns: NotRequired[dict[str, int]]
    same_value_with_surrounding_whitespace: NotRequired[CountedValues]
    identifier_inside_a_longer_value: NotRequired[CountedValues]
    inside_a_multi_line_value_in_another_column: NotRequired[CountedExcerpts]


class ColumnsSummary(TypedDict):
    count: int
    first: list[str]


class AppliedSearch(TypedDict):
    """The search exactly as it was applied."""

    identifier: str
    match_type: str
    filters: list[ColumnValue]  # empty: the identifier alone decided a match
    column: NotRequired[str]
    columns_searched: NotRequired[str]
    columns_returned: NotRequired[Literal["all"] | list[str] | ColumnsSummary]
    max_rows: NotRequired[int]
    resumed_from_cursor: NotRequired[bool]
    matches_in_earlier_responses: NotRequired[int]


class _Response(TypedDict):
    tool: str
    error: NotRequired[ErrorInfo]
    request: NotRequired[dict[str, Any]]  # echo of the arguments, present when the request failed


# ------------------------------------------------------------------ process information

class ProcessSummary(TypedDict):
    id: str
    name: str
    description: str


class GetProcessMapResult(_Response):
    """Without `process`: `processes`. With it: `process`. With `step_id` as well: `step` and `folders`."""

    processes: NotRequired[list[ProcessSummary]]
    next_step: NotRequired[str]
    process: NotRequired[dict[str, Any]]
    process_id: NotRequired[str]
    process_name: NotRequired[str]
    step: NotRequired[dict[str, Any]]
    folders: NotRequired[list[dict[str, Any]]]
    truncated: NotRequired[bool]
    truncation_note: NotRequired[str]
    process_map_load_errors: NotRequired[list[LoadError]]


class StepUse(TypedDict):
    step_id: str
    step_name: str
    role: Literal["reads", "writes"]


class FolderUse(TypedDict):
    process_id: str
    folder_id: str
    steps: list[StepUse]
    description: NotRequired[str]


class FolderEntry(TypedDict):
    folder: str  # pass this as `folder` to the other tools
    kind: FolderKind
    name: NotRequired[str]
    mapped_root: NotRequired[str]
    description: NotRequired[str]
    exists_on_disk: NotRequired[bool]
    used_by: NotRequired[list[FolderUse]]
    filename_pattern: NotRequired[str]
    identifier_columns: NotRequired[list[IdentifierColumn]]
    record_key_columns: NotRequired[list[str]]


class ListFoldersResult(_Response):
    """Mapped folders, or - when `folder` was given - the immediate subfolders of that folder."""

    scope: NotRequired[str]
    total_folders: NotRequired[int]
    folders_returned: NotRequired[int]
    folders: NotRequired[list[FolderEntry]]
    truncated: NotRequired[bool]
    next_offset: NotRequired[int]
    note: NotRequired[str]
    process_map_load_errors: NotRequired[list[LoadError]]
    # Present when listing the subfolders of one folder:
    folder: NotRequired[str]
    kind: NotRequired[FolderKind]
    mapped_root: NotRequired[str]
    parent_folder: NotRequired[str]
    mapped_root_description: NotRequired[str]
    filename_pattern: NotRequired[str]
    identifier_columns: NotRequired[list[IdentifierColumn]]
    record_key_columns: NotRequired[list[str]]
    sorted_by: NotRequired[str]
    symbolic_links_ignored: NotRequired[int]


class ListingFilters(TypedDict):
    filename_contains: str | None
    date_from: str | None
    date_to: str | None
    date_field: str | None


class FileEntry(TypedDict):
    filename: str
    size_bytes: int
    filesystem_modified_time: str
    filename_tokens: NotRequired[dict[str, str] | None]  # null: the name does not follow the convention
    matches_filename_convention: NotRequired[bool]


class FilenameConventionInfo(TypedDict):
    pattern: str
    notes: NotRequired[str]


class TimeFields(TypedDict):
    filesystem_modified_time: str
    filename_tokens: NotRequired[dict[str, str]]


class ListCsvFilesResult(_Response):
    folder: NotRequired[str]
    kind: NotRequired[FolderKind]
    mapped_root: NotRequired[str]
    filters: NotRequired[ListingFilters]
    total_matching_files: NotRequired[int]
    files_returned: NotRequired[int]
    files: NotRequired[list[FileEntry]]
    truncated: NotRequired[bool]
    next_offset: NotRequired[int]
    sorted_by: NotRequired[str]
    time_fields: NotRequired[TimeFields]
    filename_convention: NotRequired[FilenameConventionInfo]
    other_entries_ignored: NotRequired[int]
    other_entries_note: NotRequired[str]
    files_excluded_because_filename_has_no_parseable_date: NotRequired[int]
    subfolder_count: NotRequired[int]
    subfolders: NotRequired[list[str]]
    subfolders_note: NotRequired[str]
    record_key_columns: NotRequired[list[str]]


class InspectCsvHeadersResult(_Response):
    file: NotRequired[FileInfo]
    column_count: NotRequired[int]
    columns: NotRequired[list[str]]
    columns_truncated: NotRequired[bool]
    column_offset: NotRequired[int]
    next_column_offset: NotRequired[int]
    truncation_note: NotRequired[str]
    duplicate_column_names: NotRequired[DuplicateColumnNames]
    warnings: NotRequired[list[str]]
    sample_rows: NotRequired[list[Row]]
    sample_note: NotRequired[str]
    sample_truncated: NotRequired[bool]
    sample_error: NotRequired[str]
    sample_warning: NotRequired[str]


class ColumnHit(TypedDict):
    column: str
    position: int


class SearchColumnNamesResult(_Response):
    file: NotRequired[FileInfo]
    term: NotRequired[str]
    match_type: NotRequired[str]
    column_count: NotRequired[int]
    matching_column_count: NotRequired[int]
    matching_columns: NotRequired[list[ColumnHit]]
    truncated: NotRequired[bool]
    similar_column_names: NotRequired[list[str]]
    warnings: NotRequired[list[str]]


# ------------------------------------------------------------------ scans

class SearchResult(_Response):
    """find_security_in_file and search_across_columns.

    `outcome` says whether the record exists: match_found, confirmed_absent (complete, reliable scan and
    nothing satisfied the identifier and every filter) or inconclusive (never treat as absent).
    `results_are_partial` / `more_matches` say whether every match was returned.
    """

    outcome: Outcome
    explanation: str
    file: NotRequired[FileInfo]
    search: NotRequired[AppliedSearch]
    scan: NotRequired[Scan]
    matches_returned: NotRequired[int]
    matches: NotRequired[list[Row]]
    more_matches: NotRequired[Literal["none", "yes", "unknown"]]
    results_are_partial: NotRequired[bool]
    next_cursor: NotRequired[str | None]  # null: nothing to continue
    warnings: NotRequired[list[str]]
    near_misses: NotRequired[NearMisses]
    duplicate_column_names: NotRequired[DuplicateColumnNames]


class CountResult(_Response):
    """count_matching_rows. `count` is null unless the whole file was scanned reliably."""

    outcome: Outcome
    explanation: str
    file: NotRequired[FileInfo]
    search: NotRequired[AppliedSearch]
    scan: NotRequired[Scan]
    count: NotRequired[int | None]
    count_is_exact: NotRequired[bool]
    matches_seen_before_interruption: NotRequired[int]  # a lower bound, given only when count is null
    next_cursor: NotRequired[str | None]
    warnings: NotRequired[list[str]]
    near_misses: NotRequired[NearMisses]


class ColumnPair(TypedDict):
    a: str
    b: str


class ComparisonSearch(TypedDict):
    identifier_a: str
    identifier_column_a: str
    identifier_b: str
    identifier_column_b: str
    match_type: str
    match_keys: list[ColumnPair]
    filters_a: list[ColumnValue]
    filters_b: list[ColumnValue]
    compared_columns: str | list[ColumnPair]
    compared_column_count: NotRequired[int]


class Presence(TypedDict):
    outcome: Outcome
    matching_records_found: int
    scan_complete: bool
    stop_reason: StopReason | None
    explanation: NotRequired[str]
    near_misses: NotRequired[str]


class PresenceByFile(TypedDict):
    file_a: Presence
    file_b: Presence


class ColumnsNotCompared(TypedDict):
    only_in_file_a_count: int
    only_in_file_a: list[str]
    only_in_file_b_count: int
    only_in_file_b: list[str]
    note: str


class RecordRef(TypedDict):
    record_number: int
    byte_offset: int


class ChangedValue(TypedDict):
    column_a: str
    column_b: NotRequired[str]
    value_a: str | None  # null: the record has no field at this position
    value_b: str | None
    note: NotRequired[str]


class UnchangedValue(TypedDict):
    column_a: str
    column_b: NotRequired[str]
    value: str | None


class RecordComparison(TypedDict):
    record_a: RecordRef
    record_b: RecordRef
    changed_count: int
    unchanged_count: int
    changed: list[ChangedValue]
    unchanged: list[UnchangedValue]
    key: NotRequired[dict[str, str | None]]
    truncated: NotRequired[bool]
    columns_omitted_for_size_count: NotRequired[int]
    columns_omitted_for_size: NotRequired[list[str]]


class AmbiguousKey(TypedDict):
    key: dict[str, str | None]
    records_in_file_a: list[int]
    records_in_file_b: list[int]


class UnmatchedRecord(TypedDict):
    record_number: int
    key: dict[str, str | None]


class VaryingColumns(TypedDict):
    file_a: list[str]
    file_b: list[str]


class CompareResult(_Response):
    """compare_security_records.

    `comparison_status`: compared (every record paired), partially_compared (some records could not be
    paired), ambiguous_alignment (records could not be paired at all), present_in_one_file_only,
    absent_from_both, or inconclusive (a scan was incomplete or unreliable; nothing was compared).
    On a request that could not be run, `outcome` is "inconclusive" and `comparison_status` is absent.
    """

    explanation: str
    comparison_status: NotRequired[Literal["compared", "partially_compared", "ambiguous_alignment",
                                           "present_in_one_file_only", "absent_from_both", "inconclusive"]]
    outcome: NotRequired[Literal["inconclusive"]]
    file_a: NotRequired[FileInfo]
    file_b: NotRequired[FileInfo]
    search: NotRequired[ComparisonSearch]
    presence: NotRequired[PresenceByFile]
    elapsed_seconds: NotRequired[float]
    warnings: NotRequired[list[str]]
    columns_not_compared: NotRequired[ColumnsNotCompared]
    comparisons: NotRequired[list[RecordComparison]]
    ambiguous_keys: NotRequired[list[AmbiguousKey]]
    unmatched_records_in_file_a: NotRequired[list[UnmatchedRecord]]
    unmatched_records_in_file_b: NotRequired[list[UnmatchedRecord]]
    columns_that_differ_between_matches: NotRequired[VaryingColumns]
    records_in_file_a: NotRequired[list[Row]]
    records_in_file_a_omitted_for_size: NotRequired[int]
    records_in_file_b: NotRequired[list[Row]]
    records_in_file_b_omitted_for_size: NotRequired[int]
    omitted_for_size: NotRequired[dict[str, int]]


RESULT_TYPES: dict[str, Any] = {
    "get_process_map": GetProcessMapResult,
    "list_folders": ListFoldersResult,
    "list_csv_files": ListCsvFilesResult,
    "inspect_csv_headers": InspectCsvHeadersResult,
    "search_column_names": SearchColumnNamesResult,
    "find_security_in_file": SearchResult,
    "search_across_columns": SearchResult,
    "count_matching_rows": CountResult,
    "compare_security_records": CompareResult,
}
