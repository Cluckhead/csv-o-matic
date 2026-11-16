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
from pydantic import Field

from .config import Config, ConfigError, load_config
from . import schemas
from .csvscan import ScanControl
from .service import Service, ToolFailure

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
   Folders may be nested (by date, run, portfolio...). list_csv_files names a folder's subfolders, and
   list_folders with folder=<path> lists them one level at a time. A subfolder path is used as `folder`
   exactly like a mapped folder, and its files follow the mapped folder's format and naming convention.
   Only mapped folders and real subfolders of them can be reached; nothing is ever searched recursively.
3. inspect_csv_headers / search_column_names to learn a file's layout.
4. find_security_in_file (known column), search_across_columns (unknown column), count_matching_rows,
   compare_security_records.

Pinning down one record: an identifier alone often matches many rows (one ISIN in a hundred portfolios).
Pass `filters` - column -> exact value, all of which must hold in the SAME row as the identifier - to ask
about one record, e.g. identifier GB1234567890 in column isin with filters {"portfolio_code": "ABC"}.
The folder's record_key_columns (list_folders / get_process_map) say which columns normally identify one
record, but any column can be a filter. With filters, match_found / confirmed_absent / counts all refer to
the identifier AND every filter together; near_misses reports rows that had the identifier but failed a filter.

Reading results:
- outcome is one of match_found, confirmed_absent (only after a complete scan) or inconclusive.
  Never treat inconclusive as absent.
- A match does not mean every match was returned: check results_are_partial / more_matches and use
  next_cursor to fetch more.
- Matching is exact, whole-value text matching. '00123' and '123' are different identifiers, and part of a
  name does not match the full name. When nothing matches exactly, check near_misses: it reports values that
  contain the search text, differ only by whitespace, or sit in a different column.
- Any exact value can be searched for, not only security identifiers (a portfolio code, for example).
- record_number is 1-based and counts the header as record 1.
- filesystem_modified_time is when a file was written to disk; business dates and run timestamps come from
  the filename tokens.
- Every response is returned twice with identical content: as structured content matching the tool's
  output schema, and as JSON text. A field that does not apply is absent; a value that is unavailable is null.
