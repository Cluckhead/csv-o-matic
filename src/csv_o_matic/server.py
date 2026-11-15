"""MCP wiring: exposes the nine read-only tools, with progress, cancellation and limits."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from functools import partial
from typing import Annotated, Any, Callable

import anyio
import anyio.to_thread
from mcp import types
from mcp.server.mcpserver import Context, MCPServer
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import models
from .config import Config, ConfigError, load_config
from .csvscan import ScanControl
from .access import AccessError
from .service import MAX_FILTERS, Service, ToolFailure

log = logging.getLogger("csv_o_matic")

SEARCH_TOOLS = {"find_security_in_file", "search_across_columns", "count_matching_rows", "compare_security_records"}

INSTRUCTIONS = """\
CSV-o-Matic gives read-only, on-demand evidence about individual securities in large ETL CSV files.
It never modifies files and keeps no index: every answer comes from reading the file you name.

Typical investigation:
1. get_process_map (no arguments) to see the processes, then again with a process id for its steps, folders,
   upstream/downstream links, filename conventions, identifier columns, field mappings and filter rules.
2. list_folders, then list_csv_files on the folders that matter. Read the filename tokens carefully: several
   files for one business date usually mean retries, reruns or backfills (the process map says how to tell).
   Only the mapped folders and their genuine subfolders can be read; list_folders with `folder` shows a
   folder's subfolders (dated or archive directories, for example). Nothing else on the machine is reachable.
3. inspect_csv_headers / search_column_names to learn a file's layout.
4. find_security_in_file (known column), search_across_columns (unknown column), count_matching_rows,
   compare_security_records. Narrow any of them to one portfolio, date or other record key with `filters`.

Reading results:
- outcome is one of match_found, confirmed_absent (only after a complete, reliable scan), no_match_within_filters
  (complete, reliable scan, but only of the records the filters select: the identifier may still be elsewhere
  in the file) or inconclusive. Never treat inconclusive as absent.
- A column is selected by its exact name, or unambiguously as {"position": N} or {"name": "..."}.
- Responses come as structured content and as the same JSON in text.
- A match does not mean every match was returned: check results_are_partial / more_matches and use
  next_cursor to fetch more.
- Matching is exact, whole-value text matching. '00123' and '123' are different identifiers, and part of a
  name does not match the full name. When nothing matches exactly, check near_misses: it reports values that
  contain the search text, differ only by whitespace, or sit in a different column.
- Any exact value can be searched for, not only security identifiers (a portfolio code, for example).
- record_number is 1-based and counts the header as record 1.
- filesystem_modified_time is when a file was written to disk; business dates and run timestamps come from
  the filename tokens.
