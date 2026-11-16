#!/usr/bin/env python3
"""Check CSV-o-Matic against pandas, an independent CSV parser, on one of your own files.

For every column, every distinct value (up to --values-per-column) plus two values that are
not in the file is looked up with count_matching_rows and find_security_in_file, and the
answers are compared with what pandas read. The file is copied to a temporary folder first;
the original is only read.

    python scripts/check_against_pandas.py /path/to/file.csv
    python scripts/check_against_pandas.py file.csv --delimiter ';' --encoding latin-1

Needs pandas (pip install pandas). Meant for files that fit in memory: pandas loads the lot.
"""

from __future__ import annotations

import argparse
import collections
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd

from csv_o_matic.config import Config, Limits
from csv_o_matic.csvscan import ScanControl
from csv_o_matic.service import MAX_IDENTIFIER_CHARS, Service


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file", type=Path)
    parser.add_argument("--delimiter", default=",")
    parser.add_argument("--encoding", default="utf-8")
    parser.add_argument("--values-per-column", type=int, default=200)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "d").mkdir()
        (root / "m").mkdir()
        shutil.copy(args.file, root / "d" / "f.csv")
        (root / "m" / "p.yaml").write_text(
            "id: p\nname: p\ndescription: d\npurpose: p\nrun_semantics: r\nfolders:\n"
            f"  - {{id: d, path: d, description: d, csv_format: {{delimiter: {args.delimiter!r}, encoding: {args.encoding}}}}}\n"
            "steps:\n  - {id: s, name: s, description: s, writes: [d]}\n")
        frame = pd.read_csv(args.file, dtype=str, keep_default_na=False, na_filter=False, skip_blank_lines=False,
                            sep=args.delimiter, encoding=args.encoding, engine="c")
        print(f"pandas read {len(frame):,} rows x {len(frame.columns)} columns")

        problems = checked = 0
        # Normal chunks, then tiny ones so that records straddle chunk boundaries.
        for chunk, sample in ((4 * 1024 * 1024, args.values_per_column), (4096, 20)):
            service = Service(Config(root, root / "m", Limits(chunk_bytes=chunk, max_record_bytes=64 * 1024 * 1024,
                                                             max_rows_per_response=50)))
            header = service.inspect_csv_headers("d", "f.csv")
            if header["column_count"] != len(frame.columns):
                print(f"HEADER MISMATCH: {header['column_count']} columns vs pandas {len(frame.columns)}")
                return 1
            for position, column in enumerate(frame.columns, start=1):
                counts = collections.Counter(frame[column])
                values = [v for v in counts if v and len(v) <= MAX_IDENTIFIER_CHARS][:sample]
                for value in [*values, "__not_in_this_file__", "0" + (values[0] if values else "x")]:
                    want = counts.get(value, 0)
                    checked += 1
                    got = service.count_matching_rows(ScanControl(), "d", "f.csv", value, f"#{position}")
                    expected_outcome = "match_found" if want else "confirmed_absent"
                    if got["count"] != want or got["outcome"] != expected_outcome:
                        problems += 1
                        print(f"COUNT MISMATCH column {column!r} value {value[:60]!r}: got {got['count']} "
                              f"({got['outcome']}), pandas {want}. {got.get('warnings', '')}")
                        continue
                    if not want:
                        continue
                    rows, cursor = [], None
                    while True:
                        page = service.find_security_in_file(ScanControl(), "d", "f.csv", value, f"#{position}", cursor=cursor)
                        rows += page["matches"]
                        cursor = page["next_cursor"]
                        if page["more_matches"] == "none" or not cursor:
                            break
                    expected_rows = frame.index[frame[column] == value].tolist()
                    if len(rows) != len(expected_rows):
                        problems += 1
                        print(f"ROW COUNT MISMATCH column {column!r} value {value[:60]!r}: {len(rows)} vs {len(expected_rows)}")
                        continue
                    for match, index in list(zip(rows, expected_rows))[:5]:
                        if "values_omitted_for_size" in match or "truncated_cells" in match:
                            continue
                        if list(match["values"].values()) != list(frame.iloc[index]):
                            problems += 1
                            print(f"VALUE MISMATCH column {column!r} value {value[:60]!r} at record {match['record_number']}")
        print(f"{checked:,} lookups checked, {problems} disagreement(s) with pandas")
        return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