You choose the files and interpret the evidence; the server only reports what is in the files you select.
"""

Folder = Annotated[str, Field(description="Folder path exactly as returned by list_folders or list_csv_files: "
                                           "a mapped folder, or a subfolder of one")]
Filters = Annotated[
    dict[str, str] | None,
    Field(description="Optional record-key filters: column name (or '#<position>') -> exact value. A row matches "
                      "only if it holds the identifier AND every one of these values, each as the whole, "
                      "case-sensitive text of that column. Values are text: '00123' is not '123'. Typically the "
                      "folder's record_key_columns, e.g. {\"portfolio_code\": \"ABC\"}."),
]
Filename = Annotated[str, Field(description="File name exactly as returned by list_csv_files (no path)")]
Identifier = Annotated[str, Field(description="Value to match exactly against whole field values, as text. Leading zeroes are significant; partial values do not match.")]
Columns = Annotated[
    list[str] | None,
    Field(description="Columns to return for each match. Omit for the complete row. Use '#<position>' for a repeated column name."),
]
MaxRows = Annotated[int | None, Field(description="Maximum matching rows in this response (capped by the server limit)")]
Cursor = Annotated[str | None, Field(description="next_cursor from a previous response of this same search, to continue it")]
ScanSeconds = Annotated[
    float | None,
    Field(description="Optional time budget for this scan in seconds (capped by the server limit). "
                      "When it runs out the response is marked incomplete and can be continued."),
]


def build_server(config: Config, service: Service | None = None) -> MCPServer:
    service = service or Service(config)
    limits = config.limits
    scan_slots = anyio.CapacityLimiter(limits.max_concurrent_scans)
    threads = anyio.CapacityLimiter(limits.max_concurrent_scans + 8)  # scan_slots is the real limit
    read_only = types.ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
    server = MCPServer("csv-o-matic", instructions=INSTRUCTIONS)

    def respond(tool: str, payload: dict[str, Any], is_error: bool = False) -> types.CallToolResult:
        text = json.dumps(payload, ensure_ascii=False)
        try:
            size = len(text.encode("utf-8"))
        except UnicodeEncodeError:  # e.g. a lone surrogate echoed from an argument
            text = json.dumps(payload, ensure_ascii=True)
            size = len(text)
        if size > limits.max_output_bytes:
            # Responses are built to fit; this is the backstop that guarantees the bound.
            is_error = True
            message = (f"The response exceeded the output-size limit of {limits.max_output_bytes} bytes. "
                       "Request fewer columns or rows.")
            payload = {"tool": tool}
            if tool in SEARCH_TOOLS:
                payload["outcome"] = "inconclusive"
                payload["explanation"] = f"INCONCLUSIVE: no result could be returned. {message}"
            payload["error"] = {"type": "response_too_large", "message": message}
            text = json.dumps(payload)
        # The same object twice: as structured content (described by the tool's output schema) and as
        # JSON text for clients that only read text. The size limit applies to each representation.
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)],
                                    structured_content=json.loads(text), is_error=is_error)

    def guarded(tool: str, request: dict[str, Any], fn: Callable[[], dict[str, Any]]) -> tuple[dict[str, Any], bool]:
        """Run a tool body, turning failures into explicit, structured explanations."""
        request = {k: _bounded(v) for k, v in request.items() if v is not None}
        try:
            return {"tool": tool, **fn()}, False
        except ToolFailure as failure:
            error = {"type": failure.kind, "message": failure.message, **failure.details}
        except Exception as exc:  # noqa: BLE001 - report, never crash the session
            log.exception("%s failed", tool)
            error = {"type": "internal_error", "message": f"{type(exc).__name__}: {exc}"}
        payload: dict[str, Any] = {"tool": tool}
        if tool in SEARCH_TOOLS:
            payload["outcome"] = "inconclusive"
            payload["explanation"] = f"INCONCLUSIVE: no search was carried out. {error['message']}"
        payload["error"] = error
        payload["request"] = request
        return payload, True

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
                if control.started is None:
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

    @server.tool(annotations=read_only)
    async def get_process_map(
        process: Annotated[str | None, Field(description="Process id or name. Omit to list the available processes.")] = None,
        step_id: Annotated[str | None, Field(description="Optional: return only this step, in full, with the folders it touches")] = None,
    ) -> Annotated[types.CallToolResult, schemas.GetProcessMapResult]:
        """Describe an ETL process: purpose, steps, folders, upstream/downstream links (including branches),
        filename conventions and timestamp meanings, identifier columns, field mappings, and known
        transformation and filter rules. Called without arguments it lists the available processes
        (id, name, description) so you can pick one. Start every investigation here."""
        return await quick("get_process_map", {"process": process, "step_id": step_id},
                           lambda: service.get_process_map(process, step_id))

    @server.tool(annotations=read_only)
    async def list_folders(
        process: Annotated[str | None, Field(description="Process id or name. Omit for the folders of all processes.")] = None,
        offset: Annotated[int, Field(description="Continue a truncated listing from this next_offset")] = 0,
        folder: Annotated[str | None, Field(description="A mapped folder or subfolder, as returned by this tool. When "
                                                        "given, its immediate subfolders are listed instead (one "
                                                        "level; not recursive). Do not combine with process.")] = None,
    ) -> Annotated[types.CallToolResult, schemas.ListFoldersResult]:
        """List the folders described by the process maps - all of them, or those of one process. Each entry
        has the folder path, its description, the process steps that read or write it, and its identifier and
        record-key columns. Use this to decide which folders may hold relevant data, then call list_csv_files
        on them. With `folder`, lists the immediate subfolders of that folder instead (kind 'subfolder', each
        tied to its mapped_root), so nested layouts such as year/month/day can be walked one level at a time.
        Only mapped folders and their real subfolders exist for this server; links are never followed."""
        return await quick("list_folders", {"process": process, "offset": offset, "folder": folder},
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
    ) -> Annotated[types.CallToolResult, schemas.ListCsvFilesResult]:
        """List the CSV files that actually exist in one folder right now, with size and filesystem modification
        time, plus the dates/timestamps parsed from each filename according to the folder's naming convention.
        Filesystem modification time and filename dates are reported separately and mean different things.
        Optional filename and date filters narrow the listing. Only files directly in the folder are listed;
        its subfolders are named in `subfolders` and must be listed separately."""
        request = {"folder": folder, "filename_contains": filename_contains, "date_from": date_from,
                   "date_to": date_to, "date_field": date_field, "offset": offset}
        return await quick("list_csv_files", request, lambda: service.list_csv_files(
            folder, filename_contains, date_from, date_to, date_field, offset, newest_first))

    @server.tool(annotations=read_only)
    async def inspect_csv_headers(
        folder: Folder,
        filename: Filename,
        sample_rows: Annotated[int, Field(description="Number of leading data rows to include, up to the server's sample limit. Default 0: headers only.")] = 0,
        sample_columns: Annotated[list[str] | None, Field(description="Limit sample rows to these columns")] = None,
        column_offset: Annotated[int, Field(description="Continue a truncated column list from this next_column_offset")] = 0,
    ) -> Annotated[types.CallToolResult, schemas.InspectCsvHeadersResult]:
        """Return a CSV file's column names in their original order. Optionally include a few leading data rows
        to understand the layout. The sample is only the start of the file - never use it to judge whether a
        security is present."""
        request = {"folder": folder, "filename": filename, "sample_rows": sample_rows,
                   "sample_columns": sample_columns, "column_offset": column_offset}
        return await quick("inspect_csv_headers", request, lambda: service.inspect_csv_headers(
            folder, filename, sample_rows, column_offset, sample_columns))

    @server.tool(annotations=read_only)
    async def search_column_names(
        folder: Folder,
        filename: Filename,
        term: Annotated[str, Field(description="Text to look for in column names (case-insensitive substring)")],
    ) -> Annotated[types.CallToolResult, schemas.SearchColumnNamesResult]:
        """Find columns whose names contain a term, without retrieving the whole header. Useful for files with
        hundreds of columns. Returns matching column names and their positions."""
        return await quick("search_column_names", {"folder": folder, "filename": filename, "term": term},
                           lambda: service.search_column_names(folder, filename, term))

    # ---------------------------------------------------------------- scans

    @server.tool(annotations=read_only)
    async def find_security_in_file(
        folder: Folder,
        filename: Filename,
        identifier: Identifier,
        identifier_column: Annotated[str, Field(description="Exact name of the column to search")],
        ctx: Context,
        columns: Columns = None,
        max_rows: MaxRows = None,
        cursor: Cursor = None,
        max_scan_seconds: ScanSeconds = None,
        filters: Filters = None,
    ) -> Annotated[types.CallToolResult, schemas.SearchResult]:
        """Find the rows of one file whose identifier_column equals the identifier exactly. Returns each matching
        row with its original column labels (the whole row, or just `columns`). Values are text exactly as in
        the file. The response states whether the scan completed and whether all matches were returned; when
        results are partial, pass next_cursor to continue. Add `filters` to ask about one specific record
        (e.g. this ISIN in this portfolio): confirmed_absent then means no row has the identifier together
        with every filter value."""
        request = {"folder": folder, "filename": filename, "identifier": identifier,
                   "identifier_column": identifier_column, "filters": filters, "columns": columns, "max_rows": max_rows}
        return await scan(ctx, "find_security_in_file", request, max_scan_seconds, lambda control:
                          service.find_security_in_file(control, folder, filename, identifier, identifier_column,
                                                        columns, max_rows, cursor, filters))

    @server.tool(annotations=read_only)
    async def search_across_columns(
        folder: Folder,
        filename: Filename,
        identifier: Identifier,
        ctx: Context,
        columns: Columns = None,
        max_rows: MaxRows = None,
        cursor: Cursor = None,
        max_scan_seconds: ScanSeconds = None,
        filters: Filters = None,
    ) -> Annotated[types.CallToolResult, schemas.SearchResult]:
        """Find rows where ANY column equals the identifier exactly - for when you do not know which column
        holds it. Each matching row is returned once, with matched_columns naming every column that matched
        (matched columns are always included in the returned values). With `filters`, the identifier must
        match at least one column of a row that also satisfies every filter."""
        request = {"folder": folder, "filename": filename, "identifier": identifier, "filters": filters,
                   "columns": columns, "max_rows": max_rows}
        return await scan(ctx, "search_across_columns", request, max_scan_seconds, lambda control:
                          service.search_across_columns(control, folder, filename, identifier, columns, max_rows,
                                                        cursor, filters))

    @server.tool(annotations=read_only)
    async def count_matching_rows(
        folder: Folder,
        filename: Filename,
        identifier: Identifier,
        identifier_column: Annotated[str, Field(description="Exact name of the column to search")],
        ctx: Context,
        cursor: Cursor = None,
        max_scan_seconds: ScanSeconds = None,
        filters: Filters = None,
    ) -> Annotated[types.CallToolResult, schemas.CountResult]:
        """Count the rows whose identifier_column equals the identifier exactly, without returning row contents.
        `count` is only given after a complete scan of the file. If the scan is interrupted, count is null,
        count_is_exact is false, and next_cursor (when present) continues the count. With `filters`, only
        rows that also satisfy every filter are counted."""
        request = {"folder": folder, "filename": filename, "identifier": identifier,
                   "identifier_column": identifier_column, "filters": filters}
        return await scan(ctx, "count_matching_rows", request, max_scan_seconds, lambda control:
                          service.count_matching_rows(control, folder, filename, identifier, identifier_column,
                                                      cursor, filters))

    @server.tool(annotations=read_only)
    async def compare_security_records(
        folder_a: Annotated[str, Field(description="Folder of file A, as returned by list_folders")],
        filename_a: Annotated[str, Field(description="File A")],
        folder_b: Annotated[str, Field(description="Folder of file B")],
        filename_b: Annotated[str, Field(description="File B")],
        identifier: Identifier,
        identifier_column_a: Annotated[str, Field(description="Identifier column in file A")],
        ctx: Context,
        identifier_column_b: Annotated[str | None, Field(description="Identifier column in file B, if named differently")] = None,
        identifier_b: Annotated[str | None, Field(description="Identifier value to look for in file B, if it differs from file A (e.g. a different identifier type)")] = None,
        compare_columns: Annotated[list[str] | None, Field(description="Columns to compare, by their file A names. Omit to compare every column the files share.")] = None,
        column_mapping: Annotated[dict[str, str] | None, Field(description="File A column name -> file B column name, for columns named differently")] = None,
        match_keys: Annotated[list[str] | None, Field(description="File A column names that pair up records when a file holds several for the security (e.g. portfolio, trade date)")] = None,
        max_scan_seconds: ScanSeconds = None,
        filters: Annotated[
            dict[str, str] | None,
            Field(description="Optional record-key filters, by file A column name -> exact value, e.g. "
                              "{\"portfolio_code\": \"ABC\"}. Applied to both files (file B's column names come "
                              "from column_mapping), so only the record matching the identifier and every filter "
                              "is compared."),
        ] = None,
        filters_b: Annotated[
            dict[str, str] | None,
            Field(description="Filters for file B, by file B column name -> exact value, when its columns or values "
                              "differ from file A's. Replaces `filters` for file B; pass {} for no filter on file B."),
        ] = None,
    ) -> Annotated[types.CallToolResult, schemas.CompareResult]:
        """Find one security in two files and compare field values. Reports whether it is present in each file
        and lists changed and unchanged values. Values are compared as exact text; differences that are only
        formatting (whitespace, case, numeric format) are still listed as changed, with a note. If either file
        has several matching records, either narrow to one record with `filters` (e.g. one portfolio) or
        supply match_keys to pair them up; otherwise the alignment is reported as ambiguous rather than guessed."""
        request = {"folder_a": folder_a, "filename_a": filename_a, "folder_b": folder_b, "filename_b": filename_b,
                   "identifier": identifier, "identifier_column_a": identifier_column_a,
                   "identifier_column_b": identifier_column_b, "identifier_b": identifier_b,
                   "compare_columns": compare_columns, "column_mapping": column_mapping, "match_keys": match_keys,
                   "filters": filters, "filters_b": filters_b}
        return await scan(ctx, "compare_security_records", request, max_scan_seconds, lambda control:
                          service.compare_security_records(control, folder_a, filename_a, folder_b, filename_b,
                                                           identifier, identifier_column_a, identifier_column_b,
                                                           identifier_b, compare_columns, column_mapping, match_keys,
                                                           filters, filters_b))

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
