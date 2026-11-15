"""The published shape of every tool response.

Each tool advertises one of these models as its MCP output schema, and every response -
success, partial, inconclusive or error - is validated against it before it is sent.
The same validated payload is sent twice: as structured content and as JSON text.

A response either describes an operation that ran (possibly with partial or inconclusive
evidence; MCP isError is false) or carries `error` because it could not run (isError is
true). Fields marked "present unless error" are always there in the first case.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Outcome = Literal["match_found", "confirmed_absent", "no_match_within_filters", "inconclusive"]
StopReason = Literal["end_of_file", "row_limit", "output_size_limit", "time_limit", "cancelled", "error",
                     "no_progress", "too_many_matches", "not_scanned"]
FolderKind = Literal["mapped_folder", "discovered_subfolder"]


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorInfo(BaseModel):
    """Why a request could not be answered. Further keys give details specific to the error type."""

    model_config = ConfigDict(extra="allow")
    type: str = Field(description="Machine-readable error type, e.g. column_not_found, invalid_cursor, response_too_large")
    message: str


class ToolResult(_Out):
    required_unless_error: ClassVar[tuple[str, ...]] = ()

    tool: str
    error: ErrorInfo | None = Field(None, description="Present when the request could not be answered, or a scan failed part-way")
    request: dict[str, Any] | None = Field(None, description="Echo of the request, only on a failed call")

    @model_validator(mode="after")
    def _complete_unless_error(self) -> "ToolResult":
        if self.error is None:
            missing = [name for name in self.required_unless_error if name not in self.model_fields_set]
            if missing:
                raise ValueError(f"a response without an error must contain {missing}")
        return self


class PositionSelector(_Out):
    position: int


class FolderAccess(_Out):
    kind: FolderKind
    approved_root: str | None = Field(None, description="The mapped folder that makes this subfolder readable")
    metadata: str | None = None


class FileInfo(_Out):
    folder: str
    filename: str
    size_bytes: int
    filesystem_modified_time: str
    folder_access: FolderAccess | None = Field(None, description="Present for a file in a discovered subfolder")


class FieldCountMismatch(_Out):
    header_columns: int
    record_fields: int


class Row(_Out):
    record_number: int = Field(description="1-based; the header is record 1")
    byte_offset: int
    matched_columns: list[str] | None = None
    matched_column_count: int | None = None
    values: dict[str, str] = Field(description="Column label -> value, as text exactly as in the file")
    truncated_cells: list[str] | None = None
    truncated_cell_count: int | None = None
    requested_columns_missing_from_record: list[str] | None = None
    field_count_mismatch: FieldCountMismatch | None = None
    values_omitted_for_size: int | None = None
    size_note: str | None = None


class DuplicateColumns(_Out):
    repeated_names: int
    examples: dict[str, dict[str, Any]]
    note: str


# ---------------------------------------------------------------- get_process_map

class ProcessSummary(_Out):
    id: str
    name: str
    description: str


class LoadError(_Out):
    file: str
    error: str


class ProcessMapResult(ToolResult):
    processes: list[ProcessSummary] | None = Field(None, description="The available processes (call without arguments)")
    next_step: str | None = None
    process: dict[str, Any] | None = Field(None, description="The process map as maintained: purpose, folders, steps")
    process_id: str | None = None
    process_name: str | None = None
    step: dict[str, Any] | None = Field(None, description="One step in full (call with step_id)")
    folders: list[dict[str, Any]] | None = Field(None, description="The folders that step touches")
    truncated: bool | None = None
    truncation_note: str | None = None
    process_map_load_errors: list[LoadError] | None = None


# ---------------------------------------------------------------- list_folders

class StepUse(_Out):
    step_id: str
    step_name: str
    role: Literal["reads", "writes"]


class FolderUsage(_Out):
    process_id: str
    folder_id: str
    steps: list[StepUse]
    description: str | None = None


class IdentifierColumnInfo(_Out):
    column: str
    identifier_type: str | None = None


class FolderEntry(_Out):
    folder: str = Field(description="Pass this to list_csv_files, to the file tools, or back to list_folders as `folder`")
    kind: FolderKind
    name: str | None = None
    description: str | None = None
    exists_on_disk: bool | None = None
    used_by: list[FolderUsage] | None = None
    filename_pattern: str | None = None
    filename_patterns: list[str] | None = None
    identifier_columns: list[IdentifierColumnInfo] | None = None
    approved_root: str | None = None
    metadata: str | None = None
    note: str | None = None


class ParentFolder(_Out):
    folder: str
    kind: FolderKind
    approved_root: str | None = None
    metadata: str | None = None


class ListFoldersResult(ToolResult):
    required_unless_error = ("scope", "total_folders", "folders_returned", "folders", "truncated")

    scope: str | None = None
    parent: ParentFolder | None = Field(None, description="The folder whose subfolders are listed")
    total_folders: int | None = None
    folders_returned: int | None = None
    folders: list[FolderEntry] | None = None
    truncated: bool | None = None
    next_offset: int | None = None
    listing_complete: bool | None = Field(None, description="False if the folder could not be read to the end")
    linked_folders_not_followed: int | None = None
    unreadable_entries: int | None = None
    listing_warning: str | None = None
    note: str | None = None
    process_map_load_errors: list[LoadError] | None = None


# ---------------------------------------------------------------- list_csv_files

class FilenameInterpretation(_Out):
    tokens: dict[str, str]
    declared_by: list[str]


class FileEntry(_Out):
    filename: str
    size_bytes: int
    filesystem_modified_time: str
    filename_tokens: dict[str, str] | None = None
    matches_filename_convention: bool | None = None
    filename_status: Literal["parsed", "unparsed", "ambiguous"] | None = None
    filename_interpretations: list[FilenameInterpretation] | None = None


class ListingFilters(_Out):
    filename_contains: str | None
    date_from: str | None
    date_to: str | None
    date_field: str | None


class TimeFields(_Out):
    filesystem_modified_time: str
    filename_tokens: dict[str, str] | None = None


class FilenameConventionInfo(_Out):
    pattern: str
    notes: str | None = None
    declared_by: list[str] | None = None


class ListCsvFilesResult(ToolResult):
    required_unless_error = ("folder", "filters", "total_matching_files", "files_returned", "files", "truncated")

    folder: str | None = None
    folder_access: FolderAccess | None = None
    filters: ListingFilters | None = None
    total_matching_files: int | None = None
    files_returned: int | None = None
    files: list[FileEntry] | None = None
    truncated: bool | None = None
    next_offset: int | None = None
    sorted_by: str | None = None
    time_fields: TimeFields | None = None
    filename_convention: FilenameConventionInfo | None = None
    filename_conventions: list[FilenameConventionInfo] | None = None
    filename_conventions_note: str | None = None
    filename_conventions_inherited_from: str | None = None
    other_entries_ignored: int | None = None
    other_entries_note: str | None = None
    subfolders: int | None = None
    subfolders_note: str | None = None
    files_excluded_because_filename_has_no_parseable_date: int | None = None
    files_excluded_because_filename_date_is_ambiguous: int | None = None
    date_exclusion_note: str | None = None
    listing_complete: bool | None = None
    listing_warning: str | None = None


# ---------------------------------------------------------------- headers

class AwkwardColumn(_Out):
    position: int
    column: str
    selector: str | PositionSelector


class InspectHeadersResult(ToolResult):
    required_unless_error = ("file", "column_count", "columns", "columns_truncated")

    file: FileInfo | None = None
    column_count: int | None = None
    duplicate_column_names: DuplicateColumns | None = None
    warnings: list[str] | None = None
    columns: list[str] | None = Field(None, description="Column names in file order, starting at column_offset")
    columns_truncated: bool | None = None
    column_selection: str | None = None
    columns_needing_a_position_selector: list[AwkwardColumn] | None = None
    column_offset: int | None = None
    next_column_offset: int | None = None
    truncation_note: str | None = None
    sample_rows: list[Row] | None = None
    sample_rows_requested: int | None = None
    sample_stop_reason: Literal["requested_rows_collected", "end_of_file", "time_limit", "cancelled",
                                "output_size_limit", "error"] | None = None
    sample_complete: bool | None = Field(None, description="False when sampling stopped before the requested rows were collected")
    sample_note: str | None = None
    sample_truncated: bool | None = None
    sample_warning: str | None = None
    sample_error: str | None = None


class ColumnHit(_Out):
    column: str
    position: int
    selector: str | PositionSelector = Field(description="Pass this wherever a tool takes a column")


class SearchColumnNamesResult(ToolResult):
    required_unless_error = ("file", "term", "column_count", "matching_column_count", "matching_columns", "truncated")

    file: FileInfo | None = None
    term: str | None = None
    match_type: str | None = None
    column_count: int | None = None
    matching_column_count: int | None = None
    warnings: list[str] | None = None
    matching_columns: list[ColumnHit] | None = None
    truncated: bool | None = None
    selector_note: str | None = None
    similar_column_names: list[str] | None = None


# ---------------------------------------------------------------- searches and counts

class FilterEcho(_Out):
    column: str
    position: int
    value: str
    value_shortened_for_display: bool | None = None
    value_length: int | None = None


class ColumnsSummary(_Out):
    count: int
    first: list[str]


class SearchEcho(_Out):
    identifier: str
    match_type: str
    column: str | None = None
    columns_searched: str | None = None
    filters: list[FilterEcho] | None = Field(None, description="Every effective record filter; all must hold in the same record")
    scope: str | None = None
    columns_returned: str | list[str] | ColumnsSummary | None = None
    resumed_from_cursor: bool | None = None
    matches_in_earlier_responses: int | None = None
    max_rows: int | None = None


class ScanStatus(_Out):
    complete: bool = Field(description="The scan reached the end of the file. Not the same as reliable: see outcome and warnings")
    stop_reason: StopReason | None
    started_at_byte: int
    stopped_at_byte: int
    file_size_bytes: int
    elapsed_seconds: float
    records_after_header: int | None = None
    note: str | None = None
    integrity_problems_seen_in_earlier_responses: bool | None = None


class FailingFilters(_Out):
    count: int
    count_is_exact: bool
    meaning: str


class _ScanResult(ToolResult):
    file: FileInfo | None = None
    search: SearchEcho | None = None
    scan: ScanStatus | None = None
    outcome: Outcome | None = Field(
        None, description="match_found; confirmed_absent (complete, reliable scan, no filters); no_match_within_filters "
                          "(complete, reliable scan: nothing matches the identifier and the filters together - the "
                          "identifier may still be elsewhere in the file); inconclusive (never treat as absent)")
    identifier_matches_failing_filters: FailingFilters | None = None
    explanation: str | None = None
    next_cursor: str | None = None
    warnings: list[str] | None = None
    near_misses: dict[str, Any] | None = Field(None, description="Values that are not exact matches but may explain a miss")


class SearchResult(_ScanResult):
    required_unless_error = ("file", "search", "scan", "outcome", "matches_returned", "matches", "more_matches",
                             "results_are_partial", "explanation", "next_cursor")

    matches_returned: int | None = None
    matches: list[Row] | None = None
    more_matches: Literal["yes", "none", "unknown"] | None = None
    results_are_partial: bool | None = None
    duplicate_column_names: DuplicateColumns | None = None


class CountResult(_ScanResult):
    required_unless_error = ("file", "search", "scan", "outcome", "count", "count_is_exact", "explanation", "next_cursor")

    count: int | None = Field(None, description="Null unless the whole search scope was scanned reliably")
    count_is_exact: bool | None = None
    matches_seen_before_interruption: int | None = Field(None, description="A lower bound, given when count is null")


# ---------------------------------------------------------------- comparison

class ColumnPairEcho(_Out):
    a: str
    b: str


class CompareSearch(_Out):
    identifier_a: str
    identifier_column_a: str
    identifier_b: str
    identifier_column_b: str
    match_type: str
    match_keys: list[ColumnPairEcho]
    compared_column_count: int = Field(description="How many column pairs are compared for each record pair")
    compared_columns_other_than_identifier_and_keys: int
    filters_a: list[FilterEcho] | None = None
    filters_b: list[FilterEcho] | None = None
    scope: str | None = None
    compared_columns: str | list[ColumnPairEcho]
    coverage_warning: str | None = None


class ColumnsNotCompared(_Out):
    only_in_file_a_count: int
    only_in_file_a: list[str]
    only_in_file_b_count: int
    only_in_file_b: list[str]
    note: str


class Presence(_Out):
    outcome: Outcome
    matching_records_found: int
    scan_complete: bool
    stop_reason: StopReason | None
    identifier_matches_failing_filters: int | None = None
    scope: str | None = None
    explanation: str | None = None
    near_misses: str | None = None


class PresencePair(_Out):
    file_a: Presence
    file_b: Presence


class RecordRef(_Out):
    record_number: int
    byte_offset: int


class FieldComparison(_Out):
    column_a: str
    column_b: str | None = None
    value: str | None = None
    value_a: str | None = None
    value_b: str | None = None
    note: str | None = None
    missing_in: list[Literal["file_a", "file_b"]] | None = None


class Comparison(_Out):
    key: dict[str, str | None] | None = None
    record_a: RecordRef
    record_b: RecordRef
    columns_compared: int
    changed_count: int
    unchanged_count: int
    missing_count: int
    changed: list[FieldComparison]
    unchanged: list[FieldComparison]
    missing: list[FieldComparison] | None = None
    missing_note: str | None = None
    truncated: bool | None = None
    columns_omitted_for_size_count: int | None = None
    columns_omitted_for_size: list[str] | None = None


class AmbiguousKey(_Out):
    key: dict[str, str | None]
    records_in_file_a: list[int]
    records_in_file_b: list[int]


class UnmatchedRecord(_Out):
    record_number: int
    key: dict[str, str | None]


class UnalignableRecord(_Out):
    file: Literal["file_a", "file_b"]
    record_number: int
    missing_key_fields: list[str]
    reason: str


class ColumnsThatDiffer(_Out):
    file_a: list[str]
    file_b: list[str]


class CompareResult(ToolResult):
    required_unless_error = ("file_a", "file_b", "search", "presence", "comparison_status", "explanation")

    file_a: FileInfo | None = None
    file_b: FileInfo | None = None
    columns_not_compared: ColumnsNotCompared | None = None
    search: CompareSearch | None = None
    presence: PresencePair | None = None
    elapsed_seconds: float | None = None
    warnings: list[str] | None = None
    records_in_file_a: list[Row] | None = None
    records_in_file_a_omitted_for_size: int | None = None
    records_in_file_b: list[Row] | None = None
    records_in_file_b_omitted_for_size: int | None = None
    columns_that_differ_between_matches: ColumnsThatDiffer | None = None
    record_pairs_compared: int | None = None
    comparisons: list[Comparison] | None = None
    ambiguous_keys: list[AmbiguousKey] | None = None
    unmatched_records_in_file_a: list[UnmatchedRecord] | None = None
    unmatched_records_in_file_b: list[UnmatchedRecord] | None = None
    unalignable_records: list[UnalignableRecord] | None = None
    omitted_for_size: dict[str, int] | None = None
    comparison_status: Literal["compared", "partially_compared", "ambiguous_alignment", "no_comparable_columns",
                               "present_in_one_file_only", "absent_from_both", "inconclusive"] | None = None
    outcome: Literal["inconclusive"] | None = Field(None, description="Only on a failed call: no comparison was carried out")
    explanation: str | None = None


RESULT_MODELS: dict[str, type[ToolResult]] = {
    "get_process_map": ProcessMapResult,
    "list_folders": ListFoldersResult,
    "list_csv_files": ListCsvFilesResult,
    "inspect_csv_headers": InspectHeadersResult,
    "search_column_names": SearchColumnNamesResult,
    "find_security_in_file": SearchResult,
    "search_across_columns": SearchResult,
    "count_matching_rows": CountResult,
    "compare_security_records": CompareResult,
}
