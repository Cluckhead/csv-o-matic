"""The nine tools, as plain synchronous functions returning JSON-ready dicts.

Nothing here knows about MCP. Scanning functions take a `ScanControl` so the caller can
impose a time limit, cancel, and watch progress. Files are only ever opened read-only.
"""

from __future__ import annotations

import base64
import contextlib
import difflib
import hashlib
import heapq
import hmac
import json
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, BinaryIO, Iterator

from . import access as access_module
from .access import AccessError, ConfinedDir
from .config import Config
from .csvscan import CsvParseError, Record, ScanControl, iter_records, prefilter_needle
from .process_maps import Catalog, CsvFormat, FolderRef, Place, ProcessMap, load_catalog

MATCH_TYPE = "exact: whole field value, case-sensitive, compared as text"
MAX_IDENTIFIER_CHARS = 200
MAX_MATCH_KEYS = 10
MAX_FILTERS = 10
MAX_FILTER_VALUE_CHARS = 200
LABEL_MAX_CHARS = 200  # longer column names are shown shortened
# Sizes are measured as bytes on the wire: every response is sent twice, as structured content and
# as JSON text (see _size), so these reserves cover both copies.
ENVELOPE_RESERVE_BYTES = 7_000  # for listings: room for everything in a response other than the items
SCAN_RESERVE_BYTES = 6_800  # for searches: room for scan status, explanation, cursor and warnings
RESULT_ENVELOPE_BYTES = 400  # the MCP result wrapper and the tool name
POSITION_SELECTOR = re.compile(r"#[1-9][0-9]*")
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
    """Bytes `obj` adds to a tool result on the wire.

    A result carries the payload twice: as structured content, and as JSON text inside a JSON string
    (where quotes and backslashes are escaped again). Both copies count towards max_output_bytes.
    """
    text = json.dumps(obj, ensure_ascii=False)
    return (len(text.encode("utf-8", "replace"))
            + len(json.dumps(text, ensure_ascii=False).encode("utf-8", "replace")) - 2)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}...[{len(text)} characters]"


def _some(names: list[str], max_bytes: int) -> list[str]:
    """As many leading names as fit in `max_bytes`; callers report the full count separately."""
    out: list[str] = []
    used = 2
    for name in names:
        used += _size(name) + 4
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
    taken: set[str] = field(default_factory=set)  # every label in use, so generated ones never collide

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
        return cls(columns, labels, data_offset, used)

    def suspect_delimiter(self, delimiter: str) -> str | None:
        """Another delimiter that the header suggests the file really uses, if any."""
        if len(self.columns) != 1:
            return None
        counts = {d: self.columns[0].count(d) for d in (";", "\t", "|", ",") if d != delimiter}
        best = max(counts, key=lambda d: counts[d])
        return best if counts[best] >= 2 else None

    def label(self, index: int) -> str:
        """The key a field is returned under. A field beyond the header gets a generated label that
        never collides with a real column, even one literally named '[extra field N]'."""
        if index < len(self.labels):
            return self.labels[index]
        label = f"[extra field {index + 1}]"
        while label in self.taken:
            label += "'"
        return label

    def display(self, index: int) -> str:
        """The original name, unless it is too long to show."""
        name = self.columns[index]
        return name if len(name) <= LABEL_MAX_CHARS else self.labels[index]

    def plain_name_is_selector(self, index: int) -> bool:
        """Whether passing the column's name as a plain string selects exactly this column."""
        name = self.columns[index]
        if not name or len(name) > LABEL_MAX_CHARS or self.columns.count(name) != 1:
            return False
        if POSITION_SELECTOR.fullmatch(name) and int(name[1:]) != index + 1:
            return False
        return not any(label == name and i != index for i, label in enumerate(self.labels))

    def selector(self, index: int) -> Any:
        """An unambiguous way to select this column: its name where that is safe, else its position."""
        return self.columns[index] if self.plain_name_is_selector(index) else {"position": index + 1}

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
            "note": "Rows label repeated names as 'name [col N]'; select one as {\"position\": N} (or '#N').",
        }


@dataclass
class Location:
    """A folder the caller may read: an approved root, or a genuine subfolder below one."""

    at: Place

    @property
    def ref(self) -> FolderRef:
        return self.at.ref

    @property
    def parts(self) -> tuple[str, ...]:
        return self.at.parts

    @property
    def path(self) -> str:
        return "/".join([self.ref.path.replace("\\", "/"), *self.parts]) if self.parts else self.ref.path

    def access(self) -> dict[str, Any]:
        if not self.parts:
            return {"kind": "mapped_folder"}
        return {"kind": "discovered_subfolder", "approved_root": self.ref.path,
                "metadata": "CSV format, filename conventions and identifier columns are inherited from the "
                            "approved root; the process maps say nothing specific about this subfolder."}


@dataclass
class OpenFile:
    loc: Location
    name: str
    fmt: CsvFormat
    size: int
    mtime_ns: int
    ctime_ns: int
    identity: tuple[int, int]  # (device, inode) of the file that is open
    fh: BinaryIO  # opened read-only - the only way files are ever opened
    end_ctime_ns: int | None = None  # change time when the scan finished, which a cursor is bound to

    def info(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "folder": self.loc.path,
            "filename": self.name,
            "size_bytes": self.size,
            "filesystem_modified_time": _iso(self.mtime_ns / 1e9),
        }
        if self.loc.parts:
            out["folder_access"] = self.loc.access()
        return out


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
        self.failing_filters = 0  # exact identifier matches in records that did not satisfy the filters
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
            examples.append({"record_number": record.number, "column": _clip(label, 60), "value": _clip(value, 80)})

    def multi_line(self, record: Record, label: str, value: str) -> None:
        """The identifier inside a value that spans lines, in a column that was not searched."""
        self.spanning += 1
        if len(self.spanning_examples) < 2:
            at = value.find(self.identifier)
            self.spanning_examples.append({"record_number": record.number, "column": _clip(label, 60),
                                           "value_excerpt": value[max(at - 30, 0): at + len(self.identifier) + 30]})

    def other_column(self, label: str) -> None:
        """The exact identifier, in a column other than the one searched."""
        key = _clip(label, 60)
        if key in self.elsewhere or len(self.elsewhere) < 5:
            self.elsewhere[key] = self.elsewhere.get(key, 0) + 1

    def __bool__(self) -> bool:
        return bool(self.padded or self.longer or self.elsewhere or self.spanning)

    def state(self) -> dict[str, Any]:
        """Compact totals to carry in a cursor (examples are not carried)."""
        out: dict[str, Any] = {k: v for k, v in (("p", self.padded), ("l", self.longer), ("s", self.spanning),
                                                  ("f", self.failing_filters), ("e", self.elsewhere)) if v}
        return out

    def restore(self, state: dict[str, Any]) -> None:
        self.padded, self.longer, self.spanning = state.get("p", 0), state.get("l", 0), state.get("s", 0)
        self.failing_filters = state.get("f", 0)
        self.elsewhere = dict(state.get("e", {}))

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
        out: dict[str, Any] = {"note": "Seen in the part of the file scanned (all responses of this cursor chain; "
                                       "examples are from this response). These are NOT exact matches and are "
                                       "not counted or returned as matches. They concern the identifier "
                                       "anywhere in the file, whatever filters were given."}
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


