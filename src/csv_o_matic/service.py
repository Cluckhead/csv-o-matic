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
import threading
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
MAX_IDENTIFIER_CHARS = 200
MAX_MATCH_KEYS = 10
MAX_FILTERS = 10
MAX_FOLDER_CHARS = 1024
MAX_SUBFOLDER_DEPTH = 32
SUBFOLDERS_IN_FILE_LISTING = 50
LABEL_MAX_CHARS = 200  # longer column names are shown shortened
ENVELOPE_RESERVE_BYTES = 3_500  # for listings: room for everything in a response other than the items
SCAN_RESERVE_BYTES = 2_800  # for searches: room for scan status, explanation, cursor and warnings
TOOL_NAME_BYTES = 60
ALTERNATIVE_ENCODINGS = ("utf-8", "cp1252", "latin-1")

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
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8", "replace"))


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}...[{len(text)} characters]"


def _some(names: list[str], max_bytes: int) -> list[str]:
    """As many leading names as fit in `max_bytes`; callers report the full count separately."""
    out: list[str] = []
    used = 2
    for name in names:
        used += _size(name) + 2
        if used > max_bytes:
            break
        out.append(name)
    return out


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds")


@dataclass
class Header:
    columns: list[str]  # original names, original order
    labels: list[str]  # unique, bounded-length labels used as keys in returned rows
    data_offset: int  # byte offset of the first data record

    @classmethod
    def build(cls, columns: list[str], data_offset: int) -> "Header":
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
            examples[_clip(name, 80)] = {"occurrences": len(where), "first_positions": where[:8]}
        return {
            "repeated_names": len(repeated),
            "examples": examples,
            "note": "Rows label repeated names as 'name [col N]'; select one as '#N'.",
        }


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
            "mapped_root": self.ref.mapped_root,
            "filename": self.name,
            "size_bytes": self.size,
            "filesystem_modified_time": _iso(self.mtime_ns / 1e9),
        }


