#!/usr/bin/env python3
"""Check csv-o-matic process maps the way the server will see them, and then some.

    python check_process_map.py process_maps/pricing.yaml --config config.yaml
    python check_process_map.py --maps-dir process_maps --data-root /data/etl
    python check_process_map.py drafts/pricing.yaml --maps-dir process_maps   # draft checked alongside the live maps

Three layers of checks:

  1. Schema   - the server's own model (imported from the repo). A map that fails here is
                left out of the server entirely. Reported as ERROR.
  2. Lint     - things the schema accepts but that make the tools mislead or go quiet:
                regex groups with no token, mappings that point at folders the step does not
                touch, shared folders the maps disagree about. Needs no data.
  3. Data     - with --data-root or --config: does each folder exist, do the real filenames
                match the regex and parse as dates, do the declared columns exist in the real
                headers (exact, case-sensitive), is the delimiter/encoding right.
                Reads only the first part of a few files per folder; never writes.

Exit status: 0 = no errors (warnings possible), 1 = at least one ERROR, 2 = could not run.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

HEAD_BYTES = 1 << 20  # how much of a file is read to find its header
CANDIDATE_DELIMITERS = [",", ";", "\t", "|"]
BOMS = [
    (b"\xff\xfe\x00\x00", "UTF-32"), (b"\x00\x00\xfe\xff", "UTF-32"),
    (b"\xff\xfe", "UTF-16"), (b"\xfe\xff", "UTF-16"),
]


# --------------------------------------------------------------------------- setup

def find_repo(explicit: str | None) -> Path | None:
    """Locate the csv-o-matic checkout so the server's own schema is the one applied."""
    starts = [Path(explicit)] if explicit else []
    if os.environ.get("CSV_O_MATIC_REPO"):
        starts.append(Path(os.environ["CSV_O_MATIC_REPO"]))
    starts += [Path.cwd(), Path(__file__).resolve().parent]
    for start in starts:
        for candidate in (start, *start.resolve().parents):
            if (candidate / "src" / "csv_o_matic" / "process_maps.py").is_file():
                return candidate
    return None


def import_model(repo: Path | None):
    if repo is not None:
        sys.path.insert(0, str(repo / "src"))
    try:
        from csv_o_matic import process_maps  # type: ignore
    except ImportError as exc:
        print(
            f"Cannot import csv_o_matic.process_maps ({exc}).\n"
            "Run from inside the csv-o-matic repo, pass --repo /path/to/csv-o-matic, or "
            "`pip install -e .` there. It needs pyyaml and pydantic.",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    return process_maps


class Report:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, str, str]] = []  # level, map file, where, message

    def add(self, level: str, file: str, where: str, message: str) -> None:
        self.items.append((level, file, where, message))

    def count(self, level: str) -> int:
        return sum(1 for item in self.items if item[0] == level)


# --------------------------------------------------------------------------- layer 1: schema

def load_maps(pm, files: list[Path], report: Report) -> dict[Path, object]:
    """Same parsing, validation and error wording as the server's load_catalog."""
    import yaml
    from pydantic import ValidationError

    loaded: dict[Path, object] = {}
    seen: dict[str, str] = {}
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
            raw = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
            process = pm.ProcessMap.model_validate(raw)
        except ValidationError as exc:
            for err in exc.errors():
                where = ".".join(map(str, err["loc"])) or "<root>"
                report.add("ERROR", path.name, describe_loc(raw, err["loc"]) or where, f"{where}: {err['msg']}")
            continue
        except (OSError, ValueError, yaml.YAMLError) as exc:
            report.add("ERROR", path.name, "file", " ".join(str(exc).split()))
            continue
        key = process.id.casefold()
        if key in seen:
            report.add("ERROR", path.name, "id", f"duplicate process id '{process.id}' (also in {seen[key]}); "
                       "the server keeps the first file in name order and drops this one")
            continue
        seen[key] = path.name
        loaded[path] = process
    return loaded


def describe_loc(raw: object, loc: tuple) -> str:
    """Turn ('folders', 2, 'filename_convention') into 'folder vendor_raw' where possible."""
    try:
        if len(loc) >= 2 and loc[0] in ("folders", "steps") and isinstance(loc[1], int):
            item = raw[loc[0]][loc[1]]  # type: ignore[index]
            if isinstance(item, dict) and item.get("id"):
                return f"{loc[0][:-1]} {item['id']}"
    except (KeyError, IndexError, TypeError):
        pass
    return ""