class _Window:
    """One page of a directory listing, taken from an unsorted stream of entries without holding them all.

    Keeps only the first `offset + size` entries in name order (descending if `reverse`) that come after
    `start_after`, and counts every entry offered. Memory is bounded by the page, not the directory.
    """

    def __init__(self, size: int, offset: int = 0, start_after: str | None = None, reverse: bool = False):
        self.keep = max(offset, 0) + size
        self.offset = max(offset, 0)
        self.size = size
        self.after = start_after
        self.reverse = reverse
        self.total = 0
        self.remaining = 0  # entries after start_after
        self._items: list[tuple[str, Any]] = []

    def offer(self, name: str, item: Any) -> None:
        self.total += 1
        if self.after is not None and (name >= self.after if self.reverse else name <= self.after):
            return
        self.remaining += 1
        self._items.append((name, item))
        if len(self._items) > 2 * self.keep + 64:
            self._trim()

    def _trim(self) -> None:
        pick = heapq.nlargest if self.reverse else heapq.nsmallest
        self._items = pick(self.keep, self._items, key=lambda pair: pair[0])

    def page(self) -> list[tuple[str, Any]]:
        self._trim()
        return self._items[self.offset: self.offset + self.size]


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

    def list_folders(self, process: str | None = None, offset: int = 0, folder: str | None = None,
                     start_after: str | None = None) -> dict[str, Any]:
        if folder is not None and folder.strip():
            if process:
                raise ToolFailure("invalid_argument", "Pass either process (mapped folders) or folder (its subfolders), not both.")
            return self._list_subfolders(folder, offset, start_after)
        catalog = self.catalog()
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
            entry: dict[str, Any] = {"folder": ref.path, "kind": "mapped_folder"}
            if len(descriptions) == 1:
                entry["description"] = descriptions[0]
            entry["exists_on_disk"] = ref.resolved.is_dir()
            entry["used_by"] = usages
            patterns = [c.pattern for c, _ in ref.conventions()]
            if len(patterns) == 1:
                entry["filename_pattern"] = patterns[0]
            elif patterns:
                entry["filename_patterns"] = patterns
            identifiers: dict[str, str | None] = {}
            for _, folder in ref.usages:
                for column in folder.identifier_columns:
                    identifiers.setdefault(column.column, column.identifier_type)
            if identifiers:
                entry["identifier_columns"] = [
                    {"column": name, "identifier_type": kind} if kind else {"column": name}
                    for name, kind in identifiers.items()]
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
            "note": "Pass a folder's 'folder' value to list_csv_files, or to list_folders as `folder` to see its "
                    "subfolders. Only these mapped folders and their genuine subfolders can be read. "
                    "get_process_map has the rest: what each filename token means, record keys, field "
                    "mappings and filter rules.",
        }
        if next_offset is not None:
            out["next_offset"] = next_offset
        if catalog.load_errors:
            out["process_map_load_errors"] = catalog.load_errors
        return out

    def _list_subfolders(self, folder: str, offset: int, start_after: str | None = None) -> dict[str, Any]:
        """The immediate child directories of an approved folder. One level only: the caller descends."""
        loc = self._folder(folder)
        catalog = self.catalog()
        window = _Window(self.limits.max_files_per_listing, offset, start_after)
        links = unreadable = 0
        complete = True
        with self._dir(loc) as directory:
            try:
                with directory.scandir() as entries:
                    for entry in entries:
                        try:
                            if entry.is_symlink() or getattr(entry, "is_junction", lambda: False)():
                                links += 1  # counted without looking at what it points to
                            elif entry.is_dir(follow_symlinks=False):
                                window.offer(entry.name, None)
                        except OSError:
                            unreadable += 1
            except OSError:
                complete = False  # the listing broke off part-way: what follows is not the whole folder
        budget = self.limits.max_output_bytes - ENVELOPE_RESERVE_BYTES
        page: list[dict[str, Any]] = []
        used = 0
        for name, _ in window.page():
            child = catalog.child(loc.at, name)
            if child is None:  # cannot happen for a real child of an approved folder; never list it if it does
                unreadable += 1
                continue
            place = Location(child)
            entry = {"folder": place.path, "name": name, **place.access()}
            if not place.parts:
                entry["note"] = "Declared in a process map in its own right: its own metadata applies, not its parent's."
            cost = _size(entry) + 4
            if page and used + cost > budget:
                break
            page.append(entry)
            used += cost
        more = window.offset + len(page) < window.remaining
        out: dict[str, Any] = {
            "scope": f"subfolders of '{loc.path}'",
            "parent": {"folder": loc.path, **loc.access()},
            "total_folders": window.total,
            "folders_returned": len(page),
            "folders": page,
            "truncated": more,
            "listing_complete": complete,
            "note": "Immediate subfolders only; call list_folders again with a subfolder to go deeper, or "
                    "list_csv_files to see its files. Nothing outside the approved root can be reached.",
        }
        if more:
            out["next_start_after"] = page[-1]["name"]
            out["next_offset"] = window.offset + len(page)
        if links:
            out["links_not_followed"] = links
        if unreadable:
            out["unreadable_entries"] = unreadable
        if links or unreadable or not complete:
            out["listing_warning"] = (
                "This is not the whole picture: "
                + "; ".join(text for text, on in (
                    (f"{links} link(s) exist here and are never followed", links),
                    (f"{unreadable} entr(ies) could not be examined", unreadable),
                    ("the folder could not be read to the end", not complete)) if on)
                + ". An empty or short list here does not show that nothing else exists.")
        if (any(f["kind"] == "discovered_subfolder" for f in page) and not access_module.HAVE_DIR_FD
                and not self.config.allow_unconfined_subfolders):
            out["listing_warning"] = (out.get("listing_warning", "") + " " if "listing_warning" in out else "") + (
                "On this platform, discovered subfolders cannot be opened with guaranteed confinement, so they "
                "are listed but cannot be opened (subfolder_access_unavailable).")
        return out

    # ------------------------------------------------------------------ files and headers

    def _folder(self, folder: str) -> Location:
        """Resolve a caller's folder reference to an approved root or a place below one.

        This is the only way a folder is ever chosen: the roots come from the process maps, which no
        tool can edit, and anything not spelled inside one of them is refused here.
        """
        catalog = self.catalog()
        located = catalog.locate(folder) if isinstance(folder, str) and folder.strip() else None
        if located is None:
            known = [r.path for r in catalog.folders()]
            raise ToolFailure(
                "folder_not_found",
                f"'{_clip(folder or '', 200)}' is not an approved folder. Only folders described by a process map, "
                "and their genuine subfolders, can be read.",
                closest_known_folders=difflib.get_close_matches((folder or "")[:300], known, n=5, cutoff=0.4),
                hint="Call list_folders for the mapped folders, and list_folders with `folder` for their subfolders.",
            )
        return Location(located)

    def _dir(self, loc: Location) -> ConfinedDir:
        try:
            return ConfinedDir(Path(loc.at.anchor), loc.at.anchor_parts, loc.path, self.config.allow_unconfined_subfolders)
        except AccessError as exc:
            raise ToolFailure(exc.kind, exc.message) from None

    def _open(self, folder: str, filename: str) -> OpenFile:
        """Open one CSV file read-only inside an approved folder. The caller closes `fh`."""
        loc = self._folder(folder)
        if (not isinstance(filename, str) or not filename or len(filename) > 255 or filename in (".", "..")
                or "/" in filename or "\\" in filename or "\x00" in filename):
            raise ToolFailure("invalid_argument", "filename must be a bare file name inside the folder, without any path.")
        if not filename.lower().endswith(".csv"):
            raise ToolFailure(
                "not_a_csv_file",
                f"'{filename}' does not have a .csv extension. Only .csv files are read; compressed or other "
                "files are not searched, so nothing can be concluded about its contents.",
            )
        fmt, conflict = loc.ref.csv_format()
        if conflict:
            raise ToolFailure(
                "process_map_conflict",
                f"The process maps disagree about the CSV format of folder '{loc.ref.path}' ({conflict}). The files "
                "cannot be read reliably until the maps agree.",
            )
        with self._dir(loc) as directory:
            try:
                fd, st = directory.open_file(filename)
            except FileNotFoundError:
                try:
                    with directory.scandir() as entries:
                        names = [e.name for e in entries if e.name.lower().endswith(".csv")][:5000]
                except OSError:
                    names = []
                raise ToolFailure(
                    "file_not_found",
                    f"No file named '{filename}' in folder '{loc.path}'.",
                    closest_filenames=difflib.get_close_matches(filename, names, n=3, cutoff=0.6),
                    hint="Call list_csv_files to see the files that exist.",
                ) from None
            except AccessError as exc:
                raise ToolFailure(exc.kind, exc.message) from None
            except OSError as exc:
                raise ToolFailure("file_unreadable", f"Cannot access '{filename}' in '{loc.path}': {exc.strerror or exc}.") from exc
        try:
            if hasattr(os, "set_blocking"):
                os.set_blocking(fd, True)
            fh = os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise
        return OpenFile(loc, filename, fmt, st.st_size, st.st_mtime_ns, st.st_ctime_ns, (st.st_dev, st.st_ino), fh)

    def _changed(self, of: OpenFile) -> bool:
        """Whether the file's content may differ from what was opened: written to, or replaced under its name.

        Change time is deliberately not part of this: a chmod, a rename elsewhere or a backup tool adding a
        hard link changes it without touching the content, and must not spoil a correct answer. It is
        recorded instead (end_ctime_ns) and binds the cursor, so a later page still notices.
        """
        try:
            st = os.fstat(of.fh.fileno())
            of.end_ctime_ns = st.st_ctime_ns
            if (st.st_size, st.st_mtime_ns) != (of.size, of.mtime_ns):
                return True
            with self._dir(of.loc) as directory:
                now = directory.lstat(of.name)
            return (now.st_dev, now.st_ino) != of.identity
        except (OSError, ValueError, ToolFailure):
            return True

    def _header(self, of: OpenFile, control: ScanControl) -> Header:
        """Read the header row, under the caller's time limit and cancellation."""
        fh = of.fh
        probe = ScanControl(max_seconds=control.max_seconds, stop=control.stop)
        control.begin()
        probe.started = control.started
        try:
            fh.seek(0)
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

    def _column(self, header: Header, spec: Any, of: OpenFile, what: str = "column") -> int:
        """Resolve a column selector. One contract for every tool and every argument that names a column:

        - {"name": "..."}: the column with exactly this header text; never read as a position or label.
        - {"position": N}: the Nth column, 1-based.
        - a plain string: the exact name, or the '#N' positional shorthand, or a label as shown in
          returned rows ('Price [col 3]'). If those readings pick different columns the request is
          refused rather than guessed, and the explicit forms settle it.
        """
        def missing(text: str, **extra: Any) -> ToolFailure:
            return ToolFailure("column_not_found", text, **extra,
                               hint="Use search_column_names or inspect_csv_headers to find the right column.")

        def repeated(name: str, where: list[int]) -> ToolFailure:
            return ToolFailure(
                "ambiguous_column",
                f"Column name '{_clip(name, 80)}' appears {len(where)} times in '{of.name}' (first positions "
                f"{[m + 1 for m in where[:8]]}). Select one by position, e.g. {{\"position\": {where[0] + 1}}} "
                f"or '#{where[0] + 1}'.")

        if hasattr(spec, "model_dump"):
            spec = spec.model_dump(exclude_none=True)
        total = len(header.columns)
        if isinstance(spec, dict):
            keys = {k for k, v in spec.items() if v is not None}
            if keys == {"position"} and isinstance(spec["position"], int) and not isinstance(spec["position"], bool):
                if 1 <= spec["position"] <= total:
                    return spec["position"] - 1
                raise missing(f"'{of.name}' has {total} column(s); there is no position {spec['position']}.")
            if keys == {"name"} and isinstance(spec["name"], str):
                named = [i for i, name in enumerate(header.columns) if name == spec["name"]]
                if len(named) == 1:
                    return named[0]
                if named:
                    raise repeated(spec["name"], named)
                spec = spec["name"]  # fall through for the similar-names help only
                named_only = True
            else:
                raise ToolFailure("invalid_argument", f"A {what} selector is a column name, or exactly one of "
                                                      "{\"name\": \"...\"} or {\"position\": N}.")
        elif isinstance(spec, str):
            named_only = False
        else:
            raise ToolFailure("invalid_argument", f"A {what} selector is a column name, or exactly one of "
                                                  "{\"name\": \"...\"} or {\"position\": N}.")
        if not named_only:
            named = [i for i, name in enumerate(header.columns) if name == spec]
            if len(named) > 1:
                raise repeated(spec, named)
            readings: dict[int, str] = {i: f"the name of column {i + 1}" for i in named}
            if POSITION_SELECTOR.fullmatch(spec) and int(spec[1:]) <= total:
                readings.setdefault(int(spec[1:]) - 1, f"the positional shorthand for column {int(spec[1:])}")
            for i, label in enumerate(header.labels):
                if label == spec and label != header.columns[i]:
                    readings.setdefault(i, f"the label shown for column {i + 1}")
            if len(readings) == 1:
                return next(iter(readings))
            if readings:
                raise ToolFailure(
                    "ambiguous_column",
                    f"'{_clip(spec, 80)}' can be read as {' or as '.join(readings.values())} in '{of.name}'. Say "
                    f"which: {{\"name\": {json.dumps(_clip(spec, 80))}}} for the column with that header text, or "
                    "{\"position\": N} for a position.")
            if POSITION_SELECTOR.fullmatch(spec):
                raise missing(f"'{of.name}' has {total} column(s); there is no position {spec[1:]}.")
        folded = [header.display(i) for i, name in enumerate(header.columns)
                  if name.strip().casefold() == spec.strip().casefold()]
        close = difflib.get_close_matches(spec, [n for n in header.columns if len(n) <= LABEL_MAX_CHARS], n=5, cutoff=0.6)
        raise missing(f"'{of.name}' has no column named '{_clip(spec, 80)}' (column names are matched exactly).",
                      similar_column_names=list(dict.fromkeys(folded[:5] + close)))

    def _columns(self, header: Header, specs: Any, of: OpenFile, what: str) -> list[int]:
        if not isinstance(specs, (list, tuple)):
            raise ToolFailure("invalid_argument", f"{what} must be a list of column selectors.")
        return [self._column(header, spec, of, what) for spec in specs]

    def _filters(self, header: Header, filters: Any, of: OpenFile, what: str = "filters") -> list[tuple[int, str]]:
        """Resolve exact-match record filters to (column index, value), in canonical order."""
        if not filters:
            return []
        if not isinstance(filters, (list, tuple)):
            raise ToolFailure("invalid_argument", f"{what} must be a list of {{\"column\": ..., \"value\": \"...\"}} objects.")
        if len(filters) > MAX_FILTERS:
            raise ToolFailure("invalid_argument", f"At most {MAX_FILTERS} {what} are supported.")
        resolved: dict[int, str] = {}
        for item in filters:
            if hasattr(item, "model_dump"):
                item = item.model_dump()
            if not isinstance(item, dict) or set(item) != {"column", "value"} or not isinstance(item["value"], str):
                raise ToolFailure("invalid_argument", f"Each of {what} must be {{\"column\": <selector>, \"value\": "
                                                      "<string>}; the value is text, compared exactly.")
            if len(item["value"]) > MAX_FILTER_VALUE_CHARS:
                raise ToolFailure("invalid_argument", f"A filter value is longer than {MAX_FILTER_VALUE_CHARS} characters.")
            index = self._column(header, item["column"], of, "filter column")
            if index in resolved:
                raise ToolFailure(
                    "invalid_argument",
                    f"{what} names column '{_clip(header.label(index), 80)}' (position {index + 1}) more than once. "
                    "Give each column one value: a field cannot equal two values, and one of them is not chosen for you.")
            resolved[index] = item["value"]
        return sorted(resolved.items())

    @staticmethod
    def _describe_filters(header: Header, filters: list[tuple[int, str]]) -> list[dict[str, Any]]:
        """Every effective predicate, bounded for display; shortening is flagged, never silent."""
        out = []
        for index, value in filters:
            item: dict[str, Any] = {"column": _clip(header.label(index), 80), "position": index + 1,
                                    "value": value[:80]}
            if len(value) > 80:
                item["value_shortened_for_display"] = True
                item["value_length"] = len(value)
            out.append(item)
        return out

    def list_csv_files(self, folder: str, filename_contains: str | None = None, date_from: str | None = None,
                       date_to: str | None = None, date_field: str | None = None, offset: int = 0,
                       newest_first: bool = True, start_after: str | None = None) -> dict[str, Any]:
        loc = self._folder(folder)
        ref = loc.ref
        conventions = ref.conventions()
        lo = self._date_bound(date_from, "date_from")
        hi = self._date_bound(date_to, "date_to")
        field = None
        if lo or hi:
            valid = ["filesystem_modified"] + ref.date_tokens()
            defaults = ref.default_date_tokens()
            if date_field is None and len(defaults) > 1:
                raise ToolFailure(
                    "ambiguous_date_field",
                    f"The process maps give this folder several filename conventions whose main dates differ "
                    f"({defaults}). Say which date to filter on with date_field: one of {valid}.")
            field = date_field or (defaults[0] if defaults else None)
            if field not in valid:
                raise ToolFailure(
                    "invalid_argument",
                    f"date_field must be one of {valid} for this folder." if field else
                    "This folder has no filename convention with a date token, so filename dates cannot be filtered. "
                    "Use date_field='filesystem_modified' or filename_contains instead.",
                )

        files = _Window(self.limits.max_files_per_listing, offset, start_after, reverse=newest_first)
        ignored = subfolders = unparseable = ambiguous = 0
        complete = True
        needle = filename_contains.casefold() if filename_contains else None
        with self._dir(loc) as directory:
            try:
                entries = directory.scandir()
            except OSError as exc:
                raise ToolFailure("folder_unreadable", f"Cannot list folder '{loc.path}': {exc.strerror or exc}.") from exc
            try:
                with entries:
                    for entry in entries:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                subfolders += 1
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
                        item: dict[str, Any] = {
                            "filename": entry.name,
                            "size_bytes": st.st_size,
                            "filesystem_modified_time": _iso(st.st_mtime),
                        }
                        parsed = ref.parse_filename(entry.name) if conventions else None
                        if parsed:
                            item["filename_tokens"] = parsed["tokens"]
                            item["matches_filename_convention"] = parsed["status"] != "unparsed"
                            item["filename_status"] = parsed["status"]
                            if parsed["status"] == "ambiguous":
                                item["filename_interpretations"] = parsed["interpretations"][:4]
                        if field == "filesystem_modified":
                            value: str | None = _iso(st.st_mtime)
                        elif field:
                            # The date is usable when every convention that reads this name agrees on it.
                            readings = ({i["tokens"][field] for i in parsed["interpretations"] if field in i["tokens"]}
                                        if parsed else set())
                            if len(readings) > 1:
                                ambiguous += 1
                                continue
                            value = next(iter(readings), None)
                            if value is None:
                                unparseable += 1
                                continue
                        if field and ((lo and value[:10] < lo) or (hi and value[:10] > hi)):
                            continue
                        files.offer(entry.name, item)
            except OSError:
                complete = False

        budget = self.limits.max_output_bytes - ENVELOPE_RESERVE_BYTES
        page: list[dict[str, Any]] = []
        used = 0
        for _, item in files.page():
            cost = _size(item) + 4
            if page and used + cost > budget:
                break
            page.append(item)
            used += cost
        more = files.offset + len(page) < files.remaining
        out: dict[str, Any] = {
            "folder": loc.path,
            "filters": {"filename_contains": filename_contains, "date_from": lo, "date_to": hi, "date_field": field},
            "total_matching_files": files.total,
            "files_returned": len(page),
            "files": page,
            "truncated": more,
            "sorted_by": "filename, " + ("descending" if newest_first else "ascending"),
            "time_fields": {
                "filesystem_modified_time": "When the file was last written on disk (UTC). Not a business date: "
                                            "copies, reruns and backfills change it.",
            },
        }
        if loc.parts:
            out["folder_access"] = loc.access()
        if conventions:
            described = []
            for convention, owners in conventions:
                entry_: dict[str, Any] = {"pattern": convention.pattern, "declared_by": owners}
                if convention.notes:
                    entry_["notes"] = convention.notes
                described.append(entry_)
            if len(described) == 1:
                out["filename_convention"] = {k: v for k, v in described[0].items() if k != "declared_by"}
            else:
                out["filename_conventions"] = described
                out["filename_conventions_note"] = (
                    "Several processes describe this folder's filenames. Each file is read with every convention: "
                    "filename_status says whether exactly one reading resulted (parsed), none (unparsed) or "
                    "conflicting ones (ambiguous).")
            if loc.parts:
                out["filename_conventions_inherited_from"] = ref.path
            meanings: dict[str, str] = {}
            for convention, _ in conventions:
                for name, token in convention.tokens.items():
                    if meanings.setdefault(name, token.meaning) != token.meaning:
                        meanings[name] += f" / {token.meaning}"
            out["time_fields"]["filename_tokens"] = meanings
        if more:
            out["next_start_after"] = page[-1]["filename"]
            out["next_offset"] = files.offset + len(page)
        if ignored:
            out["other_entries_ignored"] = ignored
            out["other_entries_note"] = "Entries that are not regular .csv files (other types, links) are not listed or read."
        if subfolders:
            out["subfolders"] = subfolders
            out["subfolders_note"] = ("This lists only files directly in this folder. Its subfolders are not searched; "
                                      "see them with list_folders(folder=...).")
        if unparseable:
            out["files_excluded_because_filename_has_no_parseable_date"] = unparseable
        if ambiguous:
            out["files_excluded_because_filename_date_is_ambiguous"] = ambiguous
        if unparseable or ambiguous:
            out["date_exclusion_note"] = ("These files were left out because their date could not be established, "
                                          "NOT because it is outside the requested range. List without date "
                                          "filters to see them.")
        if not complete:
            out["listing_complete"] = False
            out["listing_warning"] = "The folder could not be read to the end, so files may be missing from this listing."
        return out

    @staticmethod
    def _date_bound(value: str | None, name: str) -> str | None:
        if not value:
            return None
        try:
            return date.fromisoformat(value.strip()[:10]).isoformat()
        except ValueError:
            raise ToolFailure("invalid_argument", f"{name} must be an ISO date (YYYY-MM-DD), got '{value}'.") from None

    def _quick_control(self) -> ScanControl:
        """For direct callers that pass no control: the server's ordinary time limit, never an unlimited read."""
        return ScanControl(max_seconds=self.limits.max_scan_seconds)

    def _awkward_columns(self, header: Header, start: int, stop: int, max_bytes: int) -> list[dict[str, Any]]:
        """Columns in [start, stop) whose plain name would not select them, with a selector that does."""
        out: list[dict[str, Any]] = []
        used = 0
        for i in range(start, stop):
            if header.plain_name_is_selector(i):
                continue
            item = {"position": i + 1, "column": _clip(header.display(i), 80), "selector": header.selector(i)}
            used += _size(item) + 4
            if used > max_bytes:
                break
            out.append(item)
        return out

    def inspect_csv_headers(self, folder: str, filename: str, sample_rows: int = 0, column_offset: int = 0,
                            sample_columns: list[Any] | None = None,
                            control: ScanControl | None = None) -> dict[str, Any]:
        control = control or self._quick_control()
        control.begin()
        control.phase = "opening"
        if isinstance(sample_rows, bool) or not isinstance(sample_rows, int) or not 0 <= sample_rows <= self.limits.max_sample_rows:
            raise ToolFailure("invalid_argument", f"sample_rows must be between 0 and {self.limits.max_sample_rows}.")
        of = self._open(folder, filename)
        with of.fh as fh:
            control.total_bytes = of.size
            control.phase = "reading the header"
            header = self._header(of, control)
            total = len(header.columns)
            out: dict[str, Any] = {"file": of.info(), "column_count": total}
            duplicates = header.duplicates()
            if duplicates:
                out["duplicate_column_names"] = duplicates
            layout = self._layout_warnings(header, of)
            if layout:
                out["warnings"] = layout
            budget = self.limits.max_output_bytes - RESULT_ENVELOPE_BYTES - _size(out) - 2600
            start = min(max(column_offset, 0), total)
            names = _some([header.display(i) for i in range(start, total)], budget // 2 if sample_rows else budget)
            used = _size(names)
            out["columns"] = names
            out["columns_truncated"] = start + len(names) < total
            out["column_selection"] = (
                f"columns[i] is at position {start + 1}+i. Select a column anywhere by its exact name, or "
                "unambiguously as {\"position\": N} or {\"name\": \"...\"}.")
            awkward = self._awkward_columns(header, start, start + len(names), 1200)
            if awkward:
                out["columns_needing_a_position_selector"] = awkward
            if start:
                out["column_offset"] = start
            if out["columns_truncated"]:
                out["next_column_offset"] = start + len(names)
                out["truncation_note"] = "Not all column names fit. Continue with column_offset, or use search_column_names."
            if sample_rows:
                wanted = self._columns(header, sample_columns, of, "sample_columns") if sample_columns else None
                rows: list[dict[str, Any]] = []
                room = budget - used - _size(awkward)
                truncated = False
                control.phase = "sampling"
                control.stop_reason = None
                stopped: str | None = None
                try:
                    for record in iter_records(fh, delimiter=of.fmt.delimiter, encoding=of.fmt.encoding,
                                               start_offset=header.data_offset, start_record=1, control=control,
                                               chunk_bytes=64 * 1024, max_record_bytes=self.limits.max_record_bytes):
                        if not record.fields:
                            continue  # a blank line is not a sample row, but reading it is work under the time limit
                        share = max(room // (sample_rows - len(rows)), 600)
                        row = self._fit(self._row(header, record, wanted), share)
                        cost = _size(row) + 4
                        if cost > room:
                            truncated = True
                            stopped = "output_size_limit"
                            break
                        truncated = truncated or "values_omitted_for_size" in row
                        room -= cost
                        rows.append(row)
                        if len(rows) >= sample_rows:
                            stopped = "requested_rows_collected"
                            break
                    stopped = stopped or control.stop_reason
                except CsvParseError as exc:
                    stopped = "error"
                    out["sample_error"] = _clip(f"Sampling stopped: {exc}", 400)
                except OSError as exc:
                    stopped = "error"
                    out["sample_error"] = f"Sampling stopped: reading failed part-way: {exc.strerror or exc}."
                out["sample_rows"] = rows
                out["sample_rows_requested"] = sample_rows
                out["sample_stop_reason"] = stopped
                # Complete means: the rows asked for, or every data record the file has.
                out["sample_complete"] = stopped in ("requested_rows_collected", "end_of_file")
                note = (f"The first {len(rows)} data record(s) only: a layout sample, not evidence "
                        "about any security.")
                if stopped == "end_of_file" and len(rows) < sample_rows:
                    note += f" The file has only {len(rows)} data record(s)."
                elif not out["sample_complete"]:
                    note = (f"PARTIAL SAMPLE: {len(rows)} of the {sample_rows} requested row(s) were collected before "
                            f"sampling stopped ({STOP_EXPLANATIONS.get(stopped or '', 'see sample_error')}). The file "
                            "may hold more records. " + note)
                out["sample_note"] = note
                if truncated:
                    out["sample_truncated"] = True
                if control.undecodable_lines:
                    out["sample_warning"] = self._undecodable_warning(of)
        return out

    def search_column_names(self, folder: str, filename: str, term: str,
                            control: ScanControl | None = None) -> dict[str, Any]:
        control = control or self._quick_control()
        control.begin()
        control.phase = "opening"
        if not isinstance(term, str) or not term.strip():
            raise ToolFailure("invalid_argument", "term must not be empty.")
        of = self._open(folder, filename)
        with of.fh:
            control.total_bytes = of.size
            control.phase = "reading the header"
            header = self._header(of, control)
        wanted = term.strip().casefold()
        hits = [{"column": header.display(i), "position": i + 1, "selector": header.selector(i)}
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
        budget = self.limits.max_output_bytes - RESULT_ENVELOPE_BYTES - _size(out) - 2400
        page: list[dict[str, Any]] = []
        for hit in hits:
            budget -= _size(hit) + 4
            if budget < 0:
                break
            page.append(hit)
        out["matching_columns"] = page
        out["truncated"] = len(page) < len(hits)
        out["selector_note"] = "Pass a column's 'selector' value wherever a tool takes a column."
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
        absent: list[str] = []
        for i in indices:
            label = header.label(i)  # unique for every position, inside the header or beyond it
            if i >= len(fields):
                absent.append(label)
                continue
            values[label], was_cut = self._cell(fields[i])
            if was_cut:
                cut.append(label)
        row: dict[str, Any] = {"record_number": record.number, "byte_offset": record.offset}
        if matched is not None:
            names = [header.label(i) for i in matched]
            row["matched_columns"] = _some(names, 2400)
            if len(row["matched_columns"]) < len(names):
                row["matched_column_count"] = len(names)
        row["values"] = values
        if cut:
            row["truncated_cells"] = _some(cut, 1200)
            if len(row["truncated_cells"]) < len(cut):
                row["truncated_cell_count"] = len(cut)
        if absent:
            # A requested column the record is too short to have: missing, which is not the same as empty.
            row["requested_columns_missing_from_record"] = _some(absent, 1200)
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
        used = _size({k: v for k, v in row.items() if k != "values"}) + 520  # the notes added below
        first_cut: str | None = None
        for label, value in values.items():
            cost = _size(label) + _size(value) + 8
            if used + cost > budget:
                room = (budget - used - _size(label) - 160) // 8
                if not kept and room > 40:  # show at least the start of the first value
                    kept[label] = f"{value[:room]}...[cut to fit; {len(value)} characters in total]"
                    first_cut = label
                break
            used += cost
            kept[label] = value
        row["values"] = kept
        row["values_omitted_for_size"] = len(values) - len(kept)
        row["size_note"] = ("This record is too wide for the output-size limit; trailing columns were left out. "
                            "Request specific columns to see them.")
        room = len(kept.get(first_cut, "")) if first_cut else 0
        while first_cut and _size(row) > budget and room > 40:  # heavily escaped text costs more than estimated
            room //= 2
            kept[first_cut] = f"{values[first_cut][:room]}...[cut to fit; {len(values[first_cut])} characters in total]"
        return row

    # ------------------------------------------------------------------ cursors

    def _fingerprint(self, tool: str, of: OpenFile, identifier: str, column: int | None,
                     filters: list[tuple[int, str]]) -> str:
        """Binds a cursor to one search of one physical file in one state.

        Size and modification time alone do not identify a file: a replacement can match both. The
        device and inode (the file's identity) are part of the binding, so a file swapped in under the same
        name no longer accepts the old cursor. The change time is checked separately when a cursor is used
        (see _decode_cursor), which catches a rewrite in place that kept the size and timestamp.
        """
        blob = json.dumps([tool, of.loc.at.anchor, list(of.loc.at.anchor_parts), of.name, of.size, of.mtime_ns,
                           list(of.identity), of.fmt.delimiter, of.fmt.encoding, identifier, column,
                           [list(f) for f in filters]])
        return hashlib.sha256(blob.encode()).hexdigest()[:24]

    def _encode_cursor(self, state: dict[str, Any]) -> str:
        body = json.dumps(state, separators=(",", ":")).encode()
        mac = hmac.new(self._cursor_key, body, hashlib.sha256).digest()[:12]
        return base64.urlsafe_b64encode(mac + body).decode()

    def _decode_cursor(self, cursor: str, fingerprint: str, of: OpenFile) -> dict[str, Any]:
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
        if state.get("c") != of.ctime_ns:
            raise ToolFailure(
                "invalid_cursor",
                f"'{of.name}' has the same size and modification time as when the cursor was issued, but its "
                "metadata has changed since (its permissions or links changed, or it was rewritten in place "
                "keeping its timestamp). Earlier pages cannot safely be combined with this one. Start the "
                "search again without a cursor.",
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

    def _integrity(self, control: ScanControl, of: OpenFile, header: Header, identifier: str,
                   carried: dict[str, Any] | None = None) -> tuple[list[str], bool, bool, dict[str, Any]]:
        """Warnings about the file, whether a complete scan of it can be trusted, whether it changed, and
        the integrity state a cursor must carry forward.

        `carried` is that state from the earlier responses of a cursor chain. What one page saw holds for
        the whole search: a clean final page never erases an earlier problem.
        """
        carried = carried or {}
        warnings: list[str] = self._layout_warnings(header, of, identifier)
        changed = self._changed(of)
        if changed:
            warnings.append("The file was modified or replaced while it was being scanned (it may still be being "
                            "written), so the scan may have missed or misread records.")
        if control.unterminated_quote_at_eof:
            warnings.append("The file ends inside a quoted field (unterminated quote): the final record is "
                            "incomplete and records may be hidden inside it.")
        alt_hits = sorted(set(control.alt_hits) | set(carried.get("a", [])))
        if alt_hits:
            warnings.append(
                f"The identifier's bytes were found in the file as {' / '.join(alt_hits)} text, but the "
                f"folder is declared as {of.fmt.encoding}. The declared encoding is probably wrong for this file, so "
                "values with non-ASCII characters cannot be matched reliably.")
        sound = not warnings
        if not changed and of.end_ctime_ns is not None and of.end_ctime_ns != of.ctime_ns:
            # Informational only: the content did not change size or timestamp, so the answer stands.
            warnings.append("The file's metadata changed while it was being scanned (permissions, links, or a "
                            "rewrite that kept its size and timestamp). Its size and modification time did not "
                            "change, so this result stands, but a next_cursor from this response will only be "
                            "accepted while the file stays as it is now.")
        # A flag, not a count: a record looked at twice (once as look-ahead) must not be counted twice.
        undecodable = bool(control.undecodable_lines or carried.get("u"))
        if undecodable:
            warnings.append(self._undecodable_warning(of))
        state: dict[str, Any] = {}
        if alt_hits:
            state["a"] = alt_hits
        if undecodable:
            state["u"] = 1
        return warnings, sound, changed, state

    def _iter_matches(self, of: OpenFile, header: Header, identifier: str, column: int | None,
                      start_offset: int, start_record: int, control: ScanControl, hints: _Hints,
                      filters: list[tuple[int, str]] | None = None) -> Iterator[tuple[Record, list[int]]]:
        """Yield (record, indices of matching fields) for exact matches, in file order.

        With filters, a record is yielded only when the identifier matches AND every filter column of
        that same record equals its value exactly. A field the record is too short to have equals
        nothing, not even the empty string. Filtering happens here, during the scan, so records that
        fail it are never collected or counted as matches.
        """
        filters = filters or []
        control.alt_needles = self._alt_needles(identifier, of.fmt.encoding)
        control.alt_hits = set()
        control.undecodable_lines = 0
        control.unterminated_quote_at_eof = False
        for record in iter_records(
            of.fh, delimiter=of.fmt.delimiter, encoding=of.fmt.encoding,
            needle=prefilter_needle(identifier, of.fmt.encoding),
            start_offset=start_offset, start_record=start_record, control=control,
            chunk_bytes=self.limits.chunk_bytes, max_record_bytes=self.limits.max_record_bytes,
        ):
            fields = record.fields
            count = len(fields)
            if column is not None:
                value = fields[column] if column < count else None
                if value == identifier:
                    if all(i < count and fields[i] == wanted for i, wanted in filters):
                        control.matches += 1
                        yield record, [column]
                    else:
                        hints.failing_filters += 1
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
                if all(i < count and fields[i] == wanted for i, wanted in filters):
                    control.matches += 1
                    yield record, found
                else:
                    hints.failing_filters += 1
            else:
                for i, value in enumerate(fields):
                    if identifier in value:
                        hints.value(record, header.label(i), value)

    def _search(self, tool: str, control: ScanControl, folder: str, filename: str, identifier: str,
                column: Any, columns: list[Any] | None, max_rows: int | None,
                cursor: str | None, *, count_only: bool = False, filters: Any = None) -> dict[str, Any]:
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
        limit = self.limits.max_output_bytes - RESULT_ENVELOPE_BYTES
        search: dict[str, Any] = {"identifier": identifier, "match_type": MATCH_TYPE}
        result: dict[str, Any] = {"file": of.info(), "search": search}
        with of.fh:
            header = self._header(of, control)
            column_index = None
            if tool != "search_across_columns":
                column_index = self._column(header, "" if column is None else column, of, "identifier column")
                search["column"] = header.label(column_index)
            else:
                search["columns_searched"] = f"all {len(header.columns)} columns"
            predicates = self._filters(header, filters, of)
            if predicates:
                search["filters"] = self._describe_filters(header, predicates)
                search["scope"] = ("Only records where the identifier matches AND every filter column equals its "
                                   "value exactly (whole field, case-sensitive, as text) in that same record. "
                                   "Nothing here says whether the identifier occurs elsewhere in the file.")
            wanted = None
            if columns and not count_only:
                wanted = self._columns(header, columns, of, "columns")
                names = [header.label(i) for i in wanted]
                shown = _some(names, 1600)
                search["columns_returned"] = shown if len(shown) == len(names) else {"count": len(names), "first": shown}
            elif not count_only:
                search["columns_returned"] = "all"

            fingerprint = self._fingerprint(tool, of, identifier, column_index, predicates)
            start_offset, start_record, before = header.data_offset, 1, 0
            carried: dict[str, Any] = {}
            hints = _Hints(identifier)
            if cursor:
                state = self._decode_cursor(cursor, fingerprint, of)
                start_offset, start_record, before = state["o"], state["r"], state["m"]
                carried = state.get("i", {})
                hints.restore(state.get("h", {}))
                search["resumed_from_cursor"] = True
                search["matches_in_earlier_responses"] = before
            if not count_only:
                search["max_rows"] = row_limit

            control.total_bytes = of.size
            control.phase = "scanning"
            rows: list[dict[str, Any]] = []
            found = 0
            # Rows get whatever is left after the request echo and a reserve for the scan report.
            budget = max(limit - SCAN_RESERVE_BYTES - _size(result), 800)
            used = 0
            stop_reason: str | None = None
            resume: tuple[int, int] | None = None
            error: ToolFailure | None = None
            try:
                for record, hits in self._iter_matches(of, header, identifier, column_index,
                                                       start_offset, start_record, control, hints, predicates):
                    if count_only:
                        found += 1
                        continue
                    # A record that does not fit is left where it is: the cursor resumes AT it, so no
                    # match is ever consumed without being returned.
                    if len(rows) >= row_limit:
                        stop_reason, resume = "row_limit", (record.offset, record.number - 1)
                        break
                    row = self._row(header, record, wanted, hits if column_index is None else None)
                    cost = _size(row) + 4
                    if used + cost > budget:
                        if rows:
                            stop_reason, resume = "output_size_limit", (record.offset, record.number - 1)
                            break
                        row = self._fit(row, budget)
                        cost = _size(row) + 4
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
            warnings, sound, changed, integrity_state = self._integrity(control, of, header, identifier, carried)

        # iter_records never stops on the time limit before it has passed at least one record, and the row
        # and size limits stop only after returning one, so every cursor issued points past where this began.
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
            result["scan"]["note"] = ("Together with the earlier responses in this cursor chain, the whole file has "
                                      "been scanned. Warnings and near misses seen by earlier responses still apply "
                                      "and are repeated here.")
        if carried:
            result["scan"]["integrity_problems_seen_in_earlier_responses"] = True

        where = f"column '{_clip(search['column'], 80)}'" if column_index is not None else "any column"
        scoped = bool(predicates)
        if scoped:
            where += f" together with the {len(predicates)} filter(s)"
        if total:
            outcome = "match_found"
        elif trustworthy:
            outcome = "no_match_within_filters" if scoped else "confirmed_absent"
        else:
            outcome = "inconclusive"
        result["outcome"] = outcome

        can_resume = error is None and not reached_end and resume is not None and not changed
        next_cursor = None
        if can_resume:
            state = {"fp": fingerprint, "c": of.end_ctime_ns, "o": resume[0], "r": resume[1], "m": total}
            if integrity_state:
                state["i"] = integrity_state
            if hints.state():
                state["h"] = hints.state()
            next_cursor = self._encode_cursor(state)
        why = error.message if error else STOP_EXPLANATIONS.get(stop_reason or "", "the scan stopped early")
        if reached_end:
            why = "the file could not be read reliably (see warnings)"
        aside = ""
        if not total and hints:
            aside = f" Not exact matches, but worth checking: {hints.sentence()} (see near_misses)."
        elif not total and reached_end:
            aside = " No near misses were seen either."
        if scoped and not total:
            if hints.failing_filters:
                aside += (f" The identifier itself matched in {hints.failing_filters} record(s) "
                          f"{'of the file' if reached_end else 'scanned so far'} that did not satisfy the filters.")
            elif trustworthy:
                aside += " The identifier did not match in any record outside the filters either."
        if records_after_header == 0:
            aside += " Note: the file has no data records, only a header."
        if scoped:
            result["identifier_matches_failing_filters"] = {
                "count": hints.failing_filters,
                "count_is_exact": trustworthy and error is None,
                "meaning": "Records where the identifier matched but at least one filter did not. They are "
                           "outside the search scope: not returned and not counted as matches.",
            }

        if count_only:
            exact = trustworthy and error is None
            result["count"] = total if exact else None
            result["count_is_exact"] = exact
            if exact:
                result["explanation"] = f"Complete scan: exactly {total} record(s) have '{shown_id}' in {where}." + aside
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
                            + (", including earlier responses" if before else "") + ")."
                            + (" They are the records within the filters, not every record for the identifier."
                               if scoped else ""))
                elif more == "yes":
                    text = (f"PARTIAL RESULTS: {len(rows)} matching record(s) returned and at least one more exists, "
                            f"because {why}. Pass next_cursor to retrieve further matches.")
                else:
                    text = (f"PARTIAL RESULTS: the scan is incomplete or unreliable because {why}. The {len(rows)} "
                            "record(s) returned are only the matches that could be read; there may be more."
                            + (" Pass next_cursor to continue." if next_cursor else ""))
            elif outcome == "confirmed_absent":
                text = f"Complete scan: no record has '{shown_id}' as the exact value of {where}." + aside
            elif outcome == "no_match_within_filters":
                text = (f"Complete scan: no record has '{shown_id}' as the exact value of {where}. This is a "
                        "statement about the filtered scope only, NOT that the identifier is absent from the file."
                        + aside)
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
            if key not in result and _size(result) + _size(value) + 2 * len(key) + 12 <= limit:
                result[key] = value
        return result

    def find_security_in_file(self, control: ScanControl, folder: str, filename: str, identifier: str,
                              identifier_column: Any, columns: list[Any] | None = None,
                              max_rows: int | None = None, cursor: str | None = None,
                              filters: Any = None) -> dict[str, Any]:
        return self._search("find_security_in_file", control, folder, filename, identifier,
                            identifier_column, columns, max_rows, cursor, filters=filters)

    def search_across_columns(self, control: ScanControl, folder: str, filename: str, identifier: str,
                              columns: list[Any] | None = None, max_rows: int | None = None,
                              cursor: str | None = None, filters: Any = None) -> dict[str, Any]:
        return self._search("search_across_columns", control, folder, filename, identifier,
                            None, columns, max_rows, cursor, filters=filters)

    def count_matching_rows(self, control: ScanControl, folder: str, filename: str, identifier: str,
                            identifier_column: Any, cursor: str | None = None,
                            filters: Any = None) -> dict[str, Any]:
        return self._search("count_matching_rows", control, folder, filename, identifier,
                            identifier_column, None, None, cursor, count_only=True, filters=filters)

    # ------------------------------------------------------------------ comparison

    def _mapping(self, head_a: Header, head_b: Header, of_a: OpenFile, of_b: OpenFile,
                 column_mapping: Any) -> dict[int, int]:
        """Resolve column_mapping to {file A column index: file B column index}. Both ends go through the
        common selector, and every entry must resolve: nothing the caller asked for is quietly dropped."""
        if not column_mapping:
            return {}
        if isinstance(column_mapping, dict):
            entries = list(column_mapping.items())
        elif isinstance(column_mapping, (list, tuple)):
            entries = []
            for item in column_mapping:
                if hasattr(item, "model_dump"):
                    item = item.model_dump(exclude_none=True)
                if not isinstance(item, dict) or set(item) != {"a", "b"}:
                    raise ToolFailure("invalid_argument", "Each column_mapping entry must be {\"a\": <file A column "
                                                          "selector>, \"b\": <file B column selector>}.")
                entries.append((item["a"], item["b"]))
        else:
            raise ToolFailure("invalid_argument", "column_mapping must be an object of file A name -> file B name, "
                                                  "or a list of {\"a\": ..., \"b\": ...} selector pairs.")
        if len(entries) > 2000:
            raise ToolFailure("invalid_argument", "column_mapping has more than 2000 entries.")
        resolved: dict[int, int] = {}
        for spec_a, spec_b in entries:
            a = self._column(head_a, spec_a, of_a, "column_mapping (file A side)")
            b = self._column(head_b, spec_b, of_b, "column_mapping (file B side)")
            if a in resolved:
                raise ToolFailure("invalid_argument", f"column_mapping maps file A column '{_clip(head_a.label(a), 80)}' "
                                                      f"(position {a + 1}) more than once.")
            resolved[a] = b
        return resolved

    def compare_security_records(self, control: ScanControl, folder_a: str, filename_a: str, folder_b: str,
                                 filename_b: str, identifier: str, identifier_column_a: Any,
                                 identifier_column_b: Any = None, identifier_b: str | None = None,
                                 compare_columns: list[Any] | None = None,
                                 column_mapping: Any = None,
                                 match_keys: list[Any] | None = None,
                                 filters_a: Any = None, filters_b: Any = None) -> dict[str, Any]:
        """Find one security in two files and compare it: plan the columns, scan both files, conclude."""
        control.begin()
        control.phase = "opening"
        identifier_b = identifier if identifier_b is None else identifier_b
        self._check_identifier(identifier)
        self._check_identifier(identifier_b)
        if match_keys and len(match_keys) > MAX_MATCH_KEYS:
            raise ToolFailure("invalid_argument", f"At most {MAX_MATCH_KEYS} match_keys are supported.")
        with contextlib.ExitStack() as stack:
            sides: dict[str, OpenFile] = {}
            for key, (folder, filename) in (("a", (folder_a, filename_a)), ("b", (folder_b, filename_b))):
                sides[key] = self._open(folder, filename)
                stack.enter_context(sides[key].fh)
            result: dict[str, Any] = {"file_a": sides["a"].info(), "file_b": sides["b"].info()}
            headers = {key: self._header(of, control) for key, of in sides.items()}
            plan = self._plan_comparison(sides, headers, identifier, identifier_b, identifier_column_a,
                                         identifier_column_b, compare_columns, column_mapping, match_keys,
                                         filters_a, filters_b)
            if plan.columns_not_compared is not None:
                result["columns_not_compared"] = plan.columns_not_compared
            result["search"] = plan.search
            records, presence, warnings = self._find_in_both(control, sides, headers, plan, identifier, identifier_b)
        result["presence"] = {"file_a": presence["a"], "file_b": presence["b"]}
        result["elapsed_seconds"] = round(control.elapsed, 2)
        if warnings:
            result["warnings"] = warnings
        report = _ComparisonReport(self, result, plan, self.limits.max_output_bytes - RESULT_ENVELOPE_BYTES)
        result["comparison_status"], result["explanation"] = report.conclude(records, presence)
        return result

    def _plan_comparison(self, sides: dict[str, OpenFile], headers: dict[str, Header], identifier: str,
                         identifier_b: str, identifier_column_a: Any, identifier_column_b: Any,
                         compare_columns: Any, column_mapping: Any, match_keys: Any, filters_a: Any,
                         filters_b: Any) -> "_ComparisonPlan":
        """Resolve every column the comparison uses, in both files, and describe the plan."""
        head_a, head_b = headers["a"], headers["b"]
        label_a, label_b = head_a.labels, head_b.labels
        mapping = self._mapping(head_a, head_b, sides["a"], sides["b"], column_mapping)
        used_mappings: set[int] = set()

        def counterpart(a: int, what: str) -> int:
            """The file B column for a file A column: the explicit mapping, else the one column of the same name."""
            if a in mapping:
                used_mappings.add(a)
                return mapping[a]
            same = [j for j, name in enumerate(head_b.columns) if name == head_a.columns[a]]
            if len(same) == 1:
                return same[0]
            raise ToolFailure(
                "column_not_found" if not same else "ambiguous_column",
                f"{what} '{_clip(label_a[a], 80)}' of file A has "
                + ("no column of the same name" if not same else f"{len(same)} columns of the same name")
                + f" in file B ('{sides['b'].name}'). Say which file B column it corresponds to with "
                  "column_mapping, e.g. [{\"a\": ..., \"b\": {\"position\": N}}].")

        id_a = self._column(head_a, identifier_column_a, sides["a"], "identifier_column_a")
        if identifier_column_b is not None:
            id_b = self._column(head_b, identifier_column_b, sides["b"], "identifier_column_b")
        else:
            id_b = counterpart(id_a, "identifier column")

        only_a: list[str] = []
        if compare_columns:
            pairs = [(a, counterpart(a, "compare column"))
                     for a in self._columns(head_a, compare_columns, sides["a"], "compare_columns")]
        else:
            # Default: every mapped column, plus every column whose name occurs exactly once in each file.
            pairs = []
            for a, name in enumerate(head_a.columns):
                if a in mapping:
                    pairs.append((a, counterpart(a, "column")))
                elif head_a.columns.count(name) == 1 and head_b.columns.count(name) == 1:
                    pairs.append((a, head_b.columns.index(name)))
                else:
                    only_a.append(head_a.label(a))
        keys = [(a, counterpart(a, "match key"))
                for a in self._columns(head_a, match_keys, sides["a"], "match_keys")] if match_keys else []
        unused = sorted(set(mapping) - used_mappings)
        predicates = {"a": self._filters(head_a, filters_a, sides["a"], "filters_a"),
                      "b": self._filters(head_b, filters_b, sides["b"], "filters_b")}
        identifying = {(id_a, id_b), *keys}
        substantive = [p for p in pairs if p not in identifying]

        search: dict[str, Any] = {
            "identifier_a": identifier, "identifier_column_a": label_a[id_a],
            "identifier_b": identifier_b, "identifier_column_b": label_b[id_b],
            "match_type": MATCH_TYPE,
            "match_keys": [{"a": label_a[a], "b": label_b[b]} for a, b in keys],
            "compared_column_count": len(pairs),
            "compared_columns_other_than_identifier_and_keys": len(substantive),
        }
        for key in ("a", "b"):
            if predicates[key]:
                search[f"filters_{key}"] = self._describe_filters(headers[key], predicates[key])
        if predicates["a"] or predicates["b"]:
            search["scope"] = ("In a file with filters, only records where the identifier matches AND every filter "
                               "column equals its value exactly in that same record are considered. Each file's "
                               "filters apply to that file's own columns only. 'Absent' for a filtered file means "
                               "absent within its filters, not absent from the file.")
        not_compared = None
        if compare_columns:
            listed: list[dict[str, str]] = []
            room = 2400
            for a, b in pairs:
                entry = {"a": label_a[a], "b": label_b[b]}
                room -= _size(entry) + 4
                if room < 0:
                    break
                listed.append(entry)
            search["compared_columns"] = listed
        else:
            search["compared_columns"] = (f"all {len(pairs)} columns of file A with exactly one counterpart in "
                                          "file B (same name, or column_mapping)")
            used_b = {b for _, b in pairs}
            only_b = [l for i, l in enumerate(label_b) if i not in used_b]
            not_compared = {
                "only_in_file_a_count": len(only_a), "only_in_file_a": _some(only_a, 1000),
                "only_in_file_b_count": len(only_b), "only_in_file_b": _some(only_b, 1000),
                "note": "Columns are paired by identical name unless column_mapping says otherwise; "
                        "repeated names are not paired.",
            }
        if unused:
            # Not an error: a caller may pass one general mapping and compare a subset. But say so, so
            # nobody believes those columns were compared.
            search["unused_column_mapping"] = {
                "count": len(unused), "file_a_columns": _some([label_a[a] for a in unused], 800),
                "note": "These column_mapping entries were valid but not used: a mapping only takes effect for "
                        "a column in compare_columns or match_keys, or for the identifier column. They were "
                        "NOT compared."}
        if not pairs:
            search["coverage_warning"] = ("NO columns are paired between the two files, so no field values can be "
                                          "compared. Supply column_mapping (and compare_columns).")
        elif not substantive:
            search["coverage_warning"] = ("The ONLY columns compared are the identifier and match-key columns, "
                                          "which are equal by construction. This says nothing about any other field.")
        return _ComparisonPlan(head_a, head_b, {"a": id_a, "b": id_b}, pairs, keys, substantive, predicates,
                               search, not_compared)

    def _find_in_both(self, control: ScanControl, sides: dict[str, OpenFile], headers: dict[str, Header],
                      plan: "_ComparisonPlan", identifier: str, identifier_b: str
                      ) -> tuple[dict[str, list[Record]], dict[str, dict[str, Any]], list[str]]:
        """Collect the matching records of each file, with what each scan established. One time budget."""
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
            scoped = bool(plan.predicates[key])
            reason: str | None = None
            problem: str | None = None
            try:
                # Filters are applied inside the scan: only records that satisfy them count towards the cap.
                for record, _ in self._iter_matches(of, header, ident, plan.id_cols[key],
                                                    header.data_offset, 1, control, hints, plan.predicates[key]):
                    if len(records[key]) >= cap:
                        reason = "too_many_matches"
                        break
                    records[key].append(record)
            except CsvParseError as exc:
                reason, problem = "error", _clip(str(exc), 300)
            except OSError as exc:
                reason, problem = "error", f"reading failed part-way: {exc.strerror or exc}"
            reason = reason or control.stop_reason
            file_warnings, sound, _, _ = self._integrity(control, of, header, ident)
            warnings += [f"File {key.upper()}: {text}" for text in file_warnings]
            complete = reason == "end_of_file" and sound
            absent = "no_match_within_filters" if scoped else "confirmed_absent"
            presence[key] = {
                "outcome": "match_found" if records[key] else (absent if complete else "inconclusive"),
                "matching_records_found": len(records[key]),
                "scan_complete": complete,
                "stop_reason": reason,
            }
            if scoped:
                presence[key]["identifier_matches_failing_filters"] = hints.failing_filters
                presence[key]["scope"] = "records matching the identifier and this file's filters"
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
        return records, presence, warnings


@dataclass
class _ComparisonPlan:
    head_a: Header
    head_b: Header
    id_cols: dict[str, int]
    pairs: list[tuple[int, int]]  # (file A column, file B column) compared for each record pair
    keys: list[tuple[int, int]]  # match keys
    substantive: list[tuple[int, int]]  # pairs other than the identifier and the keys
    predicates: dict[str, list[tuple[int, str]]]
    search: dict[str, Any]
    columns_not_compared: dict[str, Any] | None


def _cell_at(record: Record, index: int) -> str | None:
    """None means the record is too short to have this field: missing, which is not an empty string."""
    return record.fields[index] if index < len(record.fields) else None


class _ComparisonReport:
    """Turns the records found in both files into the comparison part of a response, within the output budget."""

    OMITTABLE = ("comparisons", "ambiguous_keys", "unmatched_records_in_file_a", "unmatched_records_in_file_b",
                 "unalignable_records")

    def __init__(self, service: "Service", result: dict[str, Any], plan: _ComparisonPlan, limit: int):
        self.service = service
        self.result = result
        self.plan = plan
        self.label_a, self.label_b = plan.head_a.labels, plan.head_b.labels
        # Everything below is added only while it fits; room is kept for the status and explanation.
        self.budget = limit - _size(result) - 3000
        self.omitted = dict.fromkeys(self.OMITTABLE, 0)

    # -------------------------------------------------------- budgeted output

    def take(self, cost: int) -> bool:
        if cost > self.budget:
            return False
        self.budget -= cost
        return True

    def add(self, name: str, item: dict[str, Any]) -> None:
        if self.take(_size(item) + 4):
            self.result.setdefault(name, []).append(item)
        else:
            self.result.setdefault(name, [])
            self.omitted[name] += 1

    def show(self, value: str | None) -> str | None:
        return None if value is None else self.service._cell(value)[0]

    def list_records(self, name: str, recs: list[Record], header: Header, indices: list[int]) -> None:
        rows = []
        for record in recs:
            row = self.service._fit(self.service._row(header, record, indices), max(min(self.budget, 4000), 600))
            if not self.take(_size(row) + 4):
                break
            rows.append(row)
        self.result[name] = rows
        if len(rows) < len(recs):
            self.result[name + "_omitted_for_size"] = len(recs) - len(rows)

    def list_both(self, recs_a: list[Record], recs_b: list[Record]) -> None:
        plan = self.plan
        self.list_records("records_in_file_a", recs_a, plan.head_a, [a for a, _ in plan.pairs][:50] or [plan.id_cols["a"]])
        self.list_records("records_in_file_b", recs_b, plan.head_b, [b for _, b in plan.pairs][:50] or [plan.id_cols["b"]])

    @staticmethod
    def varying(recs: list[Record], header: Header) -> list[str]:
        names = [label for i, label in enumerate(header.labels) if len({_cell_at(r, i) for r in recs}) == len(recs)]
        return _some(names[:20], 1000)

    @staticmethod
    def describe(labels: list[str], indices: list[int], values: tuple) -> dict[str, Any]:
        return {_clip(labels[i], 60): (None if v is None else _clip(v, 80)) for i, v in zip(indices, values)}

    def compare_pair(self, rec_a: Record, rec_b: Record, key: dict[str, Any] | None) -> tuple[bool, bool]:
        """Add one comparison; returns (any value differs, any field is structurally missing)."""
        changed, unchanged, missing = [], [], []
        for a, b in self.plan.pairs:
            va, vb = _cell_at(rec_a, a), _cell_at(rec_b, b)
            entry: dict[str, Any] = {"column_a": self.label_a[a]}
            if self.label_b[b] != self.label_a[a]:
                entry["column_b"] = self.label_b[b]
            if va is None or vb is None:
                # A field that is not there is not a value: it neither agrees nor differs.
                entry["missing_in"] = [name for name, v in (("file_a", va), ("file_b", vb)) if v is None]
                if va is not None:
                    entry["value_a"] = self.show(va)
                if vb is not None:
                    entry["value_b"] = self.show(vb)
                missing.append(entry)
                continue
            if va == vb:
                entry["value"] = self.show(va)
                unchanged.append(entry)
                continue
            entry["value_a"], entry["value_b"] = self.show(va), self.show(vb)
            note = _difference_note(va, vb)
            if note:
                entry["note"] = note
            changed.append(entry)
        out: dict[str, Any] = {} if key is None else {"key": key}
        out.update({
            "record_a": {"record_number": rec_a.number, "byte_offset": rec_a.offset},
            "record_b": {"record_number": rec_b.number, "byte_offset": rec_b.offset},
            "columns_compared": len(self.plan.pairs),
            "changed_count": len(changed), "unchanged_count": len(unchanged), "missing_count": len(missing),
            "changed": [], "unchanged": [],
        })
        if missing:
            out["missing"] = []
            out["missing_note"] = ("These fields do not exist in one or both records (the record is shorter than "
                                   "its header). They were not compared and are not evidence of agreement.")
        if not self.take(_size(out) + 800):  # 800: room for the truncation notes below
            self.result.setdefault("comparisons", [])
            self.omitted["comparisons"] += 1
            return bool(changed), bool(missing)
        left_out: list[str] = []
        for source, target in ((changed, "changed"), (missing, "missing"), (unchanged, "unchanged")):
            for entry in source:
                if self.take(_size(entry) + 4):
                    out[target].append(entry)
                else:
                    left_out.append(entry["column_a"])
        if left_out:
            out["truncated"] = True
            out["columns_omitted_for_size_count"] = len(left_out)
            out["columns_omitted_for_size"] = _some(left_out, 500)
        else:
            self.budget += 800
        self.result.setdefault("comparisons", []).append(out)
        return bool(changed), bool(missing)

    # -------------------------------------------------------- conclusions

    def conclude(self, records: dict[str, list[Record]], presence: dict[str, dict[str, Any]]) -> tuple[str, str]:
        """The comparison status and its explanation, adding whatever detail supports them."""
        plan = self.plan
        recs_a, recs_b = records["a"], records["b"]
        scope_a = " within its filters" if plan.predicates["a"] else ""
        scope_b = " within its filters" if plan.predicates["b"] else ""
        if not (presence["a"]["scan_complete"] and presence["b"]["scan_complete"]):
            return "inconclusive", (
                "INCONCLUSIVE: at least one file was not scanned completely or could not be read reliably, so "
                "presence and record alignment cannot be established. Any record counts above are partial. "
                "No comparison was made.")
        if not recs_a and not recs_b:
            return "absent_from_both", (
                f"Complete scans: no matching record in file A{scope_a} and none in file B{scope_b}."
                if scope_a or scope_b else "Complete scans: the security is in neither file.")
        if not recs_a or not recs_b:
            there, missing_side = ("A", "B") if recs_a else ("B", "A")
            scope_missing = scope_b if recs_a else scope_a
            if recs_a:
                self.list_records("records_in_file_a", recs_a, plan.head_a, [a for a, _ in plan.pairs][:50] or [plan.id_cols["a"]])
            else:
                self.list_records("records_in_file_b", recs_b, plan.head_b, [b for _, b in plan.pairs][:50] or [plan.id_cols["b"]])
            return "present_in_one_file_only", (
                f"Complete scans: the security is in file {there} ({len(recs_a or recs_b)} record(s)) and "
                + (f"has no matching record in file {missing_side}{scope_missing} (which is not the same as "
                   "being absent from that file)." if scope_missing else f"absent from file {missing_side}."))
        if not plan.pairs:
            self.list_both(recs_a, recs_b)
            return "no_comparable_columns", (
                f"NO COMPARISON WAS POSSIBLE: the security is present in both files ({len(recs_a)} record(s) in "
                f"file A, {len(recs_b)} in file B), but no column of file A is paired with a column of file B, so "
                "not one field value was compared. This is not a finding that the records agree. Supply "
                "column_mapping to say which columns correspond.")
        if not plan.keys and (len(recs_a) > 1 or len(recs_b) > 1):
            differ = {"file_a": self.varying(recs_a, plan.head_a), "file_b": self.varying(recs_b, plan.head_b)}
            if self.take(_size(differ) + 100):
                self.result["columns_that_differ_between_matches"] = differ
            self.list_both(recs_a, recs_b)
            return "ambiguous_alignment", (
                f"AMBIGUOUS: file A has {len(recs_a)} matching record(s) and file B has {len(recs_b)}, and no match_keys "
                "were given, so records cannot be paired reliably. No comparison was made. Supply match_keys that "
                "identify one record per file (see columns_that_differ_between_matches), narrow each file with "
                "filters_a / filters_b, or compare specific records using find_security_in_file.")
        return self.pair_and_compare(recs_a, recs_b)

    def pair_and_compare(self, recs_a: list[Record], recs_b: list[Record]) -> tuple[str, str]:
        """Pair records by their match keys (one record each, or none needed) and compare every pair."""
        plan = self.plan
        key_a, key_b = [a for a, _ in plan.keys], [b for _, b in plan.keys]
        groups: dict[str, dict[tuple, list[Record]]] = {"file_a": {}, "file_b": {}}
        counts = dict.fromkeys(self.OMITTABLE, 0)
        # A record too short to have a key field has no key: it is set aside, never matched on "nothing".
        for side, recs, indices, labels in (("file_a", recs_a, key_a, self.label_a), ("file_b", recs_b, key_b, self.label_b)):
            for rec in recs:
                values = tuple(_cell_at(rec, i) for i in indices)
                if None in values:
                    counts["unalignable_records"] += 1
                    self.add("unalignable_records", {
                        "file": side, "record_number": rec.number,
                        "missing_key_fields": [_clip(labels[i], 60) for i, v in zip(indices, values) if v is None],
                        "reason": "the record is too short to have this match-key field"})
                else:
                    groups[side].setdefault(values, []).append(rec)
        groups_a, groups_b = groups["file_a"], groups["file_b"]
        differing = with_missing = 0
        for key_values, group in groups_a.items():
            other = groups_b.get(key_values)
            described = self.describe(self.label_a, key_a, key_values)
            if other is None:
                for r in group:
                    counts["unmatched_records_in_file_a"] += 1
                    self.add("unmatched_records_in_file_a", {"record_number": r.number, "key": described})
            elif len(group) > 1 or len(other) > 1:
                counts["ambiguous_keys"] += 1
                self.add("ambiguous_keys", {"key": described, "records_in_file_a": [r.number for r in group],
                                            "records_in_file_b": [r.number for r in other]})
            else:
                counts["comparisons"] += 1
                differs, lacks = self.compare_pair(group[0], other[0], described if plan.keys else None)
                differing += differs
                with_missing += lacks
        for key_values, group in groups_b.items():
            if key_values not in groups_a:
                described = self.describe(self.label_b, key_b, key_values)
                for r in group:
                    counts["unmatched_records_in_file_b"] += 1
                    self.add("unmatched_records_in_file_b", {"record_number": r.number, "key": described})
        left = {name: n for name, n in self.omitted.items() if n}
        if left:
            self.result["omitted_for_size"] = left
        compared, ambiguous = counts["comparisons"], counts["ambiguous_keys"]
        only_in_a, only_in_b = counts["unmatched_records_in_file_a"], counts["unmatched_records_in_file_b"]
        unalignable = counts["unalignable_records"]
        self.result["record_pairs_compared"] = compared
        coverage = (f"{len(plan.pairs)} column(s) compared per pair, of which {len(plan.substantive)} other than "
                    "the identifier and match keys")
        findings = (f"{differing} pair(s) have at least one differing value"
                    + (f"; {with_missing} pair(s) have fields missing from a record, which were not compared"
                       if with_missing else ""))
        if (ambiguous or unalignable) and not compared:
            status = "ambiguous_alignment"
            text = ("AMBIGUOUS: no records could be paired. "
                    + (f"The match_keys do not identify a single record in each file for {ambiguous} key(s) "
                       "(see ambiguous_keys); add further match_keys. " if ambiguous else "")
                    + (f"{unalignable} record(s) are too short to have a match-key field (see "
                       "unalignable_records). " if unalignable else "")).rstrip()
        elif ambiguous or only_in_a or only_in_b or unalignable:
            status = "partially_compared"
            text = (f"{compared} record pair(s) compared ({coverage}); {findings}. Some "
                    f"records could not be paired: {ambiguous} ambiguous key(s), {only_in_a} record(s) only in "
                    f"file A, {only_in_b} only in file B"
                    + (f", {unalignable} without a usable match key (see unalignable_records)" if unalignable else "")
                    + ".")
        else:
            status = "compared"
            text = f"Complete scans: {compared} record pair(s) compared ({coverage}); {findings}."
        if compared and not plan.substantive:
            text += (" ONLY identifier and match-key columns were compared, so this does not show that any "
                     "other field agrees.")
        if left:
            text += " Some detail was left out to fit the output-size limit (see omitted_for_size)."
        return status, text


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