You choose the files and interpret the evidence; the server only reports what is in the files you select.
"""

class ColumnRef(BaseModel):
    """Selects a column explicitly: give exactly one of name or position."""

    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(None, description="The exact header text. Never read as a position, so {\"name\": \"#1\"} is the column called '#1'.")
    position: int | None = Field(None, ge=1, description="1-based position of the column in the file")


ColumnSelector = Annotated[
    str | ColumnRef,
    Field(description="A column: its exact name; or {\"position\": N} (1-based); or {\"name\": \"...\"}. A plain "
                      "string may also be the '#N' positional shorthand or a label shown in returned rows; if a "
                      "plain string could mean two different columns the call is refused, and the object forms settle it."),
]


class RecordFilter(BaseModel):
    """One exact-match condition on a record."""

    model_config = ConfigDict(extra="forbid")
    column: ColumnSelector
    value: str = Field(description="Text the whole field must equal exactly (case-sensitive; leading zeroes and "
                                   "whitespace are significant; no number or date conversion). \"\" matches an empty "
                                   "field, but not a field the record is too short to have.")


class ColumnPair(BaseModel):
    model_config = ConfigDict(extra="forbid")
    a: ColumnSelector = Field(description="Column in file A")
    b: ColumnSelector = Field(description="The corresponding column in file B")


Folder = Annotated[str, Field(description="Folder path exactly as returned by list_folders (a mapped folder or one of its subfolders)")]
Filename = Annotated[str, Field(description="File name exactly as returned by list_csv_files (no path)")]
Identifier = Annotated[str, Field(description="Value to match exactly against whole field values, as text. Leading zeroes are significant; partial values do not match.")]
Columns = Annotated[
    list[ColumnSelector] | None,
    Field(description="Columns to return for each match. Omit for the complete row."),
]
Filters = Annotated[
    list[RecordFilter] | None,
    Field(max_length=MAX_FILTERS,
          description="Optional exact-match conditions that must ALL hold in the same record as the identifier, e.g. "
                      "[{\"column\": \"Portfolio\", \"value\": \"P1\"}, {\"column\": \"TradeDate\", \"value\": \"2026-11-13\"}]. "
                      "With filters, a result of no match means no record matches the identifier and the filters "
                      "together, not that the identifier is absent from the file."),
]
MaxRows = Annotated[int | None, Field(description="Maximum matching rows in this response (capped by the server limit)")]
Cursor = Annotated[str | None, Field(description="next_cursor from a previous response of this same search (same file, identifier, column and filters), to continue it")]
ScanSeconds = Annotated[
    float | None,
    Field(description="Optional time budget for this scan in seconds (capped by the server limit). "
                      "When it runs out the response is marked incomplete and can be continued."),
]


def _plain(value: Any) -> Any:
    """Argument models as plain data, which is all the service layer knows about."""
    if isinstance(value, BaseModel):
        return value.model_dump(exclude_none=True)
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def _wire_safe(value: Any) -> Any:
    """Replace characters that cannot be sent as UTF-8 (lone surrogates echoed from an argument)."""
    if isinstance(value, str):
        return value.encode("utf-8", "replace").decode("utf-8")
    if isinstance(value, list):
        return [_wire_safe(v) for v in value]
    if isinstance(value, dict):
        return {_wire_safe(k): _wire_safe(v) for k, v in value.items()}
    return value


def result_size(result: types.CallToolResult) -> int:
    """Bytes of the complete MCP result as serialised: both representations and the envelope."""
    return len(result.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8"))


def build_server(config: Config, service: Service | None = None) -> MCPServer:
    service = service or Service(config)
    limits = config.limits
    scan_slots = anyio.CapacityLimiter(limits.max_concurrent_scans)
    threads = anyio.CapacityLimiter(limits.max_concurrent_scans + 8)  # scan_slots is the real limit
    read_only = types.ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
    server = MCPServer("csv-o-matic", instructions=INSTRUCTIONS)

    def failed(tool: str, error: dict[str, Any], request: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"tool": tool}
        if tool in SEARCH_TOOLS:
            payload["outcome"] = "inconclusive"
            payload["explanation"] = f"INCONCLUSIVE: no search was carried out. {error['message']}"
        payload["error"] = error
        if request is not None:
            payload["request"] = request
        return payload

    def build(payload: dict[str, Any], is_error: bool) -> types.CallToolResult:
        """One payload, two representations: the structured content, and the same thing as JSON text."""
        text = json.dumps(payload, ensure_ascii=False)
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)],
                                    structured_content=payload, is_error=is_error)

    def respond(tool: str, payload: dict[str, Any], is_error: bool = False) -> types.CallToolResult:
        """Validate the final payload against the tool's published schema - error payloads included -
        and enforce the output limit on the whole serialised result."""
        model = models.RESULT_MODELS[tool]
        try:
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except UnicodeEncodeError:
            payload = _wire_safe(payload)
        try:
            model.model_validate(payload)
        except ValidationError as exc:
            log.error("%s produced a response that does not match its output schema: %s", tool, exc)
            is_error = True
            payload = failed(tool, {"type": "internal_error",
                                     "message": "The server built a response that does not match its published schema; "
                                                "it was withheld. This is a server defect, not a finding about the data."})
        result = build(payload, is_error)
        if result_size(result) > limits.max_output_bytes:
            # Responses are built to fit; this is the backstop that guarantees the bound. Whatever the
            # response held is withheld whole, so nothing in it can pass for a conclusion.
            result = build(failed(tool, {
                "type": "response_too_large",
                "message": f"The response exceeded the output-size limit of {limits.max_output_bytes} bytes and was "
                           "withheld. Request fewer columns or rows."}), True)
        return result

    def guarded(tool: str, request: dict[str, Any], fn: Callable[[], dict[str, Any]]) -> tuple[dict[str, Any], bool]:
        """Run a tool body, turning failures into explicit, structured explanations."""
        request = {k: _bounded(_plain(v)) for k, v in request.items() if v is not None}
        try:
            return {"tool": tool, **fn()}, False
        except AccessError as denied:
            error = {"type": denied.kind, "message": denied.message}
        except ToolFailure as failure:
            error = {"type": failure.kind, "message": failure.message, **failure.details}
        except Exception as exc:  # noqa: BLE001 - report, never crash the session
            log.exception("%s failed", tool)
            error = {"type": "internal_error", "message": f"{type(exc).__name__}: {exc}"}
        return failed(tool, error, request), True

    async def quick(tool: str, request: dict[str, Any], fn: Callable[[], dict[str, Any]]) -> types.CallToolResult:
        payload, failed = await anyio.to_thread.run_sync(guarded, tool, request, fn)
        return respond(tool, payload, failed)

    async def scan(ctx: Context, tool: str, request: dict[str, Any], max_scan_seconds: float | None,
                   fn: Callable[[ScanControl], dict[str, Any]]) -> types.CallToolResult:
        """Run a file scan in a worker thread with a slot limit, time limit, progress and cancellation."""
        budget = limits.max_scan_seconds
        if max_scan_seconds is not None:
            if max_scan_seconds <= 0:
                payload, _ = guarded(tool, request, _raise(ToolFailure("invalid_argument", "max_scan_seconds must be positive.")))
                return respond(tool, payload, True)
            budget = min(budget, max_scan_seconds)
        control = ScanControl(max_seconds=budget)

        async def report_progress() -> None:
            while True:
                await anyio.sleep(limits.progress_interval_seconds)
                if control.phase == "queued":
                    message = f"queued: waiting for a free scan slot (at most {limits.max_concurrent_scans} scans run at once)"
                else:
                    message = (f"{control.phase}: {(control.bytes_base + control.bytes_done) / 1e6:,.0f} of "
                               f"{control.total_bytes / 1e6:,.0f} MB, {control.matches} match(es) so far, "
                               f"{control.elapsed:.0f}s of {budget:.0f}s allowed")
                try:
                    await ctx.report_progress(control.bytes_base + control.bytes_done, control.total_bytes or None, message)
                except Exception:  # noqa: BLE001 - progress is best-effort
                    log.debug("progress notification failed", exc_info=True)

        outcome: list[tuple[dict[str, Any], bool]] = []
        done = anyio.Event()

        async def run_in_thread() -> None:
            async with scan_slots:  # waiting here is cancellable and does not use up the time budget
                # Shielded: the slot is held until the worker thread has really finished, so the cap
                # on concurrent scans holds even while a cancelled scan is winding down.
                with anyio.CancelScope(shield=True):
                    outcome.append(await anyio.to_thread.run_sync(guarded, tool, request, partial(fn, control), limiter=threads))
            done.set()

        async with anyio.create_task_group() as tg:
            tg.start_soon(report_progress)  # also reports while the scan is queued for a slot
            tg.start_soon(run_in_thread)
            try:
                await done.wait()
            except BaseException:
                # The client cancelled or disconnected. Tell the worker to stop; it checks this flag
                # on every read. MCP sends no response to a cancelled request.
                control.stop.set()
                log.info("%s cancelled by client after %.1fs; stopping the scan", tool, control.elapsed)
                raise
            tg.cancel_scope.cancel()
        payload, failed = outcome[0]
        return respond(tool, payload, failed)

    # ---------------------------------------------------------------- process information
    # Every tool returns one validated payload, published under an explicit output schema.

    @server.tool(annotations=read_only)
    async def get_process_map(
        process: Annotated[str | None, Field(description="Process id or name. Omit to list the available processes.")] = None,
        step_id: Annotated[str | None, Field(description="Optional: return only this step, in full, with the folders it touches")] = None,
    ) -> Annotated[types.CallToolResult, models.ProcessMapResult]:
        """Describe an ETL process: purpose, steps, folders, upstream/downstream links (including branches),
        filename conventions and timestamp meanings, identifier columns, field mappings, and known
        transformation and filter rules. Called without arguments it lists the available processes
        (id, name, description) so you can pick one. Start every investigation here."""
        return await quick("get_process_map", {"process": process, "step_id": step_id},
                           lambda: service.get_process_map(process, step_id))

    @server.tool(annotations=read_only)
    async def list_folders(
        process: Annotated[str | None, Field(description="Process id or name. Omit for the folders of all processes.")] = None,
        folder: Annotated[str | None, Field(description="A folder from an earlier list_folders response. Lists its immediate subfolders instead of the mapped folders.")] = None,
        offset: Annotated[int, Field(description="Continue a truncated listing from this next_offset")] = 0,
    ) -> Annotated[types.CallToolResult, models.ListFoldersResult]:
        """List the folders that can be read. Without `folder`: the folders described by the process maps - all
        of them, or those of one process - each with its description and the process steps that read or write
        it. With `folder`: the immediate subfolders of that folder (dated or archive directories, say), one
        level at a time; call again on a subfolder to go deeper. Only mapped folders and their genuine
        subfolders are reachable; links are never followed. Then call list_csv_files on a folder."""
        return await quick("list_folders", {"process": process, "folder": folder, "offset": offset},
                           lambda: service.list_folders(process, offset, folder))

    @server.tool(annotations=read_only)
    async def list_csv_files(
        folder: Folder,
        filename_contains: Annotated[str | None, Field(description="Keep only filenames containing this text (case-insensitive)")] = None,
        date_from: Annotated[str | None, Field(description="Earliest date to include, YYYY-MM-DD")] = None,
        date_to: Annotated[str | None, Field(description="Latest date to include, YYYY-MM-DD")] = None,
        date_field: Annotated[
            str | None,
            Field(description="Which date the date filters apply to: a filename token such as 'business_date' or "
                              "'run_timestamp' (default: the folder's main filename date), or 'filesystem_modified'"),
        ] = None,
        offset: Annotated[int, Field(description="Continue a truncated listing from this next_offset")] = 0,
        newest_first: Annotated[bool, Field(description="Sort filenames descending (default) or ascending")] = True,
    ) -> Annotated[types.CallToolResult, models.ListCsvFilesResult]:
        """List the CSV files that actually exist directly in one folder right now (subfolders are not searched),
        with size and filesystem modification time, plus the dates/timestamps parsed from each filename
        according to the folder's naming convention(s). Filesystem modification time and filename dates are
        reported separately and mean different things. Optional filename and date filters narrow the listing;
        files left out because their date could not be established are counted separately from files outside
        the date range."""
        request = {"folder": folder, "filename_contains": filename_contains, "date_from": date_from,
                   "date_to": date_to, "date_field": date_field, "offset": offset}
        return await quick("list_csv_files", request, lambda: service.list_csv_files(
            folder, filename_contains, date_from, date_to, date_field, offset, newest_first))

    # Reading a header or a sample can mean reading a lot of file (a huge first line, a long run of blank
    # lines), so these two run under the same supervision as the searches: slot, time limit, progress, cancel.

    @server.tool(annotations=read_only)
    async def inspect_csv_headers(
        folder: Folder,
        filename: Filename,
        ctx: Context,
        sample_rows: Annotated[int, Field(description="Number of leading data rows to include, up to the server's sample limit. Default 0: headers only.")] = 0,
        sample_columns: Annotated[list[ColumnSelector] | None, Field(description="Limit sample rows to these columns")] = None,
        column_offset: Annotated[int, Field(description="Continue a truncated column list from this next_column_offset")] = 0,
        max_scan_seconds: ScanSeconds = None,
    ) -> Annotated[types.CallToolResult, models.InspectHeadersResult]:
        """Return a CSV file's column names in their original order. Optionally include a few leading data rows
        to understand the layout; sample_complete says whether all requested rows were collected. The sample
        is only the start of the file - never use it to judge whether a security is present."""
        request = {"folder": folder, "filename": filename, "sample_rows": sample_rows,
                   "sample_columns": sample_columns, "column_offset": column_offset}
        return await scan(ctx, "inspect_csv_headers", request, max_scan_seconds, lambda control:
                          service.inspect_csv_headers(folder, filename, sample_rows, column_offset,
                                                      _plain(sample_columns), control=control))

    @server.tool(annotations=read_only)
    async def search_column_names(
        folder: Folder,
        filename: Filename,
        term: Annotated[str, Field(description="Text to look for in column names (case-insensitive substring)")],
        ctx: Context,
    ) -> Annotated[types.CallToolResult, models.SearchColumnNamesResult]:
        """Find columns whose names contain a term, without retrieving the whole header. Useful for files with
        hundreds of columns. Returns matching column names, their positions, and a selector for each that is
        safe to pass to the other tools."""
        return await scan(ctx, "search_column_names", {"folder": folder, "filename": filename, "term": term}, None,
                          lambda control: service.search_column_names(folder, filename, term, control=control))

    # ---------------------------------------------------------------- scans

    @server.tool(annotations=read_only)
    async def find_security_in_file(
        folder: Folder,
        filename: Filename,
        identifier: Identifier,
        identifier_column: Annotated[ColumnSelector, Field(description="The column to search")],
        ctx: Context,
        filters: Filters = None,
        columns: Columns = None,
        max_rows: MaxRows = None,
        cursor: Cursor = None,
        max_scan_seconds: ScanSeconds = None,
    ) -> Annotated[types.CallToolResult, models.SearchResult]:
        """Find the rows of one file whose identifier_column equals the identifier exactly, optionally narrowed
        by `filters` to one portfolio, date or other record key. Returns each matching row with its original
        column labels (the whole row, or just `columns`). Values are text exactly as in the file. The response
        states whether the scan completed and whether all matches were returned; when results are partial,
        pass next_cursor to continue."""
        request = {"folder": folder, "filename": filename, "identifier": identifier,
                   "identifier_column": identifier_column, "filters": filters, "columns": columns, "max_rows": max_rows}
        return await scan(ctx, "find_security_in_file", request, max_scan_seconds, lambda control:
                          service.find_security_in_file(control, folder, filename, identifier, _plain(identifier_column),
                                                        _plain(columns), max_rows, cursor, _plain(filters)))

    @server.tool(annotations=read_only)
    async def search_across_columns(
        folder: Folder,
        filename: Filename,
        identifier: Identifier,
        ctx: Context,
        filters: Filters = None,
        columns: Columns = None,
        max_rows: MaxRows = None,
        cursor: Cursor = None,
        max_scan_seconds: ScanSeconds = None,
    ) -> Annotated[types.CallToolResult, models.SearchResult]:
        """Find rows where ANY column equals the identifier exactly - for when you do not know which column
        holds it. Each matching row is returned once, with matched_columns naming every column that matched
        (matched columns are always included in the returned values). `filters` narrow it to records where
        named columns also equal given values."""
        request = {"folder": folder, "filename": filename, "identifier": identifier, "filters": filters,
                   "columns": columns, "max_rows": max_rows}
        return await scan(ctx, "search_across_columns", request, max_scan_seconds, lambda control:
                          service.search_across_columns(control, folder, filename, identifier, _plain(columns),
                                                        max_rows, cursor, _plain(filters)))

    @server.tool(annotations=read_only)
    async def count_matching_rows(
        folder: Folder,
        filename: Filename,
        identifier: Identifier,
        identifier_column: Annotated[ColumnSelector, Field(description="The column to search")],
        ctx: Context,
        filters: Filters = None,
        cursor: Cursor = None,
        max_scan_seconds: ScanSeconds = None,
    ) -> Annotated[types.CallToolResult, models.CountResult]:
        """Count the rows whose identifier_column equals the identifier exactly (and that satisfy `filters`, if
        given), without returning row contents. `count` is only given after a complete, reliable scan of the
        file. If the scan is interrupted or the file cannot be read reliably, count is null, count_is_exact is
        false, and next_cursor (when present) continues the count."""
        request = {"folder": folder, "filename": filename, "identifier": identifier,
                   "identifier_column": identifier_column, "filters": filters}
        return await scan(ctx, "count_matching_rows", request, max_scan_seconds, lambda control:
                          service.count_matching_rows(control, folder, filename, identifier, _plain(identifier_column),
                                                      cursor, _plain(filters)))

    @server.tool(annotations=read_only)
    async def compare_security_records(
        folder_a: Annotated[str, Field(description="Folder of file A, as returned by list_folders")],
        filename_a: Annotated[str, Field(description="File A")],
        folder_b: Annotated[str, Field(description="Folder of file B")],
        filename_b: Annotated[str, Field(description="File B")],
        identifier: Identifier,
        identifier_column_a: Annotated[ColumnSelector, Field(description="Identifier column in file A")],
        ctx: Context,
        identifier_column_b: Annotated[ColumnSelector | None, Field(description="Identifier column in file B, if named differently")] = None,
        identifier_b: Annotated[str | None, Field(description="Identifier value to look for in file B, if it differs from file A (e.g. a different identifier type)")] = None,
        filters_a: Annotated[list[RecordFilter] | None, Field(max_length=MAX_FILTERS, description="Exact-match conditions on file A's records, using file A's columns")] = None,
        filters_b: Annotated[list[RecordFilter] | None, Field(max_length=MAX_FILTERS, description="Exact-match conditions on file B's records, using file B's columns. Independent of filters_a: nothing is copied or renamed across.")] = None,
        compare_columns: Annotated[list[ColumnSelector] | None, Field(description="Columns to compare, selected in file A. Omit to compare every column the files share.")] = None,
        column_mapping: Annotated[
            dict[str, str] | list[ColumnPair] | None,
            Field(description="Which file B column corresponds to a file A column, for columns named differently: "
                              "{\"A name\": \"B name\"}, or a list of {\"a\": selector, \"b\": selector} pairs when a "
                              "position is needed (e.g. repeated names). Every entry must resolve and be used, or the "
                              "call is refused."),
        ] = None,
        match_keys: Annotated[list[ColumnSelector] | None, Field(description="File A columns that pair up records when a file holds several for the security (e.g. portfolio, trade date)")] = None,
        max_scan_seconds: ScanSeconds = None,
    ) -> Annotated[types.CallToolResult, models.CompareResult]:
        """Find one security in two files and compare field values. Reports whether it is present in each file
        and lists changed, unchanged and structurally missing values, and how many columns were actually
        compared. Values are compared as exact text; differences that are only formatting (whitespace, case,
        numeric format) are still listed as changed, with a note. If either file has several matching records,
        narrow them with filters_a / filters_b or supply match_keys; otherwise the alignment is reported as
        ambiguous rather than guessed. If no columns can be paired the status is no_comparable_columns."""
        request = {"folder_a": folder_a, "filename_a": filename_a, "folder_b": folder_b, "filename_b": filename_b,
                   "identifier": identifier, "identifier_column_a": identifier_column_a,
                   "identifier_column_b": identifier_column_b, "identifier_b": identifier_b,
                   "filters_a": filters_a, "filters_b": filters_b,
                   "compare_columns": compare_columns, "column_mapping": column_mapping, "match_keys": match_keys}
        return await scan(ctx, "compare_security_records", request, max_scan_seconds, lambda control:
                          service.compare_security_records(
                              control, folder_a, filename_a, folder_b, filename_b, identifier,
                              _plain(identifier_column_a), _plain(identifier_column_b), identifier_b,
                              _plain(compare_columns), _plain(column_mapping), _plain(match_keys),
                              _plain(filters_a), _plain(filters_b)))

    return server


def _bounded(value: Any) -> Any:
    """Keep the echo of a request small: long strings are cut, long collections summarised."""
    if isinstance(value, str):
        return value if len(value) <= 200 else f"{value[:200]}...[{len(value)} characters]"
    if isinstance(value, list):
        head = [_bounded(v) for v in value[:5]]
        return head if len(value) <= 5 else head + [f"...and {len(value) - 5} more"]
    if isinstance(value, dict):
        items = list(value.items())
        head = {_bounded(k): _bounded(v) for k, v in items[:5]}
        if len(items) > 5:
            head["..."] = f"and {len(items) - 5} more"
        return head
    return value


def _raise(exc: Exception) -> Callable[[], dict[str, Any]]:
    def thrower() -> dict[str, Any]:
        raise exc
    return thrower


def main() -> None:
    parser = argparse.ArgumentParser(prog="csv-o-matic", description=__doc__)
    parser.add_argument("--config", help="Path to the YAML configuration file (default: $CSV_O_MATIC_CONFIG)")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        sys.exit(f"csv-o-matic: {exc}")
    build_server(config).run(transport=args.transport)


if __name__ == "__main__":
    main()
