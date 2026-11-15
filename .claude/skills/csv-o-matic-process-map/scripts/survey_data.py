#!/usr/bin/env python3
"""Survey data folders before writing a csv-o-matic process map.

    python survey_data.py /data/etl/pricing              # every folder under it that holds .csv files
    python survey_data.py /data/etl/pricing/01_raw --rows 3

For each folder that directly contains .csv files it prints the evidence a map has to agree with:

  - the filename shapes in use (digit runs collapsed), how many files have each shape, and
    which date/time formats every value in a digit run is consistent with;
  - for the newest file of each shape: the likely encoding and delimiter, the exact header,
    columns that look like security identifiers, and a few sample rows.

Read-only. Reads at most the first 256 KB of a few files per folder, so it is safe on
multi-gigabyte files. Stdlib only.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

HEAD_BYTES = 256 * 1024
DELIMITERS = [",", ";", "\t", "|"]
DATE_FORMATS = {
    4: ["%H%M", "%m%d", "%Y"],
    6: ["%H%M%S", "%y%m%d", "%d%m%y", "%Y%m"],
    8: ["%Y%m%d", "%d%m%Y", "%m%d%Y"],
    12: ["%Y%m%d%H%M"],
    14: ["%Y%m%d%H%M%S"],
}
ID_HINTS = re.compile(r"isin|sedol|cusip|figi|ticker|\bric\b|symbol|secur|instrument|asset.?id|(^|_)id$|Id$|ID$", re.IGNORECASE)


def shape_of(name: str) -> str:
    return re.sub(r"\d+", lambda m: f"<{len(m.group())}d>", name)


def consistent_formats(values: list[str]) -> list[str]:
    out = []
    for fmt in DATE_FORMATS.get(len(values[0]), []):
        try:
            parsed = [datetime.strptime(v, fmt) for v in values]
        except ValueError:
            continue
        if "%Y" in fmt and not all(1990 <= p.year <= 2100 for p in parsed):
            continue
        out.append(fmt)
    return out


def describe_file(path: Path, rows: int) -> None:
    try:
        with open(path, "rb") as fh:
            raw = fh.read(HEAD_BYTES)
    except OSError as exc:
        print(f"      cannot read: {exc.strerror or exc}")
        return
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        print("      encoding: UTF-16/UTF-32 byte-order mark -> NOT SUPPORTED by the server; the file must be re-exported")
        return
    if raw.startswith(b"\x1f\x8b") or raw.startswith(b"PK\x03\x04"):
        print("      compressed data under a .csv name -> the server refuses it")
        return
    bom = raw.startswith(b"\xef\xbb\xbf")
    body = raw[3:] if bom else raw
    if len(raw) == HEAD_BYTES:
        body = body[: max(body.rfind(b"\n"), body.rfind(b"\r")) + 1] or body
    if body.isascii():
        encoding, note = "utf-8", "pure ASCII in the part read; utf-8 (the default) is fine unless later rows differ"
    else:
        try:
            body.decode("utf-8")
            encoding, note = "utf-8", "valid UTF-8 with non-ASCII characters"
        except UnicodeDecodeError:
            encoding, note = "cp1252", "NOT valid UTF-8 -> cp1252 or latin-1; declare csv_format.encoding"
    print(f"      encoding: {encoding}{' (with BOM, which the server ignores)' if bom else ''} - {note}")
    text = body.decode(encoding, errors="replace")
    first_line = text.splitlines()[0] if text else ""
    counts = {d: first_line.count(d) for d in DELIMITERS}
    delimiter = max(counts, key=counts.get) if any(counts.values()) else ","
    shown = "tab" if delimiter == "\t" else repr(delimiter)
    default = "" if delimiter == "," else "  -> not the default; declare csv_format.delimiter"
    print(f"      delimiter: {shown}{default}")
    try:
        records = []
        for record in csv.reader(io.StringIO(text, newline=""), delimiter=delimiter):
            records.append(record)
            if len(records) > rows:
                break
    except csv.Error as exc:
        print(f"      cannot parse: {exc}")
        return
    if not records:
        print("      empty file")
        return
    header = records[0]
    print(f"      header ({len(header)} columns, exact spelling): {header}")
    if all(re.fullmatch(r"[\d.\-+eE]*", h) for h in header) or len(set(header)) < len(header):
        print("      ! header looks odd (numeric or repeated names) - check the file really has a header row")
    likely = [h for h in header if ID_HINTS.search(h)]
    if likely:
        print(f"      columns that look like identifiers: {likely}")
    for record in records[1:]:
        clipped = [v if len(v) <= 40 else v[:37] + "..." for v in record]
        print(f"      row: {clipped}")
    if len(records) == 1:
        print("      (header only - no data rows)")


def survey_folder(folder: Path, rows: int, max_shapes: int) -> None:
    csvs, others, subdirs = [], [], []
    for entry in os.scandir(folder):
        if entry.is_dir(follow_symlinks=False):
            subdirs.append(entry.name)
        elif entry.name.lower().endswith(".csv") and entry.is_file(follow_symlinks=False):
            csvs.append(entry.name)
        else:
            others.append(entry.name)
    if not csvs:
        return
    print(f"\n=== {folder}")
    print(f"  {len(csvs)} .csv files" + (f"; {len(others)} other entries the server will not see, e.g. {sorted(others)[:3]}" if others else ""))
    shapes: dict[str, list[str]] = defaultdict(list)
    for name in csvs:
        shapes[shape_of(name)].append(name)
    ordered = sorted(shapes.items(), key=lambda kv: -len(kv[1]))
    if len(ordered) > 1:
        print(f"  {len(ordered)} different filename shapes - decide whether they are one convention with optional parts, "
              "or strays to mention in notes")
    for shape, names in ordered[:max_shapes]:
        names.sort()
        print(f"\n  shape {shape}   ({len(names)} files)")
        print(f"    first: {names[0]}")
        if len(names) > 1:
            print(f"    last:  {names[-1]}")
        runs = [re.findall(r"\d+", n) for n in names]
        for position in range(len(runs[0])):
            values = [r[position] for r in runs]
            formats = consistent_formats(values)
            if formats:
                tail = " <- ambiguous: confirm with the owner or the code" if len(formats) > 1 else ""
                print(f"    digit run {position + 1} (e.g. {values[-1]}): every value parses as {formats}{tail}")
            elif len(values[0]) in DATE_FORMATS:
                print(f"    digit run {position + 1} (e.g. {values[-1]}): not a date/time in any usual format - a sequence or id?")
        dates = sorted({r[0] for r in runs if r})
        if len(dates) < len(names):
            print(f"    {len(names)} files but {len(dates)} distinct values in digit run 1 -> several files per date "
                  "(retries, reruns, redeliveries?). run_semantics must say which one wins")
        print(f"    newest by name: {names[-1]}")
        describe_file(folder / names[-1], rows)
    if len(ordered) > max_shapes:
        print(f"\n  ... {len(ordered) - max_shapes} more shapes not shown (--max-shapes)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", type=Path, help="a data folder, or a directory containing data folders")
    parser.add_argument("--rows", type=int, default=2, help="sample data rows to print per file (default 2; 0 for none)")
    parser.add_argument("--max-shapes", type=int, default=6, help="filename shapes to detail per folder (default 6)")
    args = parser.parse_args()
    if not args.root.is_dir():
        print(f"{args.root} is not a directory", file=sys.stderr)
        return 2
    found = False
    for current, dirs, files in os.walk(args.root):
        dirs.sort()
        if any(f.lower().endswith(".csv") for f in files):
            found = True
            survey_folder(Path(current), max(0, args.rows), args.max_shapes)
    if not found:
        print(f"No .csv files under {args.root}")
    else:
        print("\nPaths in a map are written relative to the server's data_root. Headers above are from the newest "
              "file only; older files may differ.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