# --------------------------------------------------------------------------- layer 2: lint

def lint(process, file: str, report: Report) -> None:
    folder_ids = {f.id for f in process.folders}
    read_by: dict[str, list[str]] = {fid: [] for fid in folder_ids}
    written_by: dict[str, list[str]] = {fid: [] for fid in folder_ids}
    for step in process.steps:
        for fid in step.reads:
            read_by[fid].append(step.id)
        for fid in step.writes:
            written_by[fid].append(step.id)

    for folder in process.folders:
        where = f"folder {folder.id}"
        if not read_by[folder.id] and not written_by[folder.id]:
            report.add("WARN", file, where, "no step reads or writes it, so list_folders shows it with no role in the process")
        if not folder.identifier_columns:
            report.add("WARN", file, where, "no identifier_columns: the investigating model has to guess which column holds the security id")
        if not folder.record_key_columns:
            report.add("INFO", file, where, "no record_key_columns: compare_security_records cannot be told what identifies one record")
        conv = folder.filename_convention
        if conv is None:
            report.add("INFO", file, where, "no filename_convention: date filters can only use filesystem time, which is "
                       "often unreliable. Fine if another map declares the convention for this folder")
            continue
        groups = set(re.compile(conv.regex).groupindex)
        for name in sorted(groups - set(conv.tokens)):
            report.add("WARN", file, where, f"regex group '{name}' has no entry under tokens, so its value is never returned or filterable")
        placeholders = set(re.findall(r"\{(\w+)\}", conv.pattern))
        for name in sorted(placeholders - groups):
            report.add("WARN", file, where, f"pattern shows {{{name}}} but the regex has no group of that name")
        for name in sorted(set(conv.tokens) - placeholders):
            if conv.tokens[name].format:
                report.add("INFO", file, where, f"token '{name}' is not shown as {{{name}}} in the human-readable pattern")
        dated = [n for n, t in conv.tokens.items() if t.format]
        if not dated:
            report.add("WARN", file, where, "no token has a 'format', so files cannot be filtered by filename date")
        elif len(dated) > 1 and "default_date_token" not in conv.model_fields_set:
            report.add("INFO", file, where, f"default_date_token not set; date filters default to '{conv.default_date_token}' "
                       f"(first of {dated}). State it if that is not the business date")
        for name, token in conv.tokens.items():
            has_time = token.format and any(c in token.format for c in ("%H", "%I", "%M", "%S"))
            if has_time and not token.timezone:
                report.add("INFO", file, where, f"token '{name}' is a time with no timezone stated")
        if not re.search(r"csv\W*$", conv.regex, re.IGNORECASE):
            report.add("INFO", file, where, "regex does not appear to end in .csv; only .csv files are ever listed")

    steps = {s.id: s for s in process.steps}
    for step in process.steps:
        where = f"step {step.id}"
        if not step.reads and not step.writes:
            report.add("WARN", file, where, "reads and writes nothing")
        for m in step.field_mappings:
            if m.from_folder not in step.reads:
                report.add("WARN", file, where, f"field mapping reads from '{m.from_folder}', which is not in this step's reads")
            if m.to_folder not in step.writes:
                report.add("WARN", file, where, f"field mapping writes to '{m.to_folder}', which is not in this step's writes")
        if len(step.downstream) > 1:
            silent = [d for d in step.downstream if not steps[d].branch_condition]
            if silent:
                report.add("WARN", file, where, f"branches to {step.downstream} but {silent} have no branch_condition, "
                           "so nothing says which records go where")
        for fid in step.reads:
            producers = [s for s in written_by[fid] if s != step.id]
            if producers and not any(p in step.upstream for p in producers):
                report.add("INFO", file, where, f"reads '{fid}' written by {producers} but is not linked to it by upstream/downstream")
        if step.writes and not step.filter_rules and step.reads:
            report.add("INFO", file, where, "no filter_rules. If the step can drop records, say how; if it never does, "
                       "say so in notes so 'missing' is not blamed on it")

    # cycles in the step graph
    state: dict[str, int] = {}

    def visit(sid: str, trail: list[str]) -> None:
        if state.get(sid) == 1:
            report.add("WARN", file, f"step {sid}", "steps form a loop: " + " -> ".join(trail[trail.index(sid):] + [sid]))
            return
        if state.get(sid) == 2:
            return
        state[sid] = 1
        for nxt in steps[sid].downstream:
            visit(nxt, trail + [sid])
        state[sid] = 2

    for sid in steps:
        visit(sid, [])


