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
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, BinaryIO, Iterator

from .config import Config
from .csvscan import CsvParseError, Record, ScanControl, iter_records, prefilter_needle
from .process_maps import Catalog, CsvFormat, FolderRef, ProcessMap, load_catalog

MATCH_TYPE = "exact: whole field value, case-sensitive, compared as text"
MAX_IDENTIFIER_CHARS = 500
ENVELOPE_RESERVE_BYTES = 3_500
RECORD_NUMBER_NOTE = "record_number is 1-based and counts the header as record 1"

STOP_EXPLANATIONS = {
    "end_of_file": "the scan reached the end of the file",
    "row_limit": "the row limit for one response was reached",
    "output_size_limit": "the output-size limit for one response was reached",
    "time_limit": "the scan time limit was reached before the end of the file",
    "cancelled": "the scan was cancelled before the end of the file",
    "too_many_matches": "more matching records were found than one comparison can hold",
}


class ToolFailure(Exception):
    """A request that cannot be answered; becomes an `error` in the response."""

    def __init__(self, kind: str, message: str, **details: Any):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.details = details


def _size(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds")


@dataclass
class Header:
    columns: list[str]  # original names, original order
    labels: list[str]  # unique labels used as keys in returned rows
    data_offset: int  # byte offset of the first data record

    @property
    def duplicates(self) -> dict[str, list[int]]:
        positions: dict[str, list[int]] = {}
        for index, name in enumerate(self.columns):
            positions.setdefault(name, []).append(index + 1)
        return {name: where for name, where in positions.items() if len(where) > 1}


@dataclass
class OpenFile:
    ref: FolderRef
    path: Path
    name: str
    fmt: CsvFormat
    size: int
    mtime_ns: int

    def info(self) -> dict[str, Any]:
        return {
            "folder": self.ref.path,
            "filename": self.name,
            "size_bytes": self.size,
            "filesystem_modified_time": _iso(self.mtime_ns / 1e9),
        }


class _NearMisses:
    """Values equal to the identifier only after trimming whitespace: not matches, but worth knowing."""

    def __init__(self) -> None:
        self.count = 0
        self.examples: list[dict[str, Any]] = []

    def add(self, record: Record, label: str, value: str) -> None:
        self.count += 1
        if len(self.examples) < 3:
            self.examples.append({"record_number": record.number, "column": label, "value": value[:200]})

    def warning(self) -> str | None:
        if not self.count:
            return None
        return (
            f"{self.count} record(s) seen so far hold the identifier with surrounding whitespace. These are NOT "
            f"exact matches and are not included in the results. Examples: {json.dumps(self.examples, ensure_ascii=False)}"
        )


class Service:
    def __init__(self, config: Config):
        self.config = config
        self.limits = config.limits
        self._cursor_key = secrets.token_bytes(32)

    # ------------------------------------------------------------------ process maps

    def catalog(self) -> Catalog:
        return load_catalog(self.config.process_maps_dir, self.config.data_root)

    def _process(self, catalog: Catalog, name_or_id: str) -> ProcessMap:
        process = catalog.find_process(name_or_id)
        if process is None:
            raise ToolFailure(
                "process_not_found",
                f"No process map named or identified as '{name_or_id}'.",
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
                raise ToolFailure("step_not_found", f"Process '{proc.id}' has no step '{step_id}'.",
                                  available_steps=[s.id for s in proc.steps])
            touched = set(step.get("reads", [])) | set(step.get("writes", []))
            return {"process_id": proc.id, "process_name": proc.name, "step": step,
                    "folders": [f for f in full["folders"] if f["id"] in touched]}
        result = {"process": full}
        if _size(result) <= self.limits.max_output_bytes - ENVELOPE_RESERVE_BYTES:
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
        budget = self.limits.max_output_bytes - ENVELOPE_RESERVE_BYTES
        folders: list[dict[str, Any]] = []
        used = 0
        next_offset = None
        for index in range(max(offset, 0), len(refs)):
            ref = refs[index]
            usages = []
            for owner, folder in ref.usages:
                steps = [
                    {"step_id": s.id, "step_name": s.name, "role": role}
                    for s in owner.steps
                    for role, ids in (("reads", s.reads), ("writes", s.writes))
                    if folder.id in ids
                ]
                usages.append({"process_id": owner.id, "folder_id": folder.id,
                               "description": folder.description, "steps": steps})
            entry: dict[str, Any] = {
                "folder": ref.path,
                "description": ref.primary.description,
                "exists_on_disk": ref.resolved.is_dir(),
                "used_by": usages,
            }
            convention = ref.filename_convention
            if convention:
                entry["filename_pattern"] = convention.pattern
            identifiers = [c.column for _, f in ref.usages for c in f.identifier_columns]
            if identifiers:
                entry["identifier_columns"] = list(dict.fromkeys(identifiers))
            cost = _size(entry)
            if folders and used + cost > budget:
                next_offset = index
                break
            folders.append(entry)
            used += cost
        out: dict[str, Any] = {
            "scope": f"process '{proc.id}'" if proc else "all processes",
            "total_folders": len(refs),
            "folders_returned": len(folders),
            "folders": folders,
            "truncated": next_offset is not None,
            "note": "Pass a folder's 'folder' value to list_csv_files. Folder details (filename convention, "
                    "timestamp meanings, identifier columns) are in get_process_map.",
        }
        if next_offset is not None:
            out["next_offset"] = next_offset
        if catalog.load_errors:
            out["process_map_load_errors"] = catalog.load_errors
        return out

    # ------------------------------------------------------------------ files and headers

    def _folder(self, folder: str) -> FolderRef:
        catalog = self.catalog()
        ref = catalog.find_folder(folder) if folder and folder.strip() else None
        if ref is None:
            known = [r.path for r in catalog.folders()]
            raise ToolFailure(
                "folder_not_found",
                f"'{folder}' is not a folder described by any process map. Only mapped folders can be read.",
                closest_known_folders=difflib.get_close_matches(folder or "", known, n=5, cutoff=0.4),
                hint="Call list_folders for the full inventory.",
            )
        return ref

    def _open(self, folder: str, filename: str) -> OpenFile:
        ref = self._folder(folder)
        if not filename or filename in (".", "..") or "/" in filename or "\\" in filename or "\x00" in filename:
            raise ToolFailure("invalid_argument", "filename must be a bare file name inside the folder, without any path.")
        path = ref.resolved / filename
        try:
            st = os.stat(path)
        except FileNotFoundError:
            try:
                names = [e.name for e in os.scandir(ref.resolved)]
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
            raise ToolFailure("file_unreadable", f"'{filename}' is not a regular file.")
        if Path(os.path.realpath(path)).parent != ref.resolved:
            raise ToolFailure("file_unreadable", f"'{filename}' resolves to a location outside folder '{ref.path}'.")
        return OpenFile(ref, path, filename, ref.primary.csv_format, st.st_size, st.st_mtime_ns)

    def _fh(self, of: OpenFile) -> BinaryIO:
        try:
            return open(of.path, "rb")  # read-only: this is the only way files are ever opened
        except OSError as exc:
            raise ToolFailure("file_unreadable", f"Cannot open '{of.name}' for reading: {exc.strerror or exc}.") from exc

    def _header(self, fh: BinaryIO, of: OpenFile) -> Header:
        try:
            first = next(
                iter_records(fh, delimiter=of.fmt.delimiter, encoding=of.fmt.encoding,
                             chunk_bytes=64 * 1024, max_record_bytes=self.limits.max_record_bytes),
                None,
            )
        except CsvParseError as exc:
            raise ToolFailure("parse_error", f"Cannot read the header row of '{of.name}': {exc}") from exc
        except OSError as exc:
            raise ToolFailure("file_unreadable", f"Reading '{of.name}' failed: {exc.strerror or exc}.") from exc
        if first is None or not first.fields:
            raise ToolFailure("parse_error", f"'{of.name}' is empty or its first line is blank, so it has no header row.")
        seen: dict[str, int] = {}
        for name in first.fields:
            seen[name] = seen.get(name, 0) + 1
        labels = [name if seen[name] == 1 else f"{name} [col {i + 1}]" for i, name in enumerate(first.fields)]
        return Header(first.fields, labels, first.end)

    def _column(self, header: Header, spec: str, of: OpenFile) -> int:
        """Resolve a column given by exact name, by unique label, or as '#<position>'."""
        matches = [i for i, name in enumerate(header.columns) if name == spec]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ToolFailure(
                "ambiguous_column",
                f"Column name '{spec}' appears {len(matches)} times in '{of.name}' (positions "
                f"{[m + 1 for m in matches]}). Select one by position, e.g. '#{matches[0] + 1}'.",
            )
        if spec in header.labels:
            return header.labels.index(spec)
        if spec.startswith("#") and spec[1:].isdigit() and 1 <= int(spec[1:]) <= len(header.columns):
            return int(spec[1:]) - 1
        folded = [name for name in header.columns if name.strip().casefold() == spec.strip().casefold()]
        raise ToolFailure(
            "column_not_found",
            f"'{of.name}' has no column named '{spec}' (column names are matched exactly).",
            similar_column_names=list(dict.fromkeys(
                folded + difflib.get_close_matches(spec, header.columns, n=5, cutoff=0.6))),
            hint="Use search_column_names or inspect_csv_headers to find the right column.",
        )

    def list_csv_files(self, folder: str, filename_contains: str | None = None, date_from: str | None = None,
                       date_to: str | None = None, date_field: str | None = None, offset: int = 0,
                       newest_first: bool = True) -> dict[str, Any]:
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
                if not entry.name.lower().endswith(".csv") or not entry.is_file():
                    ignored += 1
                    continue
                st = entry.stat()
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
        files.sort(key=lambda f: f["filename"], reverse=newest_first)

        budget = self.limits.max_output_bytes - ENVELOPE_RESERVE_BYTES
        page: list[dict[str, Any]] = []
        used = 0
        start = max(offset, 0)
        for item in files[start: start + self.limits.max_files_per_listing]:
            cost = _size(item)
            if page and used + cost > budget:
                break
            page.append(item)
            used += cost
        more = start + len(page) < len(files)
        out: dict[str, Any] = {
            "folder": ref.path,
            "filters": {"filename_contains": filename_contains, "date_from": lo, "date_to": hi, "date_field": field},
            "total_matching_files": len(files),
            "files_returned": len(page),
            "files": page,
            "truncated": more,
            "sorted_by": "filename, " + ("descending" if newest_first else "ascending"),
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
            out["non_csv_entries_ignored"] = ignored
        if unparseable:
            out["files_excluded_because_filename_has_no_parseable_date"] = unparseable
        return out

    @staticmethod
    def _date_bound(value: str | None, name: str) -> str | None:
        if not value:
            return None
        try:
            return date.fromisoformat(value.strip()[:10]).isoformat()
        except ValueError:
            raise ToolFailure("invalid_argument", f"{name} must be an ISO date (YYYY-MM-DD), got '{value}'.") from None

    def inspect_csv_headers(self, folder: str, filename: str, sample_rows: int = 0, column_offset: int = 0,
                            sample_columns: list[str] | None = None) -> dict[str, Any]:
        of = self._open(folder, filename)
        if sample_rows < 0 or sample_rows > self.limits.max_sample_rows:
            raise ToolFailure("invalid_argument", f"sample_rows must be between 0 and {self.limits.max_sample_rows}.")
        with self._fh(of) as fh:
            header = self._header(fh, of)
            budget = self.limits.max_output_bytes - ENVELOPE_RESERVE_BYTES
            start = min(max(column_offset, 0), len(header.columns))
            columns: list[str] = []
            used = 0
            for name in header.columns[start:]:
                cost = _size(name) + 2
                if columns and used + cost > budget // 2:
                    break
                columns.append(name)
                used += cost
            out: dict[str, Any] = {
                "file": of.info(),
                "column_count": len(header.columns),
                "columns": columns,
                "columns_truncated": start + len(columns) < len(header.columns),
            }
            if start:
                out["column_offset"] = start
            if out["columns_truncated"]:
                out["next_column_offset"] = start + len(columns)
                out["truncation_note"] = "Not all column names fit. Continue with column_offset, or use search_column_names."
            if header.duplicates:
                out["duplicate_column_names"] = header.duplicates
                out["duplicate_note"] = "Rows label repeated names as 'name [col N]'; select them as '#N'."
            if sample_rows:
                wanted = [self._column(header, c, of) for c in sample_columns] if sample_columns else None
                rows: list[dict[str, Any]] = []
                room = budget - used
                truncated = False
                try:
                    for record in iter_records(fh, delimiter=of.fmt.delimiter, encoding=of.fmt.encoding,
                                               start_offset=header.data_offset, start_record=1,
                                               chunk_bytes=64 * 1024, max_record_bytes=self.limits.max_record_bytes):
                        if len(rows) >= sample_rows:
                            break
                        if not record.fields:
                            continue
                        row = self._fit(self._row(header, record, wanted), room)
                        truncated = truncated or "values_omitted_for_size" in row
                        room = max(room - _size(row), 200)
                        rows.append(row)
                except CsvParseError as exc:
                    out["sample_error"] = f"Sampling stopped: {exc}"
                out["sample_rows"] = rows
                out["sample_note"] = (
                    f"The first {len(rows)} data record(s) only: a layout sample, not evidence about any security. "
                    + RECORD_NUMBER_NOTE + "."
                )
                if truncated:
                    out["sample_truncated"] = True
        return out

    def search_column_names(self, folder: str, filename: str, term: str) -> dict[str, Any]:
        of = self._open(folder, filename)
        if not term or not term.strip():
            raise ToolFailure("invalid_argument", "term must not be empty.")
        with self._fh(of) as fh:
            header = self._header(fh, of)
        wanted = term.strip().casefold()
        hits = [{"column": name, "position": i + 1} for i, name in enumerate(header.columns) if wanted in name.casefold()]
        budget = self.limits.max_output_bytes - ENVELOPE_RESERVE_BYTES
        page: list[dict[str, Any]] = []
        used = 0
        for hit in hits:
            used += _size(hit) + 2
            if page and used > budget:
                break
            page.append(hit)
        out: dict[str, Any] = {
            "file": of.info(),
            "term": term,
            "match_type": "case-insensitive substring of the column name",
            "column_count": len(header.columns),
            "matching_column_count": len(hits),
            "matching_columns": page,
            "truncated": len(page) < len(hits),
        }
        if not hits:
            out["similar_column_names"] = difflib.get_close_matches(term, header.columns, n=5, cutoff=0.5)
        return out

    # ------------------------------------------------------------------ row formatting

    def _row(self, header: Header, record: Record, wanted: list[int] | None,
             matched: list[int] | None = None) -> dict[str, Any]:
        fields = record.fields
        indices = list(dict.fromkeys([*wanted, *(matched or [])])) if wanted is not None else range(len(fields))
        values: dict[str, str] = {}
        cut: list[str] = []
        limit = self.limits.max_cell_chars
        for i in indices:
            if i >= len(fields):
                continue
            label = header.labels[i] if i < len(header.labels) else f"[extra field {i + 1}]"
            value = fields[i]
            if len(value) > limit:
                cut.append(label)
                value = f"{value[:limit]}...[cell truncated; {len(value)} characters in total]"
            values[label] = value
        row: dict[str, Any] = {"record_number": record.number, "byte_offset": record.offset}
        if matched is not None:
            row["matched_columns"] = [header.labels[i] if i < len(header.labels) else f"[extra field {i + 1}]" for i in matched]
        row["values"] = values
        if cut:
            row["truncated_cells"] = cut
        if len(fields) != len(header.columns):
            row["field_count_mismatch"] = {"header_columns": len(header.columns), "record_fields": len(fields)}
        return row

    @staticmethod
    def _fit(row: dict[str, Any], budget: int) -> dict[str, Any]:
        """Drop trailing values so a single over-wide row still fits; say so in the row."""
        if _size(row) <= budget:
            return row
        values = row["values"]
        kept: dict[str, str] = {}
        used = _size({k: v for k, v in row.items() if k != "values"}) + 200
        for label, value in values.items():
            used += _size(label) + _size(value) + 4
            if used > budget:
                break
            kept[label] = value
        row["values"] = kept
        row["values_omitted_for_size"] = len(values) - len(kept)
        row["size_note"] = ("This record is too wide for the output-size limit; trailing columns were left out. "
                            "Request specific columns to see them.")
        return row

    # ------------------------------------------------------------------ cursors

    def _fingerprint(self, tool: str, of: OpenFile, identifier: str, column: int | None) -> str:
        blob = json.dumps([tool, str(of.path), of.size, of.mtime_ns, identifier, column])
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
        except Exception:
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

    def _iter_matches(self, fh: BinaryIO, of: OpenFile, header: Header, identifier: str, column: int | None,
                      start_offset: int, start_record: int, control: ScanControl,
                      near: _NearMisses) -> Iterator[tuple[Record, list[int]]]:
        """Yield (record, indices of matching fields) for exact matches, in file order."""
        trimmed = identifier.strip()
        labels = header.labels
        for record in iter_records(
            fh, delimiter=of.fmt.delimiter, encoding=of.fmt.encoding,
            needle=prefilter_needle(identifier, of.fmt.encoding),
            start_offset=start_offset, start_record=start_record, control=control,
            chunk_bytes=self.limits.chunk_bytes, max_record_bytes=self.limits.max_record_bytes,
        ):
            fields = record.fields
            if column is not None:
                if column < len(fields):
                    value = fields[column]
                    if value == identifier:
                        control.matches += 1
                        yield record, [column]
                    elif value.strip() == trimmed:
                        near.add(record, labels[column], value)
                continue
            hits = [i for i, value in enumerate(fields) if value == identifier]
            if hits:
                control.matches += 1
                yield record, hits
            else:
                for i, value in enumerate(fields):
                    if identifier in value and value.strip() == trimmed:
                        near.add(record, labels[i] if i < len(labels) else f"[extra field {i + 1}]", value)

    def _search(self, tool: str, control: ScanControl, folder: str, filename: str, identifier: str,
                column: str | None, columns: list[str] | None, max_rows: int | None,
                cursor: str | None, *, count_only: bool = False) -> dict[str, Any]:
        """Shared implementation of find_security_in_file, search_across_columns and count_matching_rows."""
        control.begin()
        control.phase = "opening"
        self._check_identifier(identifier)
        row_limit = self.limits.max_rows_per_response
        if max_rows is not None:
            if max_rows < 1:
                raise ToolFailure("invalid_argument", "max_rows must be at least 1.")
            row_limit = min(row_limit, max_rows)
        of = self._open(folder, filename)
        search: dict[str, Any] = {"identifier": identifier, "match_type": MATCH_TYPE}
        result: dict[str, Any] = {"file": of.info(), "search": search}
        warnings: list[str] = []
        with self._fh(of) as fh:
            header = self._header(fh, of)
            column_index = None
            if tool != "search_across_columns":
                column_index = self._column(header, column or "", of)
                search["column"] = header.labels[column_index]
            else:
                search["columns_searched"] = f"all {len(header.columns)} columns"
            wanted = None
            if columns and not count_only:
                wanted = [self._column(header, c, of) for c in columns]
                search["columns_returned"] = [header.labels[i] for i in wanted]
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

            control.total_bytes = of.size
            control.phase = "scanning"
            near = _NearMisses()
            rows: list[dict[str, Any]] = []
            found = 0
            budget = self.limits.max_output_bytes - ENVELOPE_RESERVE_BYTES
            used = 0
            stop_reason: str | None = None
            resume: tuple[int, int] | None = None
            error: ToolFailure | None = None
            try:
                for record, hits in self._iter_matches(fh, of, header, identifier, column_index,
                                                       start_offset, start_record, control, near):
                    if count_only:
                        found += 1
                        continue
                    if len(rows) >= row_limit:
                        stop_reason, resume = "row_limit", (record.offset, record.number - 1)
                        break
                    row = self._row(header, record, wanted, hits if column_index is None else None)
                    cost = _size(row)
                    if used + cost > budget:
                        if rows:
                            stop_reason, resume = "output_size_limit", (record.offset, record.number - 1)
                            break
                        row = self._fit(row, budget)
                        cost = _size(row)
                    rows.append(row)
                    used += cost
                    found += 1
            except CsvParseError as exc:
                error = ToolFailure("parse_error", str(exc), record_number=exc.record_number, byte_offset=exc.byte_offset)
            except OSError as exc:
                error = ToolFailure("file_unreadable", f"Reading '{of.name}' failed part-way: {exc.strerror or exc}.")
            if stop_reason is None and error is None:
                stop_reason = control.stop_reason
                resume = (control.resume_offset, control.resume_record)

            try:
                st = os.stat(of.path)
                changed = (st.st_size, st.st_mtime_ns) != (of.size, of.mtime_ns)
            except OSError:
                changed = True

        reached_end = stop_reason == "end_of_file"
        total = before + found
        if changed:
            warnings.append("The file was modified while it was being scanned (it may still be being written), "
                            "so the scan may have missed or misread records.")
        if control.unterminated_quote_at_eof:
            warnings.append("The file ends inside a quoted field (unterminated quote): the final record is "
                            "incomplete and records may be hidden inside it.")
        if near.warning():
            warnings.append(near.warning())
        trustworthy = reached_end and not changed and not control.unterminated_quote_at_eof

        result["scan"] = {
            "complete": reached_end,
            "stop_reason": "error" if error else stop_reason,
            "started_at_byte": start_offset,
            "stopped_at_byte": control.bytes_done if resume is None else resume[0],
            "file_size_bytes": of.size,
            "elapsed_seconds": round(control.elapsed, 2),
        }
        if cursor and reached_end:
            result["scan"]["note"] = "Together with the earlier responses in this cursor chain, the whole file has been scanned."

        where = f"column '{search['column']}'" if column_index is not None else "any column"
        if total:
            outcome = "match_found"
        elif trustworthy:
            outcome = "confirmed_absent"
        else:
            outcome = "inconclusive"
        result["outcome"] = outcome

        can_resume = error is None and not reached_end and resume is not None and not changed
        next_cursor = self._encode_cursor({"fp": fingerprint, "o": resume[0], "r": resume[1], "m": total}) if can_resume else None
        why = error.message if error else STOP_EXPLANATIONS.get(stop_reason or "", "the scan stopped early")

        if count_only:
            exact = trustworthy and error is None
            result["count"] = total if exact else None
            result["count_is_exact"] = exact
            if exact:
                result["explanation"] = f"Complete scan: exactly {total} record(s) have '{identifier}' in {where}."
            else:
                result["matches_seen_before_interruption"] = total
                result["explanation"] = (
                    f"A complete count is NOT available because {why if not reached_end else 'the file could not be read reliably (see warnings)'}. "
                    f"{total} matching record(s) were seen in the part scanned; this is a lower bound, not the count."
                    + (" Pass next_cursor to continue counting from where this scan stopped." if next_cursor else "")
                )
        else:
            more = "none" if reached_end and error is None else ("yes" if stop_reason in ("row_limit", "output_size_limit") else "unknown")
            result["matches_returned"] = len(rows)
            result["matches"] = rows
            result["more_matches"] = more
            result["results_are_partial"] = more != "none"
            if outcome == "match_found":
                if more == "none":
                    text = (f"Complete scan: all matching records have been returned ({total} in total"
                            + (", including earlier responses" if before else "") + ").")
                elif more == "yes":
                    text = (f"PARTIAL RESULTS: {len(rows)} matching record(s) returned and at least one more exists, "
                            f"because {why}. Pass next_cursor to retrieve further matches.")
                else:
                    text = (f"PARTIAL RESULTS: the scan is incomplete because {why}. The {len(rows)} record(s) returned "
                            "are only the matches in the part scanned; there may be more."
                            + (" Pass next_cursor to continue." if next_cursor else ""))
            elif outcome == "confirmed_absent":
                text = f"Complete scan: no record has '{identifier}' as the exact value of {where}."
            else:
                text = (f"INCONCLUSIVE: no match was found, but absence is not confirmed because "
                        f"{why if not reached_end else 'the file could not be read reliably (see warnings)'}."
                        + (" Pass next_cursor to continue the scan." if next_cursor else ""))
            result["explanation"] = text
            if rows:
                result["record_number_note"] = RECORD_NUMBER_NOTE
        result["next_cursor"] = next_cursor
        if error:
            result["error"] = {"type": error.kind, "message": error.message, **error.details}
        if warnings:
            result["warnings"] = warnings
        if header.duplicates and not count_only:
            result["duplicate_column_names"] = header.duplicates
        return result

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

    # ------------------------------------------------------------------ comparison

    def compare_security_records(self, control: ScanControl, folder_a: str, filename_a: str, folder_b: str,
                                 filename_b: str, identifier: str, identifier_column_a: str,
                                 identifier_column_b: str | None = None, identifier_b: str | None = None,
                                 compare_columns: list[str] | None = None,
                                 column_mapping: dict[str, str] | None = None,
                                 match_keys: list[str] | None = None) -> dict[str, Any]:
        control.begin()
        control.phase = "opening"
        mapping = column_mapping or {}
        identifier_b = identifier if identifier_b is None else identifier_b
        self._check_identifier(identifier)
        self._check_identifier(identifier_b)
        sides = {"a": self._open(folder_a, filename_a), "b": self._open(folder_b, filename_b)}
        result: dict[str, Any] = {"file_a": sides["a"].info(), "file_b": sides["b"].info()}
        with self._fh(sides["a"]) as fh_a, self._fh(sides["b"]) as fh_b:
            handles = {"a": fh_a, "b": fh_b}
            headers = {key: self._header(handles[key], of) for key, of in sides.items()}
            id_cols = {
                "a": self._column(headers["a"], identifier_column_a, sides["a"]),
                "b": self._column(headers["b"], identifier_column_b or mapping.get(identifier_column_a, identifier_column_a), sides["b"]),
            }

            def pair(name_a: str) -> tuple[int, int]:
                return (self._column(headers["a"], name_a, sides["a"]),
                        self._column(headers["b"], mapping.get(name_a, name_a), sides["b"]))

            only_a: list[str] = []
            if compare_columns:
                pairs = [pair(name) for name in compare_columns]
            else:
                # Default: every column of A that has a counterpart in B (mapped, or same name).
                pairs = []
                names_b = set(headers["b"].columns) | set(headers["b"].labels)
                for label in headers["a"].labels:
                    if mapping.get(label, label) in names_b:
                        pairs.append(pair(label))
                    else:
                        only_a.append(label)
            keys = [pair(name) for name in match_keys] if match_keys else []
            label_a, label_b = headers["a"].labels, headers["b"].labels
            used_b = {b for _, b in pairs}
            search = {
                "identifier_a": identifier, "identifier_column_a": label_a[id_cols["a"]],
                "identifier_b": identifier_b, "identifier_column_b": label_b[id_cols["b"]],
                "match_type": MATCH_TYPE,
                "compared_columns": "all columns of file A with a counterpart in file B" if not compare_columns
                else [{"a": label_a[a], "b": label_b[b]} for a, b in pairs],
                "match_keys": [{"a": label_a[a], "b": label_b[b]} for a, b in keys],
            }
            result["search"] = search
            if not compare_columns:
                result["columns_not_compared"] = {
                    "only_in_file_a": only_a[:100], "only_in_file_a_count": len(only_a),
                    "only_in_file_b": [l for i, l in enumerate(label_b) if i not in used_b][:100],
                    "only_in_file_b_count": len(label_b) - len(used_b),
                    "note": "Columns are paired by identical name unless column_mapping says otherwise.",
                }

            # Find the security in each file. Both scans share one time budget.
            cap = self.limits.max_rows_per_response
            control.total_bytes = sides["a"].size + sides["b"].size
            records: dict[str, list[Record]] = {"a": [], "b": []}
            presence: dict[str, dict[str, Any]] = {}
            warnings: list[str] = []
            base = 0
            for key, ident in (("a", identifier), ("b", identifier_b)):
                of, header = sides[key], headers[key]
                control.phase = f"scanning file {key.upper()}"
                control.stop_reason = None
                control.unterminated_quote_at_eof = False
                near = _NearMisses()
                reason: str | None = None
                problem: str | None = None
                try:
                    for record, _ in self._iter_matches(handles[key], of, header, ident, id_cols[key],
                                                        header.data_offset, 1, control, near):
                        if len(records[key]) >= cap:
                            reason = "too_many_matches"
                            break
                        records[key].append(record)
                except CsvParseError as exc:
                    reason, problem = "error", str(exc)
                except OSError as exc:
                    reason, problem = "error", f"reading failed part-way: {exc.strerror or exc}"
                reason = reason or control.stop_reason
                try:
                    st = os.stat(of.path)
                    changed = (st.st_size, st.st_mtime_ns) != (of.size, of.mtime_ns)
                except OSError:
                    changed = True
                complete = reason == "end_of_file" and not changed and not control.unterminated_quote_at_eof
                for text in (
                    "the file was modified while it was being scanned" if changed else None,
                    "the file ends inside a quoted field (unterminated quote)" if control.unterminated_quote_at_eof else None,
                    near.warning(),
                ):
                    if text:
                        warnings.append(f"File {key.upper()}: {text}")
                presence[key] = {
                    "outcome": "match_found" if records[key] else ("confirmed_absent" if complete else "inconclusive"),
                    "matching_records_found": len(records[key]),
                    "scan_complete": complete,
                    "stop_reason": reason,
                }
                if not complete:
                    presence[key]["explanation"] = (
                        f"The scan of file {key.upper()} is incomplete because "
                        f"{problem or STOP_EXPLANATIONS.get(reason or '', 'the file could not be read reliably')}; "
                        "the records found are partial."
                    )
                base += of.size
                control.bytes_base = base
                control.bytes_done = 0
                if reason in ("cancelled", "time_limit"):
                    if key == "a":
                        presence["b"] = {"outcome": "inconclusive", "matching_records_found": 0, "scan_complete": False,
                                         "stop_reason": "not_scanned",
                                         "explanation": "File B was not scanned because the scan of file A was interrupted."}
                    break

        result["presence"] = {"file_a": presence["a"], "file_b": presence["b"]}
        result["elapsed_seconds"] = round(control.elapsed, 2)
        if warnings:
            result["warnings"] = warnings
        recs_a, recs_b = records["a"], records["b"]
        head_a, head_b = headers["a"], headers["b"]
        budget = [self.limits.max_output_bytes - ENVELOPE_RESERVE_BYTES - _size(result)]
        limit = self.limits.max_cell_chars

        def clip(value: str) -> str:
            return value if len(value) <= limit else f"{value[:limit]}...[cell truncated; {len(value)} characters in total]"

        def cell(record: Record, index: int) -> str | None:
            return record.fields[index] if index < len(record.fields) else None

        def summary(record: Record, header: Header, indices: list[int]) -> dict[str, Any]:
            row = self._fit(self._row(header, record, indices), max(budget[0] // 4, 400))
            budget[0] -= _size(row)
            return row

        def varying(recs: list[Record], header: Header) -> list[str]:
            out = []
            for i, label in enumerate(header.labels):
                if len({cell(r, i) for r in recs}) == len(recs):
                    out.append(label)
            return out[:20]

        def compare_pair(rec_a: Record, rec_b: Record) -> dict[str, Any]:
            changed, unchanged, omitted = [], [], []
            for a, b in pairs:
                va, vb = cell(rec_a, a), cell(rec_b, b)
                entry: dict[str, Any] = {"column_a": label_a[a]}
                if label_b[b] != label_a[a]:
                    entry["column_b"] = label_b[b]
                if va == vb:
                    entry["value"] = None if va is None else clip(va)
                    unchanged.append(entry)
                    continue
                entry["value_a"] = None if va is None else clip(va)
                entry["value_b"] = None if vb is None else clip(vb)
                note = _difference_note(va, vb)
                if note:
                    entry["note"] = note
                changed.append(entry)
            out: dict[str, Any] = {
                "record_a": {"record_number": rec_a.number, "byte_offset": rec_a.offset},
                "record_b": {"record_number": rec_b.number, "byte_offset": rec_b.offset},
                "changed_count": len(changed), "unchanged_count": len(unchanged),
                "changed": [], "unchanged": [],
            }
            for source, target in ((changed, "changed"), (unchanged, "unchanged")):
                for entry in source:
                    cost = _size(entry) + 2
                    if cost > budget[0]:
                        omitted.append(entry["column_a"])
                        continue
                    budget[0] -= cost
                    out[target].append(entry)
            if omitted:
                out["truncated"] = True
                out["columns_omitted_for_size"] = omitted[:200]
                out["truncation_note"] = ("Not every compared value fits the output-size limit. The counts are complete; "
                                          "request the omitted columns with compare_columns.")
            return out

        incomplete = not (presence["a"]["scan_complete"] and presence["b"]["scan_complete"])
        shown_a = [a for a, _ in pairs][:50]
        shown_b = [b for _, b in pairs][:50]
        if incomplete:
            status = "inconclusive"
            text = ("INCONCLUSIVE: at least one file was not scanned completely, so presence and record alignment "
                    "cannot be established. Any record counts above are partial. No comparison was made.")
        elif not recs_a and not recs_b:
            status, text = "absent_from_both", "Complete scans: the security is in neither file."
        elif not recs_a or not recs_b:
            status = "present_in_one_file_only"
            there, missing = ("A", "B") if recs_a else ("B", "A")
            text = f"Complete scans: the security is in file {there} ({len(recs_a or recs_b)} record(s)) and absent from file {missing}."
            key = "records_in_file_a" if recs_a else "records_in_file_b"
            result[key] = [summary(r, head_a if recs_a else head_b, shown_a if recs_a else shown_b) for r in (recs_a or recs_b)]
        elif not keys and (len(recs_a) > 1 or len(recs_b) > 1):
            status = "ambiguous_alignment"
            text = (f"AMBIGUOUS: file A has {len(recs_a)} matching record(s) and file B has {len(recs_b)}, and no match_keys "
                    "were given, so records cannot be paired reliably. No comparison was made. Supply match_keys that "
                    "identify one record per file (see columns_that_differ_between_matches), or compare specific records "
                    "using find_security_in_file.")
            result["columns_that_differ_between_matches"] = {"file_a": varying(recs_a, head_a), "file_b": varying(recs_b, head_b)}
            result["records_in_file_a"] = [summary(r, head_a, shown_a) for r in recs_a]
            result["records_in_file_b"] = [summary(r, head_b, shown_b) for r in recs_b]
        else:
            groups_a: dict[tuple, list[Record]] = {}
            groups_b: dict[tuple, list[Record]] = {}
            for rec in recs_a:
                groups_a.setdefault(tuple(cell(rec, a) for a, _ in keys), []).append(rec)
            for rec in recs_b:
                groups_b.setdefault(tuple(cell(rec, b) for _, b in keys), []).append(rec)
            comparisons, ambiguous, unmatched_a, unmatched_b = [], [], [], []
            for key_values, group in groups_a.items():
                other = groups_b.get(key_values)
                described = {label_a[a]: v for (a, _), v in zip(keys, key_values)}
                if other is None:
                    unmatched_a.extend({"record_number": r.number, "key": described} for r in group)
                elif len(group) > 1 or len(other) > 1:
                    ambiguous.append({"key": described, "records_in_file_a": [r.number for r in group],
                                      "records_in_file_b": [r.number for r in other]})
                else:
                    entry = compare_pair(group[0], other[0])
                    if keys:
                        entry = {"key": described, **entry}
                    comparisons.append(entry)
            for key_values, group in groups_b.items():
                if key_values not in groups_a:
                    described = {label_b[b]: v for (_, b), v in zip(keys, key_values)}
                    unmatched_b.extend({"record_number": r.number, "key": described} for r in group)
            result["comparisons"] = comparisons
            if ambiguous:
                result["ambiguous_keys"] = ambiguous
            if unmatched_a:
                result["unmatched_records_in_file_a"] = unmatched_a
            if unmatched_b:
                result["unmatched_records_in_file_b"] = unmatched_b
            if ambiguous and not comparisons:
                status = "ambiguous_alignment"
                text = ("AMBIGUOUS: the match_keys do not identify a single record in each file (see ambiguous_keys), "
                        "so no records could be paired. Add further match_keys.")
            elif ambiguous or unmatched_a or unmatched_b:
                status = "partially_compared"
                text = (f"{len(comparisons)} record pair(s) compared. Some records could not be paired: "
                        f"{len(ambiguous)} ambiguous key(s), {len(unmatched_a)} record(s) only in file A, "
                        f"{len(unmatched_b)} only in file B.")
            else:
                status = "compared"
                differing = sum(1 for c in comparisons if c["changed_count"])
                text = (f"Complete scans: {len(comparisons)} record pair(s) compared; {differing} pair(s) have at "
                        "least one differing value.")
        result["comparison_status"] = status
        result["explanation"] = text
        result["record_number_note"] = RECORD_NUMBER_NOTE
        return result


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
