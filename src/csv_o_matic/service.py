"""The nine tools, as plain synchronous functions returning JSON-ready dicts.

Nothing here knows about MCP. Scanning functions take a `ScanControl` so the caller can
impose a time limit, cancel, and watch progress. Files are only ever opened read-only.
"""

from __future__ import annotations

import base64
import difflib
import hashlib
import hmac
import json
import os
import secrets
import stat as stat_module
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, BinaryIO

from .compare import ComparisonReport, Pairing, SideScan, describe_pairing
from .config import Config
from .csvscan import CsvParseError, Record, ScanControl, ScanResult, iter_records, prefilter_needle
from .errors import ToolFailure
from .output import (
    COLUMN_SEARCH_RESERVE_BYTES,
    COMPARE_STATUS_RESERVE_BYTES,
    ENVELOPE_RESERVE_BYTES,
    HEADER_NOTES_RESERVE_BYTES,
    MIN_ROWS_BYTES,
    SCAN_RESERVE_BYTES,
    TOOL_NAME_BYTES,
    Budget,
    clip,
    size,
    some,
)
from .process_maps import Catalog, CsvFormat, FolderRef, ProcessMap, load_catalog
from .rows import LABEL_MAX_CHARS, Header, RowFormatter
from .search import MATCH_TYPE, STOP_EXPLANATIONS, Findings, Hints, SearchRequest, add_extras, report_search

__all__ = ["Service", "ToolFailure"]

MAX_IDENTIFIER_CHARS = 200
MAX_MATCH_KEYS = 10
ALTERNATIVE_ENCODINGS = ("utf-8", "cp1252", "latin-1")


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat(timespec="seconds")


@dataclass
class OpenFile:
    ref: FolderRef
    path: Path
    name: str
    fmt: CsvFormat
    size: int
    mtime_ns: int
    identity: tuple[int, int] = (0, 0)  # (device, inode) of the file that was checked

    def info(self) -> dict[str, Any]:
        return {
            "folder": self.ref.path,
            "filename": self.name,
            "size_bytes": self.size,
            "filesystem_modified_time": _iso(self.mtime_ns / 1e9),
        }


@dataclass
class _SearchPlan:
    """A search request resolved against one file."""

    of: OpenFile
    header: Header
    identifier: str
    column: int | None  # the column searched; None for every column
    wanted: list[int] | None  # the columns to return; None for all
    row_limit: int
    count_only: bool
    fingerprint: str
    start_offset: int
    start_record: int
    before: int  # matches reported by earlier responses in the cursor chain