def lint_shared_folders(pm, loaded: dict[Path, object], data_root: Path, report: Report) -> None:
    """Folders declared by several maps are merged by physical path; check the maps agree."""
    owner_file = {id(proc): path.name for path, proc in loaded.items()}
    catalog = pm.Catalog(list(loaded.values()), [], data_root)
    for ref in catalog.folders():
        if len(ref.usages) < 2:
            continue
        names = ", ".join(f"{owner_file[id(p)]}:{f.id}" for p, f in ref.usages)
        for proc, _folder in ref.usages:
            file = owner_file[id(proc)]
            _fmt, conflict = ref.csv_format()
            if conflict:
                report.add("ERROR", file, f"path {ref.path}", f"maps disagree on csv_format ({conflict}); the server refuses "
                           "every file in this folder with process_map_conflict until they agree")
            regexes = {f.filename_convention.regex for _, f in ref.usages if f.filename_convention}
            if len(regexes) > 1:
                report.add("WARN", file, f"path {ref.path}", f"declared by {names} with different filename regexes; "
                           "only the first map in file-name order is used")
            keys = {tuple(f.record_key_columns) for _, f in ref.usages if f.record_key_columns}
            if len(keys) > 1:
                report.add("WARN", file, f"path {ref.path}", f"declared by {names} with different record_key_columns")


# --------------------------------------------------------------------------- layer 3: data

def read_head(path: Path) -> bytes:
    with open(path, "rb") as fh:
        return fh.read(HEAD_BYTES)


def parse_header(raw: bytes, delimiter: str, encoding: str) -> tuple[list[str] | None, str | None]:
    """First CSV record of the file under the declared format, or a reason it cannot be read."""
    for bom, name in BOMS:
        if raw.startswith(bom):
            return None, f"starts with a {name} byte-order mark; the server refuses {name} files (unsupported_encoding)"
    if raw.startswith(b"\x1f\x8b") or raw.startswith(b"PK\x03\x04"):
        return None, "is compressed data under a .csv name; the server refuses it"
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    if len(raw) >= HEAD_BYTES - 3:  # cut at a line end so a split character or record is not misread
        raw = raw[: max(raw.rfind(b"\n"), raw.rfind(b"\r")) + 1] or raw
    text = raw.decode("utf-8" if encoding == "utf-8-sig" else encoding, errors="replace")
    try:
        row = next(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter), None)
    except csv.Error as exc:
        return None, f"header cannot be parsed: {exc}"
    if row is None:
        return None, "is empty"
    return row, None


def encoding_doubt(raw: bytes, encoding: str) -> str | None:
    body = raw[3:] if raw.startswith(b"\xef\xbb\xbf") else raw
    if len(raw) >= HEAD_BYTES - 3:
        body = body[: max(body.rfind(b"\n"), body.rfind(b"\r")) + 1] or body
    if body.isascii():
        return None
    try:
        body.decode("utf-8")
        is_utf8 = True
    except UnicodeDecodeError:
        is_utf8 = False
    if encoding in ("utf-8", "utf-8-sig", "ascii") and not is_utf8:
        return (f"declared {encoding} but the first part of the file is not valid {encoding}; "
                "likely cp1252 or latin-1. Set csv_format.encoding")
    if encoding in ("latin-1", "cp1252") and is_utf8:
        return f"declared {encoding} but the non-ASCII bytes read as valid UTF-8; the encoding is probably utf-8"
    return None


def suggest(name: str, header: list[str]) -> str:
    fold = lambda s: re.sub(r"[\s_\-]+", "", s).casefold()  # noqa: E731
    close = [h for h in header if fold(h) == fold(name)]
    if close:
        return f" Header has {close[0]!r} - names are matched exactly, including case and spaces."
    import difflib
    near = difflib.get_close_matches(name, header, n=3, cutoff=0.6)
    return f" Closest: {near}." if near else ""