class _Hints:
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
        self.filtered = 0
        self.filtered_examples: list[dict[str, Any]] = []

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
            examples.append({"record_number": record.number, "column": _clip(label, 60), "value": _clip(value, 80)})

    def multi_line(self, record: Record, label: str, value: str) -> None:
        """The identifier inside a value that spans lines, in a column that was not searched."""
        self.spanning += 1
        if len(self.spanning_examples) < 2:
            at = value.find(self.identifier)
            self.spanning_examples.append({"record_number": record.number, "column": _clip(label, 60),
                                           "value_excerpt": value[max(at - 30, 0): at + len(self.identifier) + 30]})

    def filtered_out(self, record: Record, header: "Header", filters: list[tuple[int, str]]) -> None:
        """The identifier matched, but the same row does not satisfy every record-key filter."""
        self.filtered += 1
        if len(self.filtered_examples) < 3:
            fields = record.fields
            self.filtered_examples.append({
                "record_number": record.number,
                "values": {_clip(header.label(i), 60): (_clip(fields[i], 80) if i < len(fields) else None)
                           for i, _ in filters}})

    def other_column(self, label: str) -> None:
        """The exact identifier, in a column other than the one searched."""
        key = _clip(label, 60)
        if key in self.elsewhere or len(self.elsewhere) < 5:
            self.elsewhere[key] = self.elsewhere.get(key, 0) + 1

    def __bool__(self) -> bool:
        return bool(self.padded or self.longer or self.elsewhere or self.spanning or self.filtered)

    def sentence(self) -> str:
        parts = []
        if self.filtered:
            parts.append(f"{self.filtered} record(s) have the identifier but do not satisfy the filters")
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
        if self.filtered:
            out["identifier_matched_but_filters_did_not"] = {
                "count": self.filtered,
                "meaning": "Records where the identifier matched exactly but at least one filter column held "
                           "a different value. 'values' shows what the filter columns hold in those records."}
            if examples:
                out["identifier_matched_but_filters_did_not"]["examples"] = self.filtered_examples
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
                f"No process map named or identified as '{_clip(name_or_id, 200)}'.",
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
                raise ToolFailure("step_not_found", f"Process '{proc.id}' has no step '{_clip(step_id, 200)}'.",
                                  available_steps=[s.id for s in proc.steps])
            touched = set(step.get("reads", [])) | set(step.get("writes", []))
            detail: dict[str, Any] = {"process_id": proc.id, "process_name": proc.name, "step": step,
                                      "folders": [f for f in full["folders"] if f["id"] in touched]}
            room = self.limits.max_output_bytes - ENVELOPE_RESERVE_BYTES
            if _size(detail) > room:
                # Keep the step's shape and as much of each long list as fits, and say what was cut.
                detail["folders"] = [{k: f[k] for k in ("id", "path", "description")} for f in detail["folders"]]
                cut: dict[str, str] = {}
                lists = [k for k in ("field_mappings", "transformation_rules", "filter_rules") if step.get(k)]
                for key in lists:
                    items, step[key] = step[key], []
                    share = max((room - _size(detail)) // (len(lists) - lists.index(key)), 0)
                    used = 0
                    for item in items:
                        used += _size(item) + 2
                        if used > share:
                            break
                        step[key].append(item)
                    if len(step[key]) < len(items):
                        cut[key] = f"{len(step[key])} of {len(items)} shown"
                detail["truncated"] = True
                detail["truncation_note"] = ("This step is too large for one response; some lists are cut short "
                                             f"({cut}). The complete step is in the process map file itself.")
            return detail
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

    def _folder_metadata(self, ref: FolderRef) -> dict[str, Any]:
        """What the process maps say about a folder's contents; subfolders inherit it from their mapped root."""
        out: dict[str, Any] = {}
        convention = ref.filename_convention
        if convention:
            out["filename_pattern"] = convention.pattern
        identifiers: dict[str, str | None] = {}
        for _, folder in ref.usages:
            for column in folder.identifier_columns:
                identifiers.setdefault(column.column, column.identifier_type)
        if identifiers:
            out["identifier_columns"] = [
                {"column": name, "identifier_type": kind} if kind else {"column": name}
                for name, kind in identifiers.items()]
        if ref.record_key_columns:
            out["record_key_columns"] = ref.record_key_columns
        return out

    def _list_subfolders(self, catalog: Catalog, folder: str, offset: int) -> dict[str, Any]:
        """The immediate subfolders of one approved folder. Never recursive: one level per call."""
        ref = self._folder(folder, catalog)
        try:
            names, links = [], 0
            with os.scandir(ref.resolved) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        names.append(entry.name)
                    elif entry.is_symlink():
                        links += 1
        except OSError as exc:
            raise ToolFailure("folder_unreadable", f"Cannot list folder '{ref.path}': {exc.strerror or exc}.") from exc
        names.sort(reverse=True)
        roots = {str(r.resolved): r for r in catalog.folders()}
        out: dict[str, Any] = {
            "scope": f"subfolders of '{ref.path}'",
            "folder": ref.path,
            "kind": ref.kind,
            "mapped_root": ref.mapped_root,
        }
        if ref.root_path is not None:
            out["parent_folder"] = os.path.dirname(ref.path)
        out["mapped_root_description"] = _clip(ref.primary.description, 400)
        out.update(self._folder_metadata(ref))
        budget = max(self.limits.max_output_bytes - ENVELOPE_RESERVE_BYTES - _size(out), 400)
        folders: list[dict[str, Any]] = []
        used = 0
        start = max(offset, 0)
        for name in names[start: start + self.limits.max_files_per_listing]:
            mapped = roots.get(str(ref.resolved / name))
            if mapped is not None:  # mapped in its own right: its own process-map entry applies
                entry_out = {"folder": mapped.path, "kind": "mapped_root", "name": name, "mapped_root": mapped.path}
            else:
                entry_out = {"folder": os.path.join(ref.path, name), "kind": "subfolder", "name": name,
                             "mapped_root": ref.mapped_root}
            cost = _size(entry_out) + 2
            if folders and used + cost > budget:
                break
            folders.append(entry_out)
            used += cost
        more = start + len(folders) < len(names)
        out.update({
            "total_folders": len(names),
            "folders_returned": len(folders),
            "folders": folders,
            "truncated": more,
            "sorted_by": "name, descending",
            "note": "Only the immediate subfolders of this folder are listed; nothing is searched recursively. "
                    "Pass a 'folder' value to list_csv_files, or to list_folders again to go one level deeper. "
                    "Files in a subfolder are read using the mapped root's CSV format, filename convention and "
                    "identifier columns.",
        })
        if more:
            out["next_offset"] = start + len(folders)
        if links:
            out["symbolic_links_ignored"] = links
        return out

    def list_folders(self, process: str | None = None, offset: int = 0, folder: str | None = None) -> dict[str, Any]:
        catalog = self.catalog()
        if folder is not None and folder != "":
            if process:
                raise ToolFailure("invalid_argument", "Give either process (mapped folders of a process) or folder "
                                                      "(subfolders of one approved folder), not both.")
            return self._list_subfolders(catalog, folder, offset)
        proc = self._process(catalog, process) if process else None
        refs = sorted(catalog.folders(proc), key=lambda r: r.path)
        budget = self.limits.max_output_bytes - ENVELOPE_RESERVE_BYTES
        folders: list[dict[str, Any]] = []
        used = 0
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
            entry: dict[str, Any] = {"folder": ref.path, "kind": "mapped_root"}
            if len(descriptions) == 1:
                entry["description"] = descriptions[0]
            entry["exists_on_disk"] = ref.resolved.is_dir()
            entry["used_by"] = usages
            entry.update(self._folder_metadata(ref))
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
            "note": "Pass a folder's 'folder' value to list_csv_files. get_process_map has the rest: what each "
                    "filename token means, record keys, field mappings and filter rules. These are the mapped "
                    "(approved) folders; call list_folders with folder=<one of them> to see its subfolders. "
                    "record_key_columns are the columns normally used as filters to pin down one record.",
        }
        if next_offset is not None:
            out["next_offset"] = next_offset
        if catalog.load_errors:
            out["process_map_load_errors"] = catalog.load_errors
        return out

    # ------------------------------------------------------------------ files and headers

    def _folder(self, folder: str, catalog: Catalog | None = None) -> FolderRef:
        """Resolve a `folder` argument to a mapped folder or a genuine subfolder of one. Nothing else is reachable."""
        catalog = catalog or self.catalog()
        text = folder if isinstance(folder, str) else ""
        ref = catalog.find_folder(text) if text.strip() else None
        if ref is not None:
            return ref
        shown = _clip(text, 200)
        if text.strip() and len(text) <= MAX_FOLDER_CHARS and "\x00" not in text:
            if ".." in text.replace("\\", "/").split("/"):
                raise ToolFailure(
                    "folder_not_allowed",
                    f"'{shown}' contains '..'. Parent-directory references are never followed; name a mapped folder "
                    "or a subfolder of one exactly as list_folders returns it.")
            found = catalog.find_root_of(text)
            if found is not None:
                return self._subfolder(catalog, found[0], found[1], shown)
        known = [r.path for r in catalog.folders()]
        raise ToolFailure(
            "folder_not_found",
            f"'{shown}' is not a folder described by any process map, nor a subfolder of one. Only mapped "
            "folders and their subfolders can be read.",
            closest_known_folders=difflib.get_close_matches(text[:300], known, n=5, cutoff=0.4),
            hint="Call list_folders for the full inventory.",
        )

    def _subfolder(self, catalog: Catalog, root: FolderRef, parts: list[str], shown: str) -> FolderRef:
        """Walk from a mapped folder down to a subfolder, one real directory at a time.

        Every component must be an actual directory: a symbolic link is refused, never followed, so the
        mapped folder stays the boundary of what can be reached.
        """
        if len(parts) > MAX_SUBFOLDER_DEPTH:
            raise ToolFailure("invalid_argument", f"Subfolders deeper than {MAX_SUBFOLDER_DEPTH} levels are not supported.")
        path = root.resolved
        for depth, part in enumerate(parts):
            parent = os.path.join(root.path, *parts[:depth])
            if part in ("", ".", ".."):
                raise ToolFailure("folder_not_allowed", f"'{shown}' is not a plain subfolder path.")
            path = path / part
            try:
                st = os.lstat(path)  # lstat: a symbolic link is seen as a link, not as its target
            except (FileNotFoundError, NotADirectoryError):
                raise ToolFailure(
                    "folder_not_found",
                    f"'{shown}' does not exist: there is no subfolder '{_clip(part, 100)}' in '{_clip(parent, 200)}'.",
                    mapped_root=root.path,
                    hint=f"Call list_folders with folder='{_clip(parent, 200)}' to see its subfolders.",
                ) from None
            except (OSError, ValueError) as exc:
                raise ToolFailure("folder_unreadable", f"Cannot access '{shown}': {getattr(exc, 'strerror', None) or exc}.") from exc
            if stat_module.S_ISLNK(st.st_mode):
                raise ToolFailure(
                    "folder_not_allowed",
                    f"'{_clip(part, 100)}' in '{_clip(parent, 200)}' is a symbolic link. Links are never followed, "
                    "because they can lead outside the mapped folder.", mapped_root=root.path)
            if not stat_module.S_ISDIR(st.st_mode):
                raise ToolFailure("folder_not_found", f"'{shown}' is not a folder.", mapped_root=root.path)
        # Second, independent check: with every link resolved the location must be unchanged.
        if os.path.realpath(path) != str(path):
            raise ToolFailure("folder_not_allowed", f"'{shown}' does not resolve to a location inside the mapped folder "
                                                    f"'{root.path}'.", mapped_root=root.path)
        # The nearest mapped folder above the location supplies the metadata (a subfolder may itself be mapped).
        nearest = root
        for ref in catalog.folders():
            if ref.resolved == path:
                return ref
            if ref.resolved in path.parents and len(ref.resolved.parts) > len(nearest.resolved.parts):
                nearest = ref
        return nearest.child(os.path.relpath(path, nearest.resolved))

    def _open(self, folder: str, filename: str) -> OpenFile:
        ref = self._folder(folder)
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

    @staticmethod
    def _open_fd(of: OpenFile) -> int:
        """Open the file read-only without following a link at any level below the mapped folder.

        For a file in a subfolder the path is walked one directory at a time, each opened relative to
        the one above it and refused if it is a link. A directory swapped for a link after the folder
        was checked therefore cannot redirect the open to somewhere outside the mapped folder.
        """
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        parts = of.ref.relative_parts
        if not parts or os.open not in os.supports_dir_fd or not hasattr(os, "O_DIRECTORY"):
            return os.open(of.path, flags)
        step = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        dir_fd = os.open(of.ref.resolved.parents[len(parts) - 1], os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in parts:
                below = os.open(part, step, dir_fd=dir_fd)
                os.close(dir_fd)
                dir_fd = below
            return os.open(of.name, flags, dir_fd=dir_fd)
        finally:
            os.close(dir_fd)

    def _fh(self, of: OpenFile) -> BinaryIO:
        """Open read-only - the only way files are ever opened - and confirm it is the file that was checked."""
        try:
            fd = self._open_fd(of)
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
                iter_records(fh, delimiter=of.fmt.delimiter, encoding=of.fmt.encoding, control=probe,
                             chunk_bytes=64 * 1024, max_record_bytes=self.limits.max_record_bytes),
                None,
            )
        except CsvParseError as exc:
            raise ToolFailure("parse_error", _clip(f"Cannot read the header row of '{of.name}': {exc}", 500)) from exc
        except OSError as exc:
            raise ToolFailure("file_unreadable", f"Reading '{of.name}' failed: {exc.strerror or exc}.") from exc
        if first is None and probe.stop_reason == "cancelled":
            raise ToolFailure("cancelled", f"The operation was cancelled before '{of.name}' was read.")
        if first is None and probe.stop_reason == "time_limit":
            raise ToolFailure(
                "header_not_read",
                f"The time limit was reached before the header row of '{of.name}' could be read. Its first line "
                "is unusually long, which usually means a quote was opened and never closed.")
        if first is None or not first.fields:
            raise ToolFailure("parse_error", f"'{of.name}' is empty or its first line is blank, so it has no header row.")
        if probe.unterminated_quote_at_eof:
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
                f"Column name '{_clip(spec, 80)}' appears {len(matches)} times in '{of.name}' (first positions "
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
            f"'{of.name}' has no column named '{_clip(spec, 80)}' (column names are matched exactly).",
            similar_column_names=list(dict.fromkeys(folded[:5] + close)),
            hint="Use search_column_names or inspect_csv_headers to find the right column.",
        )

    def _filters(self, header: Header, of: OpenFile, filters: dict[str, str] | None, identifier_column: int | None,
                 name: str = "filters") -> list[tuple[int, str]]:
        """Resolve record-key filters to (column index, exact value) pairs, all of which one row must satisfy."""
        if not filters:
            return []
        if not isinstance(filters, dict):
            raise ToolFailure("invalid_argument", f"{name} must be an object of column name -> exact value.")
        if len(filters) > MAX_FILTERS:
            raise ToolFailure("invalid_argument", f"At most {MAX_FILTERS} {name} are supported.")
        resolved: dict[int, str] = {}
        for spec, value in filters.items():
            if not isinstance(spec, str) or not isinstance(value, str):
                raise ToolFailure("invalid_argument", f"{name}: column names and values must be text. Values are "
                                                      "compared as exact text, so pass '00123', not 123.")
            if len(value) > MAX_IDENTIFIER_CHARS:
                raise ToolFailure("invalid_argument", f"{name}: a value is longer than {MAX_IDENTIFIER_CHARS} characters.")
            index = self._column(header, spec, of)
            if index == identifier_column:
                raise ToolFailure(
                    "invalid_argument",
                    f"{name}: '{_clip(spec, 80)}' is the identifier column, which is already matched against the "
                    "identifier. Filter on other columns.")
            if index in resolved:
                raise ToolFailure(
                    "duplicate_filter_column",
                    f"{name}: more than one entry refers to column '{_clip(header.label(index), 80)}' "
                    f"(position {index + 1}). Give each column once.")
            resolved[index] = value
        return sorted(resolved.items())

    @staticmethod
    def _filters_shown(header: Header, filters: list[tuple[int, str]]) -> list[dict[str, str]]:
        return [{"column": _clip(header.label(i), 80), "value": value} for i, value in filters]

    @staticmethod
    def _filters_text(header: Header, filters: list[tuple[int, str]]) -> str:
        if not filters:
            return ""
        return " and, in the same row, " + " and ".join(
            f"'{_clip(value, 60)}' in column '{_clip(header.label(i), 60)}'" for i, value in filters)

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
        subfolders: list[str] = []
        ignored = unparseable = 0
        needle = filename_contains.casefold() if filename_contains else None
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    subfolders.append(entry.name)
                    continue
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
        files.sort(key=lambda f: f["filename"], reverse=newest_first)

        subfolders.sort(reverse=True)
        shown = _some([os.path.join(ref.path, name) for name in subfolders[:SUBFOLDERS_IN_FILE_LISTING]], 1000)
        nested: dict[str, Any] = {"subfolder_count": len(subfolders), "subfolders": shown}
        if subfolders:
            nested["subfolders_note"] = (
                "Files inside subfolders are NOT included in this listing. Pass a subfolder path as 'folder' to list "
                "its files"
                + (f"; list_folders with folder='{_clip(ref.path, 200)}' pages through all {len(subfolders)} subfolders."
                   if len(shown) < len(subfolders) else "."))
        if ref.record_key_columns:
            nested["record_key_columns"] = ref.record_key_columns
        budget = max(self.limits.max_output_bytes - ENVELOPE_RESERVE_BYTES - _size(nested), 400)
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
            "kind": ref.kind,
            "mapped_root": ref.mapped_root,
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
            out["other_entries_ignored"] = ignored
            out["other_entries_note"] = "Entries that are not regular .csv files (other types, links) are not listed or read."
        if unparseable:
            out["files_excluded_because_filename_has_no_parseable_date"] = unparseable
        out.update(nested)
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
            total = len(header.columns)
            out: dict[str, Any] = {"file": of.info(), "column_count": total}
            duplicates = header.duplicates()
            if duplicates:
                out["duplicate_column_names"] = duplicates
            layout = self._layout_warnings(header, of)
            if layout:
                out["warnings"] = layout
            budget = self.limits.max_output_bytes - TOOL_NAME_BYTES - _size(out) - 700
            start = min(max(column_offset, 0), total)
            names = _some([header.display(i) for i in range(start, total)], budget // 2 if sample_rows else budget)
            used = _size(names)
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
                room = budget - used
                truncated = False
                control = ScanControl()
                try:
                    for record in iter_records(fh, delimiter=of.fmt.delimiter, encoding=of.fmt.encoding,
                                               start_offset=header.data_offset, start_record=1, control=control,
                                               chunk_bytes=64 * 1024, max_record_bytes=self.limits.max_record_bytes):
                        if len(rows) >= sample_rows:
                            break
                        if not record.fields:
                            continue
                        share = max(room // (sample_rows - len(rows)), 300)
                        row = self._fit(self._row(header, record, wanted), share)
                        cost = _size(row) + 2
                        if cost > room:
                            truncated = True
                            break
                        truncated = truncated or "values_omitted_for_size" in row
                        room -= cost
                        rows.append(row)
                except CsvParseError as exc:
                    out["sample_error"] = _clip(f"Sampling stopped: {exc}", 400)
                out["sample_rows"] = rows
                out["sample_note"] = (f"The first {len(rows)} data record(s) only: a layout sample, not evidence "
                                      "about any security.")
                if truncated:
                    out["sample_truncated"] = True
                if control.undecodable_lines:
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
            "term": _clip(term, 200),
            "match_type": "case-insensitive substring of the column name",
            "column_count": len(header.columns),
            "matching_column_count": len(hits),
        }
        layout = self._layout_warnings(header, of)
        if layout:
            out["warnings"] = layout
        budget = self.limits.max_output_bytes - TOOL_NAME_BYTES - _size(out) - 600
        page: list[dict[str, Any]] = []
        for hit in hits:
            budget -= _size(hit) + 2
            if budget < 0:
                break
            page.append(hit)
        out["matching_columns"] = page
        out["truncated"] = len(page) < len(hits)
        if not hits:
            short = [n for n in header.columns if len(n) <= LABEL_MAX_CHARS]
            out["similar_column_names"] = _some(difflib.get_close_matches(term, short, n=5, cutoff=0.5), 1200)
        return out

    # ------------------------------------------------------------------ row formatting

    def _cell(self, value: str) -> tuple[str, bool]:
        limit = self.limits.max_cell_chars
        if len(value) <= limit:
            return value, False
        return f"{value[:limit]}...[cell truncated; {len(value)} characters in total]", True

    def _row(self, header: Header, record: Record, wanted: list[int] | None,
             matched: list[int] | None = None) -> dict[str, Any]:
        fields = record.fields
        indices = list(dict.fromkeys([*wanted, *(matched or [])])) if wanted is not None else range(len(fields))
        values: dict[str, str] = {}
        cut: list[str] = []
        for i in indices:
            if i >= len(fields):
                continue
            label = header.label(i)
            values[label], was_cut = self._cell(fields[i])
            if was_cut:
                cut.append(label)
        row: dict[str, Any] = {"record_number": record.number, "byte_offset": record.offset}
        if matched is not None:
            names = [header.label(i) for i in matched]
            row["matched_columns"] = _some(names, 1200)
            if len(row["matched_columns"]) < len(names):
                row["matched_column_count"] = len(names)
        row["values"] = values
        if cut:
            row["truncated_cells"] = _some(cut, 600)
            if len(row["truncated_cells"]) < len(cut):
                row["truncated_cell_count"] = len(cut)
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
        used = _size({k: v for k, v in row.items() if k != "values"}) + 260  # the notes added below
        for label, value in values.items():
            cost = _size(label) + _size(value) + 4
            if used + cost > budget:
                room = (budget - used - _size(label) - 80) // 4
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

    # ------------------------------------------------------------------ cursors

    def _fingerprint(self, tool: str, of: OpenFile, identifier: str, column: int | None,
                     filters: list[tuple[int, str]]) -> str:
        blob = json.dumps([tool, str(of.path), of.size, of.mtime_ns, of.fmt.delimiter, of.fmt.encoding, identifier,
                           column, filters])
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

    def _integrity(self, control: ScanControl, of: OpenFile, header: Header,
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
        if control.unterminated_quote_at_eof:
            warnings.append("The file ends inside a quoted field (unterminated quote): the final record is "
                            "incomplete and records may be hidden inside it.")
        if control.alt_hits:
            warnings.append(
                f"The identifier's bytes were found in the file as {' / '.join(sorted(control.alt_hits))} text, but the "
                f"folder is declared as {of.fmt.encoding}. The declared encoding is probably wrong for this file, so "
                "values with non-ASCII characters cannot be matched reliably.")
        sound = not warnings
        if control.undecodable_lines:
            warnings.append(self._undecodable_warning(of))
        return warnings, sound, changed

    def _iter_matches(self, fh: BinaryIO, of: OpenFile, header: Header, identifier: str, column: int | None,
                      start_offset: int, start_record: int, control: ScanControl,
                      hints: _Hints, filters: list[tuple[int, str]] = ()) -> Iterator[tuple[Record, list[int]]]:
        """Yield (record, indices of matching fields) for exact matches, in file order.

        With `filters`, a record matches only if it also holds each filter's exact value in that column.
        """
        def passes(record: Record) -> bool:
            fields = record.fields
            if all(i < len(fields) and fields[i] == value for i, value in filters):
                return True
            hints.filtered_out(record, header, filters)
            return False

        control.alt_needles = self._alt_needles(identifier, of.fmt.encoding)
        control.alt_hits = set()
        control.undecodable_lines = 0
        control.unterminated_quote_at_eof = False
        for record in iter_records(
            fh, delimiter=of.fmt.delimiter, encoding=of.fmt.encoding,
            needle=prefilter_needle(identifier, of.fmt.encoding),
            start_offset=start_offset, start_record=start_record, control=control,
            chunk_bytes=self.limits.chunk_bytes, max_record_bytes=self.limits.max_record_bytes,
        ):
            fields = record.fields
            if column is not None:
                value = fields[column] if column < len(fields) else None
                if value == identifier:
                    if passes(record):
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
                if passes(record):
                    control.matches += 1
                    yield record, found
            else:
                for i, value in enumerate(fields):
                    if identifier in value:
                        hints.value(record, header.label(i), value)

    def _search(self, tool: str, control: ScanControl, folder: str, filename: str, identifier: str,
                column: str | None, columns: list[str] | None, max_rows: int | None,
                cursor: str | None, *, count_only: bool = False,
                filters: dict[str, str] | None = None) -> dict[str, Any]:
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
        limit = self.limits.max_output_bytes - TOOL_NAME_BYTES
        search: dict[str, Any] = {"identifier": identifier, "match_type": MATCH_TYPE}
        result: dict[str, Any] = {"file": of.info(), "search": search}
        with self._fh(of) as fh:
            header = self._header(fh, of, control)
            column_index = None
            if tool != "search_across_columns":
                column_index = self._column(header, column or "", of)
                search["column"] = header.label(column_index)
            else:
                search["columns_searched"] = f"all {len(header.columns)} columns"
            row_filters = self._filters(header, of, filters, column_index)
            search["filters"] = self._filters_shown(header, row_filters)
            also = self._filters_text(header, row_filters)
            wanted = None
            if columns and not count_only:
                wanted = [self._column(header, c, of) for c in columns]
                names = [header.label(i) for i in wanted]
                shown = _some(names, 800)
                search["columns_returned"] = shown if len(shown) == len(names) else {"count": len(names), "first": shown}
            elif not count_only:
                search["columns_returned"] = "all"

            fingerprint = self._fingerprint(tool, of, identifier, column_index, row_filters)
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
            hints = _Hints(identifier)
            rows: list[dict[str, Any]] = []
            found = 0
            # Rows get whatever is left after the request echo and a reserve for the scan report.
            budget = max(limit - SCAN_RESERVE_BYTES - _size(result), 400)
            used = 0
            stop_reason: str | None = None
            resume: tuple[int, int] | None = None
            error: ToolFailure | None = None
            try:
                for record, hits in self._iter_matches(fh, of, header, identifier, column_index,
                                                       start_offset, start_record, control, hints, row_filters):
                    if count_only:
                        found += 1
                        continue
                    if len(rows) >= row_limit:
                        stop_reason, resume = "row_limit", (record.offset, record.number - 1)
                        break
                    row = self._row(header, record, wanted, hits if column_index is None else None)
                    cost = _size(row) + 2
                    if used + cost > budget:
                        if rows:
                            stop_reason, resume = "output_size_limit", (record.offset, record.number - 1)
                            break
                        row = self._fit(row, budget)
                        cost = _size(row) + 2
                    rows.append(row)
                    used += cost
                    found += 1
            except CsvParseError as exc:
                error = ToolFailure("parse_error", _clip(str(exc), 400),
                                    record_number=exc.record_number, byte_offset=exc.byte_offset)
            except OSError as exc:
                error = ToolFailure("file_unreadable", f"Reading '{of.name}' failed part-way: {exc.strerror or exc}.")
            if stop_reason is None and error is None:
                stop_reason = control.stop_reason
                resume = (control.resume_offset, control.resume_record)
            warnings, sound, changed = self._integrity(control, of, header, identifier)

        reached_end = stop_reason == "end_of_file"
        total = before + found
        trustworthy = reached_end and sound
        shown_id = _clip(identifier, 60)

        result["scan"] = {
            "complete": reached_end,
            "stop_reason": "error" if error else stop_reason,
            "started_at_byte": start_offset,
            "stopped_at_byte": control.bytes_done if resume is None else resume[0],
            "file_size_bytes": of.size,
            "elapsed_seconds": round(control.elapsed, 2),
        }
        records_after_header = None
        if reached_end:
            records_after_header = max(control.resume_record - 1, 0)
            result["scan"]["records_after_header"] = records_after_header
        if cursor and reached_end:
            result["scan"]["note"] = "Together with the earlier responses in this cursor chain, the whole file has been scanned."

        where = f"column '{_clip(search['column'], 80)}'" if column_index is not None else "any column"
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
        if reached_end:
            why = "the file could not be read reliably (see warnings)"
        aside = ""
        if not total and hints:
            aside = f" Not exact matches, but worth checking: {hints.sentence()} (see near_misses)."
        elif not total and reached_end:
            aside = " No near misses were seen either."
        if records_after_header == 0:
            aside += " Note: the file has no data records, only a header."

        if count_only:
            exact = trustworthy and error is None
            result["count"] = total if exact else None
            result["count_is_exact"] = exact
            if exact:
                result["explanation"] = f"Complete scan: exactly {total} record(s) have '{shown_id}' in {where}{also}." + aside
            else:
                result["matches_seen_before_interruption"] = total
                result["explanation"] = (
                    f"A complete count is NOT available because {why}. {total} matching record(s) were seen in the "
                    "part scanned; this is a lower bound, not the count."
                    + (" Pass next_cursor to continue counting from where this scan stopped." if next_cursor else "")
                )
        else:
            if reached_end and error is None:
                more = "none" if sound else "unknown"
            else:
                more = "yes" if stop_reason in ("row_limit", "output_size_limit") else "unknown"
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
                    text = (f"PARTIAL RESULTS: the scan is incomplete or unreliable because {why}. The {len(rows)} "
                            "record(s) returned are only the matches that could be read; there may be more."
                            + (" Pass next_cursor to continue." if next_cursor else ""))
            elif outcome == "confirmed_absent":
                text = f"Complete scan: no record has '{shown_id}' as the exact value of {where}{also}." + aside
            else:
                text = (f"INCONCLUSIVE: no match was found, but absence is not confirmed because {why}."
                        + (" Pass next_cursor to continue the scan." if next_cursor else "") + aside)
            result["explanation"] = text
        result["next_cursor"] = next_cursor
        if error:
            result["error"] = {"type": error.kind, "message": error.message, **error.details}
        if warnings:
            result["warnings"] = warnings
        # Optional extras, added only while they fit.
        extras: list[tuple[str, Any]] = []
        if hints:
            extras.append(("near_misses", hints.as_dict()))
            extras.append(("near_misses", hints.as_dict(examples=False)))
        duplicates = None if count_only else header.duplicates()
        if duplicates:
            extras.append(("duplicate_column_names", duplicates))
        for key, value in extras:
            if key not in result and _size(result) + _size(value) + len(key) + 6 <= limit:
                result[key] = value
        return result

    def find_security_in_file(self, control: ScanControl, folder: str, filename: str, identifier: str,
                              identifier_column: str, columns: list[str] | None = None,
                              max_rows: int | None = None, cursor: str | None = None,
                              filters: dict[str, str] | None = None) -> dict[str, Any]:
        return self._search("find_security_in_file", control, folder, filename, identifier,
                            identifier_column, columns, max_rows, cursor, filters=filters)

    def search_across_columns(self, control: ScanControl, folder: str, filename: str, identifier: str,
                              columns: list[str] | None = None, max_rows: int | None = None,
                              cursor: str | None = None, filters: dict[str, str] | None = None) -> dict[str, Any]:
        return self._search("search_across_columns", control, folder, filename, identifier,
                            None, columns, max_rows, cursor, filters=filters)

    def count_matching_rows(self, control: ScanControl, folder: str, filename: str, identifier: str,
                            identifier_column: str, cursor: str | None = None,
                            filters: dict[str, str] | None = None) -> dict[str, Any]:
        return self._search("count_matching_rows", control, folder, filename, identifier,
                            identifier_column, None, None, cursor, count_only=True, filters=filters)

    # ------------------------------------------------------------------ comparison

    def compare_security_records(self, control: ScanControl, folder_a: str, filename_a: str, folder_b: str,
                                 filename_b: str, identifier: str, identifier_column_a: str,
                                 identifier_column_b: str | None = None, identifier_b: str | None = None,
                                 compare_columns: list[str] | None = None,
                                 column_mapping: dict[str, str] | None = None,
                                 match_keys: list[str] | None = None,
                                 filters: dict[str, str] | None = None,
                                 filters_b: dict[str, str] | None = None) -> dict[str, Any]:
        control.begin()
        control.phase = "opening"
        mapping = column_mapping or {}
        identifier_b = identifier if identifier_b is None else identifier_b
        self._check_identifier(identifier)
        self._check_identifier(identifier_b)
        if match_keys and len(match_keys) > MAX_MATCH_KEYS:
            raise ToolFailure("invalid_argument", f"At most {MAX_MATCH_KEYS} match_keys are supported.")
        sides = {"a": self._open(folder_a, filename_a), "b": self._open(folder_b, filename_b)}
        limit = self.limits.max_output_bytes - TOOL_NAME_BYTES
        result: dict[str, Any] = {"file_a": sides["a"].info(), "file_b": sides["b"].info()}
        with self._fh(sides["a"]) as fh_a, self._fh(sides["b"]) as fh_b:
            handles = {"a": fh_a, "b": fh_b}
            headers = {key: self._header(handles[key], of, control) for key, of in sides.items()}
            head_a, head_b = headers["a"], headers["b"]
            id_cols = {
                "a": self._column(head_a, identifier_column_a, sides["a"]),
                "b": self._column(head_b, identifier_column_b or mapping.get(identifier_column_a, identifier_column_a), sides["b"]),
            }

            # Record-key filters: `filters` names file A's columns and applies to both files (through
            # column_mapping); `filters_b`, when given, replaces it for file B with B's own columns and values.
            side_filters = {"a": self._filters(head_a, sides["a"], filters, id_cols["a"])}
            if filters_b is not None:
                side_filters["b"] = self._filters(head_b, sides["b"], filters_b, id_cols["b"], "filters_b")
            else:
                # Carried over by column, not by position: '#2' in file A means A's second column, by name, in B.
                carried: dict[str, str] = {}
                for index, value in side_filters["a"]:
                    name = head_a.columns[index]
                    target = next((mapping[k] for k in (name, head_a.labels[index], f"#{index + 1}") if k in mapping), name)
                    if target in carried:
                        raise ToolFailure("duplicate_filter_column", f"filters: more than one entry maps to column "
                                                                     f"'{_clip(target, 80)}' of file B.")
                    carried[target] = value
                side_filters["b"] = self._filters(head_b, sides["b"], carried, id_cols["b"], "filters (applied to file B)")
            filtered = bool(side_filters["a"] or side_filters["b"])

            def pair(name_a: str) -> tuple[int, int]:
                return (self._column(head_a, name_a, sides["a"]),
                        self._column(head_b, mapping.get(name_a, name_a), sides["b"]))

            only_a: list[str] = []
            if compare_columns:
                pairs = [pair(name) for name in compare_columns]
            else:
                # Default: every column of A with exactly one counterpart in B (mapped, or same name).
                pairs = []
                counts_a: dict[str, int] = {}
                for name in head_a.columns:
                    counts_a[name] = counts_a.get(name, 0) + 1
                lookup_b: dict[str, int | None] = {}
                for i, name in enumerate(head_b.columns):
                    lookup_b[name] = None if name in lookup_b else i  # None marks a repeated name
                for i, label in enumerate(head_b.labels):
                    lookup_b.setdefault(label, i)
                for i, name in enumerate(head_a.columns):
                    mapped = mapping.get(name, mapping.get(head_a.labels[i])) if counts_a[name] == 1 else mapping.get(head_a.labels[i])
                    if mapped is not None:
                        j = self._column(head_b, mapped, sides["b"])  # an explicit mapping must resolve
                    else:
                        j = lookup_b.get(name) if counts_a[name] == 1 else None
                    if j is None:
                        only_a.append(head_a.label(i))
                    else:
                        pairs.append((i, j))
            keys = [pair(name) for name in match_keys] if match_keys else []
            label_a, label_b = head_a.labels, head_b.labels
            used_b = {b for _, b in pairs}
            search: dict[str, Any] = {
                "identifier_a": identifier, "identifier_column_a": label_a[id_cols["a"]],
                "identifier_b": identifier_b, "identifier_column_b": label_b[id_cols["b"]],
                "match_type": MATCH_TYPE,
                "match_keys": [{"a": label_a[a], "b": label_b[b]} for a, b in keys],
                "filters_a": self._filters_shown(head_a, side_filters["a"]),
                "filters_b": self._filters_shown(head_b, side_filters["b"]),
            }
            if compare_columns:
                listed: list[dict[str, str]] = []
                room = 1200
                for a, b in pairs:
                    entry = {"a": label_a[a], "b": label_b[b]}
                    room -= _size(entry) + 2
                    if room < 0:
                        break
                    listed.append(entry)
                search["compared_columns"] = listed
                if len(listed) < len(pairs):
                    search["compared_column_count"] = len(pairs)
            else:
                search["compared_columns"] = f"all {len(pairs)} columns of file A with exactly one counterpart in file B"
                only_b = [l for i, l in enumerate(label_b) if i not in used_b]
                result["columns_not_compared"] = {
                    "only_in_file_a_count": len(only_a), "only_in_file_a": _some(only_a, 500),
                    "only_in_file_b_count": len(only_b), "only_in_file_b": _some(only_b, 500),
                    "note": "Columns are paired by identical name unless column_mapping says otherwise; "
                            "repeated names are not paired.",
                }
            result["search"] = search

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
                hints = _Hints(ident)
                reason: str | None = None
                problem: str | None = None
                try:
                    for record, _ in self._iter_matches(handles[key], of, header, ident, id_cols[key],
                                                        header.data_offset, 1, control, hints, side_filters[key]):
                        if len(records[key]) >= cap:
                            reason = "too_many_matches"
                            break
                        records[key].append(record)
                except CsvParseError as exc:
                    reason, problem = "error", _clip(str(exc), 300)
                except OSError as exc:
                    reason, problem = "error", f"reading failed part-way: {exc.strerror or exc}"
                reason = reason or control.stop_reason
                file_warnings, sound, _ = self._integrity(control, of, header, ident)
                warnings += [f"File {key.upper()}: {text}" for text in file_warnings]
                complete = reason == "end_of_file" and sound
                presence[key] = {
                    "outcome": "match_found" if records[key] else ("confirmed_absent" if complete else "inconclusive"),
                    "matching_records_found": len(records[key]),
                    "scan_complete": complete,
                    "stop_reason": reason,
                }
                if not complete:
                    presence[key]["explanation"] = (
                        f"The scan of file {key.upper()} is incomplete or unreliable because "
                        f"{problem or STOP_EXPLANATIONS.get(reason or '', 'the file could not be read reliably (see warnings)')}"
                        "; the records found are partial."
                    )
                if hints and not records[key]:
                    presence[key]["near_misses"] = "Not exact matches, but worth checking: " + hints.sentence() + "."
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
        # Everything below is added only while it fits; 1200 bytes are kept for the status and explanation.
        budget = [limit - _size(result) - 1200]

        def take(cost: int) -> bool:
            if cost > budget[0]:
                return False
            budget[0] -= cost
            return True

        def cell(record: Record, index: int) -> str | None:
            return record.fields[index] if index < len(record.fields) else None

        def show(value: str | None) -> str | None:
            return None if value is None else self._cell(value)[0]

        def list_records(name: str, recs: list[Record], header: Header, indices: list[int]) -> None:
            rows = []
            for record in recs:
                row = self._fit(self._row(header, record, indices), max(min(budget[0], 2000), 300))
                if not take(_size(row) + 2):
                    break
                rows.append(row)
            result[name] = rows
            if len(rows) < len(recs):
                result[name + "_omitted_for_size"] = len(recs) - len(rows)

        def varying(recs: list[Record], header: Header) -> list[str]:
            names = [label for i, label in enumerate(header.labels) if len({cell(r, i) for r in recs}) == len(recs)]
            return _some(names[:20], 500)

        def describe(labels: list[str], indices: list[int], values: tuple) -> dict[str, Any]:
            return {_clip(labels[i], 60): (None if v is None else _clip(v, 80)) for i, v in zip(indices, values)}

        omitted = {"comparisons": 0, "ambiguous_keys": 0, "unmatched_records_in_file_a": 0, "unmatched_records_in_file_b": 0}

        def add(name: str, item: dict[str, Any]) -> None:
            if take(_size(item) + 2):
                result.setdefault(name, []).append(item)
            else:
                result.setdefault(name, [])
                omitted[name] += 1

        def compare_pair(rec_a: Record, rec_b: Record, key: dict[str, Any] | None) -> None:
            changed, unchanged = [], []
            for a, b in pairs:
                va, vb = cell(rec_a, a), cell(rec_b, b)
                entry: dict[str, Any] = {"column_a": label_a[a]}
                if label_b[b] != label_a[a]:
                    entry["column_b"] = label_b[b]
                if va == vb:
                    entry["value"] = show(va)
                    unchanged.append(entry)
                    continue
                entry["value_a"], entry["value_b"] = show(va), show(vb)
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
            if not take(_size(out) + 400):  # 400: room for the truncation notes below
                result.setdefault("comparisons", [])
                omitted["comparisons"] += 1
                return
            left_out: list[str] = []
            for source, target in ((changed, "changed"), (unchanged, "unchanged")):
                for entry in source:
                    if take(_size(entry) + 2):
                        out[target].append(entry)
                    else:
                        left_out.append(entry["column_a"])
            if left_out:
                out["truncated"] = True
                out["columns_omitted_for_size_count"] = len(left_out)
                out["columns_omitted_for_size"] = _some(left_out, 250)
            else:
                budget[0] += 400
            result.setdefault("comparisons", []).append(out)

        incomplete = not (presence["a"]["scan_complete"] and presence["b"]["scan_complete"])
        subject = "a record matching the identifier and all filters" if filtered else "the security"
        shown_a = [a for a, _ in pairs][:50]
        shown_b = [b for _, b in pairs][:50]
        if incomplete:
            status = "inconclusive"
            text = ("INCONCLUSIVE: at least one file was not scanned completely or could not be read reliably, so "
                    "presence and record alignment cannot be established. Any record counts above are partial. "
                    "No comparison was made.")
            if "too_many_matches" in (presence["a"]["stop_reason"], presence["b"]["stop_reason"]):
                text += (" A file holds more matching records than one comparison can line up: narrow it to one "
                         "record with filters (for example a portfolio code).")
        elif not recs_a and not recs_b:
            status, text = "absent_from_both", f"Complete scans: {subject} is in neither file."
        elif not recs_a or not recs_b:
            status = "present_in_one_file_only"
            there, missing = ("A", "B") if recs_a else ("B", "A")
            text = f"Complete scans: {subject} is in file {there} ({len(recs_a or recs_b)} record(s)) and absent from file {missing}."
            if recs_a:
                list_records("records_in_file_a", recs_a, head_a, shown_a)
            else:
                list_records("records_in_file_b", recs_b, head_b, shown_b)
        elif not keys and (len(recs_a) > 1 or len(recs_b) > 1):
            status = "ambiguous_alignment"
            text = (f"AMBIGUOUS: file A has {len(recs_a)} matching record(s) and file B has {len(recs_b)}, and no match_keys "
                    "were given, so records cannot be paired reliably. No comparison was made. Supply match_keys that "
                    "identify one record per file (see columns_that_differ_between_matches), or compare specific records "
                    "using find_security_in_file.")
            differ = {"file_a": varying(recs_a, head_a), "file_b": varying(recs_b, head_b)}
            if take(_size(differ) + 50):
                result["columns_that_differ_between_matches"] = differ
            list_records("records_in_file_a", recs_a, head_a, shown_a)
            list_records("records_in_file_b", recs_b, head_b, shown_b)
        else:
            groups_a: dict[tuple, list[Record]] = {}
            groups_b: dict[tuple, list[Record]] = {}
            for rec in recs_a:
                groups_a.setdefault(tuple(cell(rec, a) for a, _ in keys), []).append(rec)
            for rec in recs_b:
                groups_b.setdefault(tuple(cell(rec, b) for _, b in keys), []).append(rec)
            key_a, key_b = [a for a, _ in keys], [b for _, b in keys]
            counts = {"comparisons": 0, "ambiguous_keys": 0, "unmatched_records_in_file_a": 0, "unmatched_records_in_file_b": 0}
            differing = 0
            for key_values, group in groups_a.items():
                other = groups_b.get(key_values)
                described = describe(label_a, key_a, key_values)
                if other is None:
                    for r in group:
                        counts["unmatched_records_in_file_a"] += 1
                        add("unmatched_records_in_file_a", {"record_number": r.number, "key": described})
                elif len(group) > 1 or len(other) > 1:
                    counts["ambiguous_keys"] += 1
                    add("ambiguous_keys", {"key": described, "records_in_file_a": [r.number for r in group],
                                           "records_in_file_b": [r.number for r in other]})
                else:
                    counts["comparisons"] += 1
                    differing += any(cell(group[0], a) != cell(other[0], b) for a, b in pairs)
                    compare_pair(group[0], other[0], described if keys else None)
            for key_values, group in groups_b.items():
                if key_values not in groups_a:
                    described = describe(label_b, key_b, key_values)
                    for r in group:
                        counts["unmatched_records_in_file_b"] += 1
                        add("unmatched_records_in_file_b", {"record_number": r.number, "key": described})
            left = {name: n for name, n in omitted.items() if n}
            if left:
                result["omitted_for_size"] = left
            compared, ambiguous = counts["comparisons"], counts["ambiguous_keys"]
            only_in_a, only_in_b = counts["unmatched_records_in_file_a"], counts["unmatched_records_in_file_b"]
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
        result["comparison_status"] = status
        result["explanation"] = text
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