class Service:
    def __init__(self, config: Config):
        self.config = config
        self.limits = config.limits
        self.rows = RowFormatter(config.limits.max_cell_chars)
        self._cursor_key = secrets.token_bytes(32)

    # ------------------------------------------------------------------ process maps

    def catalog(self) -> Catalog:
        return load_catalog(self.config.process_maps_dir, self.config.data_root)

    def _process(self, catalog: Catalog, name_or_id: str) -> ProcessMap:
        process = catalog.find_process(name_or_id)
        if process is None:
            raise ToolFailure(
                "process_not_found",
                f"No process map named or identified as '{clip(name_or_id, 200)}'.",
                available_processes=[{"id": p.id, "name": p.name} for p in catalog.processes],
                process_map_load_errors=catalog.load_errors,
            )
        return process

    def get_process_map(self, process: str | None = None, step_id: str | None = None) -> dict[str, Any]:
        catalog = self.catalog()
        if not process:
            out: dict[str, Any] = {
                "processes": [{"id": p.id, "name": p.name, "description": p.description} for p in catalog.processes],
                "next_step": "Call get_process_map with a process id or name for the full map.",
            }
            if catalog.load_errors:
                out["process_map_load_errors"] = catalog.load_errors
            return out
        proc = self._process(catalog, process)
        full = proc.model_dump(exclude_none=True, exclude_defaults=False)
        full["steps"] = [{k: v for k, v in s.items() if v not in ([], None)} for s in full["steps"]]
        full["folders"] = [{k: v for k, v in f.items() if v not in ([], None)} for f in full["folders"]]
        if step_id:
            step = next((s for s in full["steps"] if s["id"] == step_id), None)
            if step is None:
                raise ToolFailure("step_not_found", f"Process '{proc.id}' has no step '{clip(step_id, 200)}'.",
                                  available_steps=[s.id for s in proc.steps])
            touched = set(step.get("reads", [])) | set(step.get("writes", []))
            detail: dict[str, Any] = {"process_id": proc.id, "process_name": proc.name, "step": step,
                                      "folders": [f for f in full["folders"] if f["id"] in touched]}
            room = self.limits.max_output_bytes - ENVELOPE_RESERVE_BYTES
            if size(detail) > room:
                # Keep the step's shape and as much of each long list as fits, and say what was cut.
                detail["folders"] = [{k: f[k] for k in ("id", "path", "description")} for f in detail["folders"]]
                cut: dict[str, str] = {}
                lists = [k for k in ("field_mappings", "transformation_rules", "filter_rules") if step.get(k)]
                for key in lists:
                    items, step[key] = step[key], []
                    share = max((room - size(detail)) // (len(lists) - lists.index(key)), 0)
                    step[key] = Budget(share).leading(items)
                    if len(step[key]) < len(items):
                        cut[key] = f"{len(step[key])} of {len(items)} shown"
                detail["truncated"] = True
                detail["truncation_note"] = ("This step is too large for one response; some lists are cut short "
                                             f"({cut}). The complete step is in the process map file itself.")
            return detail
        result = {"process": full}
        if size(result) <= self.limits.max_output_bytes - ENVELOPE_RESERVE_BYTES:
            return result
        # Too large for one response: return the shape of the process and let the caller drill in.
        brief_keys = ("id", "name", "description", "reads", "writes", "upstream", "downstream", "branch_condition")
        overview = {k: v for k, v in full.items() if k not in ("steps", "folders")}
        overview["steps"] = [{k: s[k] for k in brief_keys if k in s} for s in full["steps"]]
        overview["folders"] = [{k: f[k] for k in ("id", "path", "description")} for f in full["folders"]]
        return {
            "process": overview,
            "truncated": True,
            "truncation_note": "The full map exceeds the output-size limit, so step and folder detail was left out. "
            "Call get_process_map again with step_id for one step's mappings, rules and folders.",
        }

    def list_folders(self, process: str | None = None, offset: int = 0) -> dict[str, Any]:
        catalog = self.catalog()
        proc = self._process(catalog, process) if process else None
        refs = sorted(catalog.folders(proc), key=lambda r: r.path)
        budget = Budget(self.limits.max_output_bytes - ENVELOPE_RESERVE_BYTES)
        folders: list[dict[str, Any]] = []
        next_offset = None
        for index in range(max(offset, 0), len(refs)):
            ref = refs[index]
            descriptions = list(dict.fromkeys(folder.description for _, folder in ref.usages))
            usages = []
            for owner, folder in ref.usages:
                steps = [
                    {"step_id": s.id, "step_name": s.name, "role": role}
                    for s in owner.steps
                    for role, ids in (("reads", s.reads), ("writes", s.writes))
                    if folder.id in ids
                ]
                usage: dict[str, Any] = {"process_id": owner.id, "folder_id": folder.id, "steps": steps}
                if len(descriptions) > 1:
                    usage["description"] = folder.description  # each process describes it differently
                usages.append(usage)
            entry: dict[str, Any] = {"folder": ref.path}
            if len(descriptions) == 1:
                entry["description"] = descriptions[0]
            entry["exists_on_disk"] = ref.resolved.is_dir()
            entry["used_by"] = usages
            convention = ref.filename_convention
            if convention:
                entry["filename_pattern"] = convention.pattern
            identifiers: dict[str, str | None] = {}
            for _, folder in ref.usages:
                for column in folder.identifier_columns:
                    identifiers.setdefault(column.column, column.identifier_type)
            if identifiers:
                entry["identifier_columns"] = [
                    {"column": name, "identifier_type": kind} if kind else {"column": name}
                    for name, kind in identifiers.items()]
            if not budget.take(size(entry), force=not folders):  # always at least one
                next_offset = index
                break
            folders.append(entry)
        out: dict[str, Any] = {
            "scope": f"process '{proc.id}'" if proc else "all processes",
            "total_folders": len(refs),
            "folders_returned": len(folders),
            "folders": folders,
            "truncated": next_offset is not None,
            "note": "Pass a folder's 'folder' value to list_csv_files. get_process_map has the rest: what each "
                    "filename token means, record keys, field mappings and filter rules.",
        }
        if next_offset is not None:
            out["next_offset"] = next_offset
        if catalog.load_errors:
            out["process_map_load_errors"] = catalog.load_errors
        return out

    # ------------------------------------------------------------------ files and headers

    def _folder(self, folder: str, catalog: Catalog | None = None) -> FolderRef:
        catalog = catalog or self.catalog()
        ref = catalog.find_folder(folder) if folder and folder.strip() else None
        if ref is None:
            known = [r.path for r in catalog.folders()]
            raise ToolFailure(
                "folder_not_found",
                f"'{clip(folder or '', 200)}' is not a folder described by any process map. Only mapped folders can be read.",
                closest_known_folders=difflib.get_close_matches((folder or "")[:300], known, n=5, cutoff=0.4),
                hint="Call list_folders for the full inventory.",
            )
        return ref

    def _open(self, folder: str, filename: str, catalog: Catalog | None = None) -> OpenFile:
        """Check that `filename` is a readable CSV file in a mapped folder.

        A caller that opens several files passes one `catalog`, so all are judged by the same maps.
        """
        ref = self._folder(folder, catalog)
        if (not filename or len(filename) > 255 or filename in (".", "..") or "/" in filename
                or "\\" in filename or "\x00" in filename):
            raise ToolFailure("invalid_argument", "filename must be a bare file name inside the folder, without any path.")
        if not filename.lower().endswith(".csv"):
            raise ToolFailure(
                "not_a_csv_file",
                f"'{filename}' does not have a .csv extension. Only .csv files are read; compressed or other "
                "files are not searched, so nothing can be concluded about its contents.",
            )
        fmt, conflict = ref.csv_format()
        if conflict:
            raise ToolFailure(
                "process_map_conflict",
                f"The process maps disagree about the CSV format of folder '{ref.path}' ({conflict}). The files "
                "cannot be read reliably until the maps agree.",
            )
        path = ref.resolved / filename
        try:
            st = os.lstat(path)  # lstat: a symbolic link is never followed
        except FileNotFoundError:
            try:
                names = [e.name for e in os.scandir(ref.resolved) if e.name.lower().endswith(".csv")]
            except OSError:
                names = []
            raise ToolFailure(
                "file_not_found",
                f"No file named '{filename}' in folder '{ref.path}'.",
                closest_filenames=difflib.get_close_matches(filename, names, n=3, cutoff=0.6),
                hint="Call list_csv_files to see the files that exist.",
            ) from None
        except OSError as exc:
            raise ToolFailure("file_unreadable", f"Cannot access '{filename}' in '{ref.path}': {exc.strerror or exc}.") from exc
        if not stat_module.S_ISREG(st.st_mode):
            raise ToolFailure("file_unreadable", f"'{filename}' is not a regular file (links and special files are not read).")
        return OpenFile(ref, path, filename, fmt, st.st_size, st.st_mtime_ns, (st.st_dev, st.st_ino))

    def _fh(self, of: OpenFile) -> BinaryIO:
        """Open read-only - the only way files are ever opened - and confirm it is the file that was checked."""
        try:
            fd = os.open(of.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        except OSError as exc:
            raise ToolFailure("file_unreadable", f"Cannot open '{of.name}' for reading: {exc.strerror or exc}.") from exc
        try:
            st = os.fstat(fd)
            same = ((st.st_dev, st.st_ino) == of.identity and (st.st_size, st.st_mtime_ns) == (of.size, of.mtime_ns))
            if not stat_module.S_ISREG(st.st_mode) or not same:
                raise ToolFailure("file_unreadable", f"'{of.name}' was replaced or modified while it was being opened; try again.")
            return os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise

    def _header(self, fh: BinaryIO, of: OpenFile, control: ScanControl | None = None) -> Header:
        """Read the header row. `control` lets a caller's time limit and cancellation apply to it."""
        probe = ScanControl(max_seconds=self.limits.max_scan_seconds if control is None else control.max_seconds,
                            stop=threading.Event() if control is None else control.stop)
        if control is not None:
            control.begin()
            probe.started = control.started
        scan = ScanResult()
        try:
            head = fh.read(4096)
            if head[:2] == b"\x1f\x8b" or head[:4] == b"PK\x03\x04":
                raise ToolFailure("unsupported_encoding",
                                  f"'{of.name}' is compressed data, not CSV text. Compressed files are not read, "
                                  "so nothing can be concluded about its contents.")
            if (head[:2] in (b"\xff\xfe", b"\xfe\xff") or head[:4] == b"\x00\x00\xfe\xff"
                    or (len(head) >= 8 and head.count(0) * 4 >= len(head))):
                raise ToolFailure(
                    "unsupported_encoding",
                    f"'{of.name}' looks like UTF-16 or UTF-32 text (or binary data), which cannot be searched. "
                    f"Supported encodings are single-byte or UTF-8; this folder is declared as {of.fmt.encoding}. "
                    "Nothing can be concluded about this file's contents.",
                )
            first = next(
                iter_records(fh, delimiter=of.fmt.delimiter, encoding=of.fmt.encoding, control=probe, result=scan,
                             chunk_bytes=64 * 1024, max_record_bytes=self.limits.max_record_bytes),
                None,
            )
        except CsvParseError as exc:
            raise ToolFailure("parse_error", clip(f"Cannot read the header row of '{of.name}': {exc}", 500)) from exc
        except OSError as exc:
            raise ToolFailure("file_unreadable", f"Reading '{of.name}' failed: {exc.strerror or exc}.") from exc
        if first is None and scan.stop_reason == "cancelled":
            raise ToolFailure("cancelled", f"The operation was cancelled before '{of.name}' was read.")
        if first is None and scan.stop_reason == "time_limit":
            raise ToolFailure(
                "header_not_read",
                f"The time limit was reached before the header row of '{of.name}' could be read. Its first line "
                "is unusually long, which usually means a quote was opened and never closed.")
        if first is None or not first.fields:
            raise ToolFailure("parse_error", f"'{of.name}' is empty or its first line is blank, so it has no header row.")
        if scan.unterminated_quote_at_eof:
            raise ToolFailure(
                "parse_error",
                f"The header row of '{of.name}' opens a quote that is never closed, so the whole file reads as "
                "one header record and none of its data can be searched. Nothing can be concluded about its contents.")
        return Header.build(first.fields, first.end)

    def _column(self, header: Header, spec: str, of: OpenFile) -> int:
        """Resolve a column given by exact name, by label, or as '#<position>'."""
        matches = [i for i, name in enumerate(header.columns) if name == spec]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ToolFailure(
                "ambiguous_column",
                f"Column name '{clip(spec, 80)}' appears {len(matches)} times in '{of.name}' (first positions "
                f"{[m + 1 for m in matches[:8]]}). Select one by position, e.g. '#{matches[0] + 1}'.",
            )
        if spec in header.labels:
            return header.labels.index(spec)
        if spec.startswith("#") and spec[1:].isdigit() and 1 <= int(spec[1:]) <= len(header.columns):
            return int(spec[1:]) - 1
        folded = [header.display(i) for i, name in enumerate(header.columns)
                  if name.strip().casefold() == spec.strip().casefold()]
        close = difflib.get_close_matches(spec, [n for n in header.columns if len(n) <= LABEL_MAX_CHARS], n=5, cutoff=0.6)
        raise ToolFailure(
            "column_not_found",
            f"'{of.name}' has no column named '{clip(spec, 80)}' (column names are matched exactly).",
            similar_column_names=list(dict.fromkeys(folded[:5] + close)),
            hint="Use search_column_names or inspect_csv_headers to find the right column.",
        )

    def list_csv_files(self, folder: str, filename_contains: str | None = None, date_from: str | None = None,
                       date_to: str | None = None, date_field: str | None = None, offset: int = 0,
                       descending: bool = True) -> dict[str, Any]:
        ref = self._folder(folder)
        convention = ref.filename_convention
        lo = self._date_bound(date_from, "date_from")
        hi = self._date_bound(date_to, "date_to")
        field = None
        if lo or hi:
            field = date_field or (convention.default_date_token if convention else None)
            valid = ["filesystem_modified"] + ([n for n, t in convention.tokens.items() if t.format] if convention else [])
            if field not in valid:
                raise ToolFailure(
                    "invalid_argument",
                    f"date_field must be one of {valid} for this folder." if field else
                    "This folder has no filename convention with a date token, so filename dates cannot be filtered. "
                    "Use date_field='filesystem_modified' or filename_contains instead.",
                )
        try:
            entries = list(os.scandir(ref.resolved))
        except OSError as exc:
            raise ToolFailure("folder_unreadable", f"Cannot list folder '{ref.path}': {exc.strerror or exc}.") from exc

        files: list[dict[str, Any]] = []
        ignored = unparseable = 0
        needle = filename_contains.casefold() if filename_contains else None
        for entry in entries:
            try:
                if not entry.name.lower().endswith(".csv") or not entry.is_file(follow_symlinks=False):
                    ignored += 1
                    continue
                st = entry.stat(follow_symlinks=False)
            except OSError:
                ignored += 1
                continue
            if needle and needle not in entry.name.casefold():
                continue
            tokens = convention.parse(entry.name) if convention else None
            item: dict[str, Any] = {
                "filename": entry.name,
                "size_bytes": st.st_size,
                "filesystem_modified_time": _iso(st.st_mtime),
            }
            if convention:
                item["filename_tokens"] = tokens
                item["matches_filename_convention"] = tokens is not None
            if field:
                value = _iso(st.st_mtime) if field == "filesystem_modified" else (tokens or {}).get(field)
                if value is None:
                    unparseable += 1
                    continue
                if (lo and value[:10] < lo) or (hi and value[:10] > hi):
                    continue
            files.append(item)
        files.sort(key=lambda f: f["filename"], reverse=descending)

        budget = Budget(self.limits.max_output_bytes - ENVELOPE_RESERVE_BYTES)
        page: list[dict[str, Any]] = []
        start = max(offset, 0)
        for item in files[start: start + self.limits.max_files_per_listing]:
            if not budget.take(size(item), force=not page):  # always at least one
                break
            page.append(item)
        more = start + len(page) < len(files)
        out: dict[str, Any] = {
            "folder": ref.path,
            "filters": {"filename_contains": filename_contains, "date_from": lo, "date_to": hi, "date_field": field},
            "total_matching_files": len(files),
            "files_returned": len(page),
            "files": page,
            "truncated": more,
            "sorted_by": "filename, " + ("descending" if descending else "ascending"),
            "time_fields": {
                "filesystem_modified_time": "When the file was last written on disk (UTC). Not a business date: "
                                            "copies, reruns and backfills change it.",
            },
        }
        if convention:
            out["filename_convention"] = {"pattern": convention.pattern}
            if convention.notes:
                out["filename_convention"]["notes"] = convention.notes
            out["time_fields"]["filename_tokens"] = {n: t.meaning for n, t in convention.tokens.items()}
        if more:
            out["next_offset"] = start + len(page)
        if ignored:
            out["other_entries_ignored"] = ignored
            out["other_entries_note"] = "Entries that are not regular .csv files (other types, links, folders) are not listed or read."
        if unparseable:
            out["files_excluded_because_filename_has_no_parseable_date"] = unparseable
        return out

    @staticmethod
    def _date_bound(value: str | None, name: str) -> str | None:
        if not value:
            return None
        text = value.strip()
        try:
            try:
                return date.fromisoformat(text).isoformat()
            except ValueError:
                return datetime.fromisoformat(text).date().isoformat()  # a full timestamp: its date
        except ValueError:
            raise ToolFailure("invalid_argument", f"{name} must be an ISO date (YYYY-MM-DD), got '{clip(value, 60)}'.") from None

    def inspect_csv_headers(self, folder: str, filename: str, sample_rows: int = 0, column_offset: int = 0,
                            sample_columns: list[str] | None = None) -> dict[str, Any]:
        of = self._open(folder, filename)
        if sample_rows < 0 or sample_rows > self.limits.max_sample_rows:
            raise ToolFailure("invalid_argument", f"sample_rows must be between 0 and {self.limits.max_sample_rows}.")
        with self._fh(of) as fh:
            header = self._header(fh, of)
            total = len(header.columns)
            out: dict[str, Any] = {"file": of.info(), "column_count": total}
            duplicates = header.duplicates()
            if duplicates:
                out["duplicate_column_names"] = duplicates
            layout = self._layout_warnings(header, of)
            if layout:
                out["warnings"] = layout
            budget = self.limits.max_output_bytes - TOOL_NAME_BYTES - size(out) - HEADER_NOTES_RESERVE_BYTES
            start = min(max(column_offset, 0), total)
            names = some([header.display(i) for i in range(start, total)], budget // 2 if sample_rows else budget)
            used = size(names)
            out["columns"] = names
            out["columns_truncated"] = start + len(names) < total
            if start:
                out["column_offset"] = start
            if out["columns_truncated"]:
                out["next_column_offset"] = start + len(names)
                out["truncation_note"] = "Not all column names fit. Continue with column_offset, or use search_column_names."
            if sample_rows:
                wanted = [self._column(header, c, of) for c in sample_columns] if sample_columns else None
                rows: list[dict[str, Any]] = []
                room = Budget(budget - used)
                truncated = False
                scan = ScanResult()
                try:
                    for record in iter_records(fh, delimiter=of.fmt.delimiter, encoding=of.fmt.encoding,
                                               start_offset=header.data_offset, start_record=1, result=scan,
                                               chunk_bytes=64 * 1024, max_record_bytes=self.limits.max_record_bytes):
                        if len(rows) >= sample_rows:
                            break
                        if not record.fields:
                            continue
                        share = max(room.room // (sample_rows - len(rows)), 300)
                        row = self.rows.fit(self.rows.row(header, record, wanted), share)
                        if not room.add(row):
                            truncated = True
                            break
                        truncated = truncated or "values_omitted_for_size" in row
                        rows.append(row)
                except CsvParseError as exc:
                    out["sample_error"] = clip(f"Sampling stopped: {exc}", 400)
                out["sample_rows"] = rows
                out["sample_note"] = (f"The first {len(rows)} data record(s) only: a layout sample, not evidence "
                                      "about any security.")
                if truncated:
                    out["sample_truncated"] = True
                if scan.undecodable_lines:
                    out["sample_warning"] = self._undecodable_warning(of)
        return out

    def search_column_names(self, folder: str, filename: str, term: str) -> dict[str, Any]:
        of = self._open(folder, filename)
        if not term or not term.strip():
            raise ToolFailure("invalid_argument", "term must not be empty.")
        with self._fh(of) as fh:
            header = self._header(fh, of)
        wanted = term.strip().casefold()
        hits = [{"column": header.display(i), "position": i + 1}
                for i, name in enumerate(header.columns) if wanted in name.casefold()]
        out: dict[str, Any] = {
            "file": of.info(),
            "term": clip(term, 200),
            "match_type": "case-insensitive substring of the column name",
            "column_count": len(header.columns),
            "matching_column_count": len(hits),
        }
        layout = self._layout_warnings(header, of)
        if layout:
            out["warnings"] = layout
        budget = Budget(self.limits.max_output_bytes - TOOL_NAME_BYTES - size(out) - COLUMN_SEARCH_RESERVE_BYTES)
        page = budget.leading(hits)
        out["matching_columns"] = page
        out["truncated"] = len(page) < len(hits)
        if not hits:
            short = [n for n in header.columns if len(n) <= LABEL_MAX_CHARS]
            out["similar_column_names"] = some(difflib.get_close_matches(term, short, n=5, cutoff=0.5), 1200)
        return out

    # ------------------------------------------------------------------ cursors

    def _fingerprint(self, tool: str, of: OpenFile, identifier: str, column: int | None) -> str:
        blob = json.dumps([tool, str(of.path), of.size, of.mtime_ns, of.fmt.delimiter, of.fmt.encoding, identifier, column])
        return hashlib.sha256(blob.encode()).hexdigest()[:24]

    def _encode_cursor(self, state: dict[str, Any]) -> str:
        body = json.dumps(state, separators=(",", ":")).encode()
        mac = hmac.new(self._cursor_key, body, hashlib.sha256).digest()[:12]
        return base64.urlsafe_b64encode(mac + body).decode()

    def _decode_cursor(self, cursor: str, fingerprint: str) -> dict[str, Any]:
        try:
            raw = base64.urlsafe_b64decode(cursor.encode())
            mac, body = raw[:12], raw[12:]
            if not hmac.compare_digest(mac, hmac.new(self._cursor_key, body, hashlib.sha256).digest()[:12]):
                raise ValueError
            state = json.loads(body)
        except (ValueError, TypeError, AttributeError):  # not base64, not JSON, not signed by this server
            raise ToolFailure(
                "invalid_cursor",
                "The cursor is not valid for this server session (cursors do not survive a server restart). "
                "Start the search again without a cursor.",
            ) from None
        if state.get("fp") != fingerprint:
            raise ToolFailure(
                "invalid_cursor",
                "The cursor belongs to a different search, or the file has changed since the cursor was issued. "
                "Start the search again without a cursor.",
            )
        return state

    # ------------------------------------------------------------------ searching

    def _check_identifier(self, identifier: str) -> None:
        if not isinstance(identifier, str) or identifier == "":
            raise ToolFailure("invalid_argument", "identifier must be a non-empty string.")
        if len(identifier) > MAX_IDENTIFIER_CHARS:
            raise ToolFailure("invalid_argument", f"identifier is longer than {MAX_IDENTIFIER_CHARS} characters.")

    @staticmethod
    def _alt_needles(identifier: str, encoding: str) -> dict[str, bytes]:
        """The identifier as it would be stored under encodings other than the declared one."""
        if identifier.isascii():
            return {}  # ASCII text is stored identically in every supported encoding
        declared = prefilter_needle(identifier, encoding)
        out: dict[str, bytes] = {}
        for name in ALTERNATIVE_ENCODINGS:
            alt = prefilter_needle(identifier, name)
            if alt and alt != declared and alt not in out.values():
                out[name] = alt
        return out

    def _undecodable_warning(self, of: OpenFile) -> str:
        return (f"Some records examined contain bytes that are not valid {of.fmt.encoding}; they are shown with "
                "the replacement character �. The folder's declared encoding may be wrong for this file.")

    def _layout_warnings(self, header: Header, of: OpenFile, identifier: str | None = None) -> list[str]:
        """Signs that the file is not laid out the way it is being read."""
        warnings: list[str] = []
        other = header.suspect_delimiter(of.fmt.delimiter)
        if other:
            warnings.append(
                f"The header row parsed as a single column containing several {other!r} characters, so the file is "
                f"probably delimited by {other!r}, not {of.fmt.delimiter!r} as declared for this folder. Values "
                "cannot be matched until the folder's csv_format in the process map is corrected.")
        if identifier is not None and identifier in header.columns:
            warnings.append(
                "The search value equals a name in the first row, which is being read as the header and is never "
                "searched. The file may have no header row, in which case its first record is being ignored.")
        return warnings

    def _integrity(self, scan: ScanResult, of: OpenFile, header: Header,
                   identifier: str) -> tuple[list[str], bool, bool]:
        """Warnings about the file, whether a complete scan of it can be trusted, and whether it changed."""
        warnings: list[str] = self._layout_warnings(header, of, identifier)
        try:
            st = os.stat(of.path)
            changed = (st.st_size, st.st_mtime_ns) != (of.size, of.mtime_ns)
        except OSError:
            changed = True
        if changed:
            warnings.append("The file was modified while it was being scanned (it may still be being written), "
                            "so the scan may have missed or misread records.")
        if scan.unterminated_quote_at_eof:
            warnings.append("The file ends inside a quoted field (unterminated quote): the final record is "
                            "incomplete and records may be hidden inside it.")
        if scan.alt_hits:
            warnings.append(
                f"The identifier's bytes were found in the file as {' / '.join(sorted(scan.alt_hits))} text, but the "
                f"folder is declared as {of.fmt.encoding}. The declared encoding is probably wrong for this file, so "
                "values with non-ASCII characters cannot be matched reliably.")
        sound = not warnings
        if scan.undecodable_lines:
            warnings.append(self._undecodable_warning(of))
        return warnings, sound, changed

    def _iter_matches(self, fh: BinaryIO, of: OpenFile, header: Header, identifier: str, column: int | None,
                      start_offset: int, start_record: int, control: ScanControl,
                      hints: Hints, scan: ScanResult) -> Iterator[tuple[Record, list[int]]]:
        """Yield (record, indices of matching fields) for exact matches, in file order.

        `hints` collects near misses on the way; `scan` reports how and where the scan ended.
        """
        for record in iter_records(
            fh, delimiter=of.fmt.delimiter, encoding=of.fmt.encoding,
            needle=prefilter_needle(identifier, of.fmt.encoding),
            start_offset=start_offset, start_record=start_record, control=control, result=scan,
            alt_needles=self._alt_needles(identifier, of.fmt.encoding),
            chunk_bytes=self.limits.chunk_bytes, max_record_bytes=self.limits.max_record_bytes,
        ):
            fields = record.fields
            if column is not None:
                value = fields[column] if column < len(fields) else None
                if value == identifier:
                    control.matches += 1
                    yield record, [column]
                    continue
                if value is not None:
                    hints.value(record, header.label(column), value)
                for i, other in enumerate(fields):
                    if i == column or identifier not in other:
                        continue
                    if other == identifier:
                        hints.other_column(header.label(i))
                    elif "\n" in other or "\r" in other:
                        hints.multi_line(record, header.label(i), other)
                continue
            found = [i for i, value in enumerate(fields) if value == identifier]
            if found:
                control.matches += 1
                yield record, found
            else:
                for i, value in enumerate(fields):
                    if identifier in value:
                        hints.value(record, header.label(i), value)

    def _search(self, tool: str, control: ScanControl, folder: str, filename: str, identifier: str,
                column: str | None, columns: list[str] | None, max_rows: int | None,
                cursor: str | None, *, count_only: bool = False) -> dict[str, Any]:
        """Shared implementation of find_security_in_file, search_across_columns and count_matching_rows.

        Three steps: resolve the request (`_plan_search`), scan the file (`_collect`), and word the
        answer (`search.report_search`).
        """
        control.begin()
        control.phase = "opening"
        self._check_identifier(identifier)
        row_limit = self.limits.max_rows_per_response
        if max_rows is not None:
            if max_rows < 1:
                raise ToolFailure("invalid_argument", "max_rows must be at least 1.")
            row_limit = min(row_limit, max_rows)
        of = self._open(folder, filename)
        limit = self.limits.max_output_bytes - TOOL_NAME_BYTES
        search: dict[str, Any] = {"identifier": identifier, "match_type": MATCH_TYPE}
        result: dict[str, Any] = {"file": of.info(), "search": search}
        with self._fh(of) as fh:
            header = self._header(fh, of, control)
            plan = self._plan_search(tool, of, header, identifier, column, columns, cursor, row_limit, count_only, search)
            control.total_bytes = of.size
            control.phase = "scanning"
            # Rows get whatever is left after the request echo and a reserve for the scan report.
            budget = Budget(max(limit - SCAN_RESERVE_BYTES - size(result), MIN_ROWS_BYTES))
            found = self._collect(fh, plan, control, budget)

        next_cursor = None
        if found.can_resume and found.resume is not None:
            next_cursor = self._encode_cursor({"fp": plan.fingerprint, "o": found.resume[0], "r": found.resume[1],
                                               "m": plan.before + found.found})
        request = SearchRequest(
            identifier=identifier, column=search.get("column"), count_only=count_only, resumed=bool(cursor),
            start_offset=plan.start_offset, matches_before=plan.before, file_size=of.size)
        report_search(result, request, found, next_cursor)
        add_extras(result, found.hints, None if count_only else header.duplicates(), limit)
        return result

    def _plan_search(self, tool: str, of: OpenFile, header: Header, identifier: str, column: str | None,
                     columns: list[str] | None, cursor: str | None, row_limit: int, count_only: bool,
                     search: dict[str, Any]) -> _SearchPlan:
        """Resolve columns and cursor against the file's header, echoing the request into `search`."""
        column_index = None
        if tool != "search_across_columns":
            column_index = self._column(header, column or "", of)
            search["column"] = header.label(column_index)
        else:
            search["columns_searched"] = f"all {len(header.columns)} columns"
        wanted = None
        if columns and not count_only:
            wanted = [self._column(header, c, of) for c in columns]
            names = [header.label(i) for i in wanted]
            shown = some(names, 800)
            search["columns_returned"] = shown if len(shown) == len(names) else {"count": len(names), "first": shown}
        elif not count_only:
            search["columns_returned"] = "all"

        fingerprint = self._fingerprint(tool, of, identifier, column_index)
        start_offset, start_record, before = header.data_offset, 1, 0
        if cursor:
            state = self._decode_cursor(cursor, fingerprint)
            start_offset, start_record, before = state["o"], state["r"], state["m"]
            search["resumed_from_cursor"] = True
            search["matches_in_earlier_responses"] = before
        if not count_only:
            search["max_rows"] = row_limit
        return _SearchPlan(of, header, identifier, column_index, wanted, row_limit, count_only,
                           fingerprint, start_offset, start_record, before)

    def _collect(self, fh: BinaryIO, plan: _SearchPlan, control: ScanControl, budget: Budget) -> Findings:
        """Scan for matches, keeping rows until the row limit or `budget` is reached."""
        of, header = plan.of, plan.header
        hints = Hints(plan.identifier)
        scan = ScanResult()
        rows: list[dict[str, Any]] = []
        found = 0
        stop_reason: str | None = None
        resume: tuple[int, int] | None = None
        error: ToolFailure | None = None
        try:
            for record, hits in self._iter_matches(fh, of, header, plan.identifier, plan.column,
                                                   plan.start_offset, plan.start_record, control, hints, scan):
                if plan.count_only:
                    found += 1
                    continue
                if len(rows) >= plan.row_limit:
                    stop_reason, resume = "row_limit", (record.offset, record.number - 1)
                    break
                row = self.rows.row(header, record, plan.wanted, hits if plan.column is None else None)
                if not budget.add(row):
                    if rows:
                        stop_reason, resume = "output_size_limit", (record.offset, record.number - 1)
                        break
                    row = self.rows.fit(row, budget.room)  # a first row is always returned, cut to fit
                    budget.add(row, force=True)
                rows.append(row)
                found += 1
        except CsvParseError as exc:
            error = ToolFailure("parse_error", clip(str(exc), 400),
                                record_number=exc.record_number, byte_offset=exc.byte_offset)
        except OSError as exc:
            error = ToolFailure("file_unreadable", f"Reading '{of.name}' failed part-way: {exc.strerror or exc}.")
        if stop_reason is None and error is None:
            stop_reason = scan.stop_reason
            resume = (scan.resume_offset, scan.resume_record)
        warnings, sound, changed = self._integrity(scan, of, header, plan.identifier)
        return Findings(
            rows=rows, found=found, stop_reason=stop_reason, resume=resume, error=error,
            warnings=warnings, sound=sound, changed=changed, hints=hints,
            stopped_at_byte=control.bytes_done if resume is None else resume[0],
            records_seen=scan.resume_record, elapsed_seconds=round(control.elapsed, 2))

    def find_security_in_file(self, control: ScanControl, folder: str, filename: str, identifier: str,
                              identifier_column: str, columns: list[str] | None = None,
                              max_rows: int | None = None, cursor: str | None = None) -> dict[str, Any]:
        return self._search("find_security_in_file", control, folder, filename, identifier,
                            identifier_column, columns, max_rows, cursor)

    def search_across_columns(self, control: ScanControl, folder: str, filename: str, identifier: str,
                              columns: list[str] | None = None, max_rows: int | None = None,
                              cursor: str | None = None) -> dict[str, Any]:
        return self._search("search_across_columns", control, folder, filename, identifier,
                            None, columns, max_rows, cursor)

    def count_matching_rows(self, control: ScanControl, folder: str, filename: str, identifier: str,
                            identifier_column: str, cursor: str | None = None) -> dict[str, Any]:
        return self._search("count_matching_rows", control, folder, filename, identifier,
                            identifier_column, None, None, cursor, count_only=True)

    def compare_security_records(self, control: ScanControl, folder_a: str, filename_a: str, folder_b: str,
                                 filename_b: str, identifier: str, identifier_column_a: str,
                                 identifier_column_b: str | None = None, identifier_b: str | None = None,
                                 compare_columns: list[str] | None = None,
                                 column_mapping: dict[str, str] | None = None,
                                 match_keys: list[str] | None = None) -> dict[str, Any]:
        """Find the security in both files (`_scan_side`), then line the records up (`ComparisonReport`)."""
        control.begin()
        control.phase = "opening"
        identifier_b = identifier if identifier_b is None else identifier_b
        self._check_identifier(identifier)
        self._check_identifier(identifier_b)
        if match_keys and len(match_keys) > MAX_MATCH_KEYS:
            raise ToolFailure("invalid_argument", f"At most {MAX_MATCH_KEYS} match_keys are supported.")
        catalog = self.catalog()
        file_a = self._open(folder_a, filename_a, catalog)
        file_b = self._open(folder_b, filename_b, catalog)
        limit = self.limits.max_output_bytes - TOOL_NAME_BYTES
        result: dict[str, Any] = {"file_a": file_a.info(), "file_b": file_b.info()}
        with self._fh(file_a) as fh_a, self._fh(file_b) as fh_b:
            head_a = self._header(fh_a, file_a, control)
            head_b = self._header(fh_b, file_b, control)
            pairing = self._pair_columns(file_a, head_a, file_b, head_b, identifier_column_a, identifier_column_b,
                                         compare_columns, column_mapping or {}, match_keys)
            describe_pairing(result, pairing, head_a, head_b, identifier, identifier_b)
            # Both scans share one time budget.
            control.total_bytes = file_a.size + file_b.size
            side_a = self._scan_side("A", fh_a, file_a, head_a, identifier, pairing.id_a, control)
            if side_a.interrupted:
                side_b = SideScan.not_scanned("File B was not scanned because the scan of file A was interrupted.")
            else:
                side_b = self._scan_side("B", fh_b, file_b, head_b, identifier_b, pairing.id_b, control)

        result["presence"] = {"file_a": side_a.presence, "file_b": side_b.presence}
        result["elapsed_seconds"] = round(control.elapsed, 2)
        warnings = side_a.warnings + side_b.warnings
        if warnings:
            result["warnings"] = warnings
        budget = Budget(limit - size(result) - COMPARE_STATUS_RESERVE_BYTES)
        report = ComparisonReport(result, budget, self.rows, head_a, head_b, pairing)
        report.write(side_a.records, side_b.records, incomplete=not (side_a.complete and side_b.complete))
        return result

    def _pair_columns(self, file_a: OpenFile, head_a: Header, file_b: OpenFile, head_b: Header,
                      identifier_column_a: str, identifier_column_b: str | None,
                      compare_columns: list[str] | None, mapping: dict[str, str],
                      match_keys: list[str] | None) -> Pairing:
        """Resolve every column the comparison names against the two headers."""
        def pair(name_a: str) -> tuple[int, int]:
            return (self._column(head_a, name_a, file_a),
                    self._column(head_b, mapping.get(name_a, name_a), file_b))

        id_a = self._column(head_a, identifier_column_a, file_a)
        id_b = self._column(head_b, identifier_column_b or mapping.get(identifier_column_a, identifier_column_a), file_b)
        only_a: list[str] = []
        if compare_columns:
            pairs = [pair(name) for name in compare_columns]
        else:
            pairs, only_a = self._default_pairs(head_a, file_b, head_b, mapping)
        keys = [pair(name) for name in match_keys] if match_keys else []
        return Pairing(id_a, id_b, pairs, keys, only_a, explicit=bool(compare_columns))

    def _default_pairs(self, head_a: Header, file_b: OpenFile, head_b: Header,
                       mapping: dict[str, str]) -> tuple[list[tuple[int, int]], list[str]]:
        """Every column of A with exactly one counterpart in B (mapped, or same name), and the A columns without."""
        pairs: list[tuple[int, int]] = []
        only_a: list[str] = []
        counts_a: dict[str, int] = {}
        for name in head_a.columns:
            counts_a[name] = counts_a.get(name, 0) + 1
        lookup_b: dict[str, int | None] = {}
        for i, name in enumerate(head_b.columns):
            lookup_b[name] = None if name in lookup_b else i  # None marks a repeated name
        for i, label in enumerate(head_b.labels):
            lookup_b.setdefault(label, i)
        for i, name in enumerate(head_a.columns):
            unique = counts_a[name] == 1
            mapped = mapping.get(name, mapping.get(head_a.labels[i])) if unique else mapping.get(head_a.labels[i])
            j: int | None
            if mapped is not None:
                j = self._column(head_b, mapped, file_b)  # an explicit mapping must resolve
            else:
                j = lookup_b.get(name) if unique else None
            if j is None:
                only_a.append(head_a.label(i))
            else:
                pairs.append((i, j))
        return pairs, only_a

    def _scan_side(self, name: str, fh: BinaryIO, of: OpenFile, header: Header, identifier: str, column: int,
                   control: ScanControl) -> SideScan:
        """Collect the security's records from one file (`name` is "A" or "B") and say how sure the result is."""
        cap = self.limits.max_rows_per_response
        control.phase = f"scanning file {name}"
        hints = Hints(identifier)
        scan = ScanResult()
        records: list[Record] = []
        reason: str | None = None
        problem: str | None = None
        try:
            for record, _ in self._iter_matches(fh, of, header, identifier, column,
                                                header.data_offset, 1, control, hints, scan):
                if len(records) >= cap:
                    reason = "too_many_matches"
                    break
                records.append(record)
        except CsvParseError as exc:
            reason, problem = "error", clip(str(exc), 300)
        except OSError as exc:
            reason, problem = "error", f"reading failed part-way: {exc.strerror or exc}"
        reason = reason or scan.stop_reason
        file_warnings, sound, _ = self._integrity(scan, of, header, identifier)
        complete = reason == "end_of_file" and sound
        presence: dict[str, Any] = {
            "outcome": "match_found" if records else ("confirmed_absent" if complete else "inconclusive"),
            "matching_records_found": len(records),
            "scan_complete": complete,
            "stop_reason": reason,
        }
        if not complete:
            presence["explanation"] = (
                f"The scan of file {name} is incomplete or unreliable because "
                f"{problem or STOP_EXPLANATIONS.get(reason or '', 'the file could not be read reliably (see warnings)')}"
                "; the records found are partial."
            )
        if hints and not records:
            presence["near_misses"] = "Not exact matches, but worth checking: " + hints.sentence() + "."
        control.file_finished(of.size)
        return SideScan(records, presence, [f"File {name}: {text}" for text in file_warnings],
                        interrupted=reason in ("cancelled", "time_limit"))