def check_filenames(folder, files: list[str], file: str, report: Report) -> list[str]:
    """Return the files that follow the convention (all files when there is none)."""
    conv = folder.filename_convention
    where = f"folder {folder.id}"
    if conv is None:
        return files
    no_match, bad_date, good = [], [], []
    for name in files:
        match = re.fullmatch(conv.regex, name)
        if not match:
            no_match.append(name)
            continue
        problem = None
        for token_name, token in conv.tokens.items():
            value = match.group(token_name)
            if value is not None and token.format:
                try:
                    datetime.strptime(value, token.format)
                except ValueError:
                    problem = f"{name} ({token_name}={value!r} is not {token.format})"
        (bad_date if problem else good).append(problem or name)
    total = len(files)
    if total and not good:
        report.add("ERROR", file, where, f"none of the {total} .csv files match the filename convention "
                   f"(e.g. {files[0]!r}); every file would be listed as not matching and date filters would return nothing")
    elif no_match:
        report.add("WARN", file, where, f"{len(no_match)} of {total} .csv files do not match the regex, e.g. {no_match[:3]}. "
                   "Widen the regex if they belong to the process; otherwise explain them in the convention's notes")
    if bad_date:
        report.add("WARN", file, where, f"{len(bad_date)} file(s) match the regex but a date token does not parse: {bad_date[:3]}")
    return good or files


def check_folder(folder, steps, resolved: Path, file: str, report: Report, per_folder: int) -> None:
    where = f"folder {folder.id}"
    if not resolved.is_dir():
        report.add("WARN", file, where, f"does not exist on disk: {resolved}")
        return
    csvs, other, subdirs = [], [], []
    for entry in os.scandir(resolved):
        if entry.is_dir(follow_symlinks=False):
            subdirs.append(entry.name)
        elif entry.name.lower().endswith(".csv") and entry.is_file(follow_symlinks=False):
            csvs.append(entry.name)
        else:
            other.append(entry.name)
    if subdirs:
        report.add("INFO", file, where, f"has subfolders {sorted(subdirs)[:5]}; folders are flat, so anything inside them "
                   "is invisible unless each is declared as its own folder")
    if other:
        report.add("INFO", file, where, f"{len(other)} entries are not regular .csv files and are invisible to the server, e.g. {sorted(other)[:3]}")
    if not csvs:
        report.add("WARN", file, where, f"contains no .csv files: {resolved}")
        return
    csvs.sort()
    good = check_filenames(folder, csvs, file, report)

    # Columns the map claims this folder has.
    claimed: dict[str, str] = {}
    for col in folder.identifier_columns:
        claimed.setdefault(col.column, "identifier_columns")
    for col in folder.record_key_columns:
        claimed.setdefault(col, "record_key_columns")
    for step in steps:
        for m in step.field_mappings:
            if m.from_folder == folder.id:
                claimed.setdefault(m.from_column, f"step {step.id} field_mappings.from_column")
            if m.to_folder == folder.id:
                claimed.setdefault(m.to_column, f"step {step.id} field_mappings.to_column")

    sample = list(dict.fromkeys([good[-1], good[0]]))[:per_folder]  # newest and oldest by name
    fmt = folder.csv_format
    for index, name in enumerate(sample):
        try:
            raw = read_head(resolved / name)
        except OSError as exc:
            report.add("WARN", file, where, f"cannot read {name}: {exc.strerror or exc}")
            continue
        header, problem = parse_header(raw, fmt.delimiter, fmt.encoding)
        if header is None:
            report.add("ERROR" if index == 0 else "WARN", file, where, f"{name} {problem}")
            continue
        doubt = encoding_doubt(raw, fmt.encoding)
        if doubt:
            report.add("WARN", file, where, f"{name}: {doubt}")
        if len(header) == 1:
            others = [d for d in CANDIDATE_DELIMITERS if d != fmt.delimiter and d in header[0]]
            if others:
                shown = "tab" if others[0] == "\t" else repr(others[0])
                report.add("ERROR", file, where, f"{name}: the header is one column under delimiter {fmt.delimiter!r} and "
                           f"contains {shown}; set csv_format.delimiter. Until then searches give no reliable answer")
                continue
        duplicates = sorted({h for h in header if header.count(h) > 1})
        if duplicates and index == 0:
            report.add("INFO", file, where, f"{name}: header repeats column names {duplicates[:5]}; say in notes which position is meant")
        for column, source in claimed.items():
            if column not in header:
                level = "WARN" if index == 0 else "INFO"
                age = "" if index == 0 else " (older file; note the change of layout if real)"
                report.add(level, file, where, f"{source} names column {column!r}, which is not in the header of {name}{age}."
                           + suggest(column, header))
    if not claimed:
        report.add("INFO", file, where, "the map names no columns for this folder, so nothing was checked against its headers")


# --------------------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("maps", nargs="*", type=Path, help="map files to report on (default: every map in the maps dir)")
    parser.add_argument("--maps-dir", type=Path, help="directory of live maps, loaded for cross-map checks")
    parser.add_argument("--config", type=Path, help="server config.yaml; supplies data_root and process_maps_dir")
    parser.add_argument("--data-root", type=Path, help="base directory of the data; enables the data checks")
    parser.add_argument("--repo", help="path to the csv-o-matic checkout (found automatically when run inside it)")
    parser.add_argument("--files-per-folder", type=int, default=2, help="headers read per folder: newest, then oldest (default 2)")
    parser.add_argument("--quiet", action="store_true", help="hide INFO lines")
    args = parser.parse_args()

    pm = import_model(find_repo(args.repo))

    maps_dir, data_root = args.maps_dir, args.data_root
    if args.config:
        import yaml
        try:
            cfg = yaml.safe_load(args.config.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            print(f"Cannot read config {args.config}: {exc}", file=sys.stderr)
            return 2
        base = args.config.resolve().parent
        maps_dir = maps_dir or (base / str(cfg.get("process_maps_dir", ""))).resolve()
        data_root = data_root or (base / str(cfg.get("data_root", ""))).resolve()
    if maps_dir is None and args.maps:
        maps_dir = args.maps[0].resolve().parent
    if maps_dir is None:
        parser.error("give map files, --maps-dir or --config")

    suffixes = {".yaml", ".yml", ".json"}
    targets = [p.resolve() for p in args.maps]
    for path in targets:
        if not path.is_file():
            print(f"No such file: {path}", file=sys.stderr)
            return 2
        if path.suffix.lower() not in suffixes:
            print(f"{path.name}: the server only loads .yaml, .yml and .json files", file=sys.stderr)
            return 2
    live = sorted(p.resolve() for p in maps_dir.iterdir() if p.suffix.lower() in suffixes) if maps_dir.is_dir() else []
    # A draft that replaces a live map of the same file name is checked instead of it.
    target_names = {p.name for p in targets if p.parent != maps_dir.resolve()}
    files = sorted({*(p for p in live if p.name not in target_names), *targets}, key=lambda p: p.name)
    targets = targets or files
    if not files:
        print(f"No process maps found in {maps_dir}", file=sys.stderr)
        return 2

    report = Report()
    loaded = load_maps(pm, files, report)
    for path, process in loaded.items():
        lint(process, path.name, report)
    root_for_paths = (data_root or Path.cwd()).resolve()
    lint_shared_folders(pm, loaded, root_for_paths, report)

    if data_root is not None:
        if not data_root.is_dir():
            print(f"data root {data_root} is not a directory", file=sys.stderr)
            return 2
        for path, process in loaded.items():
            if path not in targets:
                continue
            for folder in process.folders:
                declared = Path(folder.path).expanduser()
                resolved = Path(os.path.realpath(declared if declared.is_absolute() else data_root / declared))
                check_folder(folder, process.steps, resolved, path.name, report, max(1, args.files_per_folder))

    target_names_all = {p.name for p in targets}
    order = {"ERROR": 0, "WARN": 1, "INFO": 2}
    shown = 0
    for path in files:
        if path.name not in target_names_all:
            continue
        items = sorted((i for i in report.items if i[1] == path.name), key=lambda i: order[i[0]])
        if args.quiet:
            items = [i for i in items if i[0] != "INFO"]
        status = "loads" if path in loaded else "REJECTED BY THE SERVER"
        print(f"\n{path.name}: {status}")
        if path in loaded:
            process = loaded[path]
            print(f"  process '{process.id}': {len(process.folders)} folders, {len(process.steps)} steps")
        for level, _file, where, message in items:
            print(f"  {level:<5} [{where}] {message}")
            shown += 1
        if not items:
            print("  no findings")

    errors = sum(1 for i in report.items if i[0] == "ERROR" and i[1] in target_names_all)
    warnings = sum(1 for i in report.items if i[0] == "WARN" and i[1] in target_names_all)
    print(f"\n{errors} error(s), {warnings} warning(s).", end=" ")
    if data_root is None:
        print("Data checks skipped: pass --data-root or --config to compare the maps with the real files.")
    else:
        print(f"Data checked under {data_root} (headers of up to {max(1, args.files_per_folder)} files per folder).")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
