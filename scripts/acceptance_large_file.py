#!/usr/bin/env python3
"""Acceptance check on a representative large file.

Generates a multi-gigabyte, 120-column CSV with quoted fields, embedded commas and newlines,
leading-zero identifiers and duplicate matches, then verifies results, memory use, time limits,
cancellation latency and that the file is left untouched.

    python scripts/acceptance_large_file.py --gb 2 --style mixed
    python scripts/acceptance_large_file.py --gb 2 --style quote_all   # worst case: every field quoted
"""

from __future__ import annotations

import argparse
import hashlib
import json
import resource
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from csv_o_matic.config import Config, Limits
from csv_o_matic.csvscan import ScanControl
from csv_o_matic.service import Service

COLUMNS = 120
PROCESS_MAP = """
id: acceptance
name: Acceptance
description: Large-file acceptance fixture.
purpose: Large-file acceptance fixture.
run_semantics: n/a
folders:
  - {id: big, path: big, description: Large generated files.}
steps:
  - {id: only, name: Only, description: Writes the large file., writes: [big]}
"""


def generate(path: Path, target_bytes: int, style: str) -> dict:
    """Write the file; return what was planted so results can be checked exactly."""
    header = ["sedol", "isin", "name", "notes", "alt_id"] + [f"attr_{i:03d}" for i in range(COLUMNS - 5)]
    filler = [f"{1000.5 + i:.4f}" for i in range(COLUMNS - 5)]
    if style == "quote_all":
        tail = ",".join(f'"{v}"' for v in filler)
        plain = '"%07d","GB00%08d","%s","%s","%s",' + tail + "\r\n"
        quoted = plain
        multiline = '"%07d","GB00%08d","%s","line one\nline two, with comma\nline three","%s",' + tail + "\r\n"
        head = ",".join(f'"{h}"' for h in header) + "\r\n"
    else:
        tail = ",".join(filler)
        plain = "%07d,GB00%08d,%s,%s,%s," + tail + "\r\n"
        quoted = '%07d,GB00%08d,"%s, Inc.",%s,%s,' + tail + "\r\n"
        multiline = '%07d,GB00%08d,"%s","line one\nline two, with comma\nline three",%s,' + tail + "\r\n"
        head = ",".join(header) + "\r\n"

    row_bytes = len(plain % (0, 0, "Company 0000000", "note", "A0")) + 2
    rows = max(target_bytes // row_bytes, 1000)
    planted = {int(rows * f) for f in (0.0001, 0.5, 0.9999)}  # duplicates of one security: start, middle, end
    lone = int(rows * 0.75)  # the same digits without leading zeroes: a different identifier
    cross = int(rows * 0.25)  # the identifier appearing in a different column only
    with open(path, "wb", buffering=8 * 1024 * 1024) as fh:
        fh.write(head.encode())
        batch = []
        for i in range(rows):
            sedol = 1_000_000 + i
            name, note, alt = f"Company {i:07d}", "note", f"A{i}"
            if i in planted:
                line = (multiline if i == max(planted) else quoted) % (42, i, f"Planted {i}", f"A{i}") if i == max(planted) \
                    else quoted % (42, i, f"Planted {i}", note, alt)
            elif i == lone:
                line = (plain % (0, i, name, note, alt)).replace("0000000", "42", 1)
            elif i == cross:
                line = plain % (sedol, i, name, note, "0000042")
            elif i % 1000 == 999:
                line = multiline % (sedol, i, name, alt)
            elif i % 2:
                line = quoted % (sedol, i, name, note, alt)
            else:
                line = plain % (sedol, i, name, note, alt)
            batch.append(line)
            if len(batch) == 2000:
                fh.write("".join(batch).encode())
                batch.clear()
        fh.write("".join(batch).encode())
    return {"rows": rows, "planted": sorted(planted), "lone": lone, "cross": cross}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gb", type=float, default=2.0)
    parser.add_argument("--style", choices=["mixed", "quote_all"], default="mixed")
    parser.add_argument("--generate", help=argparse.SUPPRESS)
    parser.add_argument("--dir", help="Where to put the generated file (default: a temporary directory)")
    args = parser.parse_args()
    if args.generate:
        print(json.dumps(generate(Path(args.generate), int(args.gb * 1024**3), args.style)))
        return 0

    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not ok:
            failures.append(label)

    with tempfile.TemporaryDirectory(dir=args.dir) as tmp:
        root = Path(tmp)
        (root / "big").mkdir()
        (root / "maps").mkdir()
        (root / "maps" / "acceptance.yaml").write_text(PROCESS_MAP)
        path = root / "big" / "big.csv"
        started = time.monotonic()
        # Generate in a child process so its memory use does not count towards this process's peak.
        child = subprocess.run([sys.executable, __file__, "--generate", str(path), "--gb", str(args.gb), "--style", args.style],
                               check=True, capture_output=True, text=True)
        facts = json.loads(child.stdout)
        size = path.stat().st_size
        print(f"Generated {size / 1024**3:.2f} GB, {facts['rows']:,} rows x {COLUMNS} columns, style={args.style}, "
              f"in {time.monotonic() - started:.0f}s")
        before = (sha256(path), path.stat().st_mtime_ns, size)

        service = Service(Config(data_root=root, process_maps_dir=root / "maps", limits=Limits(max_scan_seconds=600)))
        baseline = rss_mb()

        def timed(fn, *a, **k):
            t = time.monotonic()
            out = fn(ScanControl(max_seconds=600), *a, **k)
            return out, time.monotonic() - t

        print("Correctness")
        out, secs = timed(service.find_security_in_file, "big", "big.csv", "0000042", "sedol", ["sedol", "isin", "name", "notes"])
        rate = size / secs / 1e6
        isins = [m["values"]["isin"] for m in out["matches"]]
        check("leading-zero identifier: all duplicate matches found, complete scan",
              out["outcome"] == "match_found" and out["more_matches"] == "none"
              and isins == [f"GB00{i:08d}" for i in facts["planted"]], f"{secs:.1f}s, {rate:,.0f} MB/s")
        check("embedded newlines and commas preserved in a quoted field",
              out["matches"][-1]["values"]["notes"] == "line one\nline two, with comma\nline three")
        check("record numbers are exact despite multi-line records",
              [m["record_number"] for m in out["matches"]] == [i + 2 for i in facts["planted"]])
        out, _ = timed(service.find_security_in_file, "big", "big.csv", "42", "sedol", ["isin"])
        check("'42' is not '0000042'", [m["values"]["isin"] for m in out["matches"]] == [f"GB00{facts['lone']:08d}"])
        out, secs = timed(service.find_security_in_file, "big", "big.csv", "9999999", "sedol")
        check("absent identifier: confirmed absent after a complete scan",
              out["outcome"] == "confirmed_absent" and out["scan"]["complete"], f"{secs:.1f}s, {size / secs / 1e6:,.0f} MB/s")
        out, _ = timed(service.count_matching_rows, "big", "big.csv", "0000042", "sedol")
        check("exact count", out["count"] == 3 and out["count_is_exact"])
        out, secs = timed(service.search_across_columns, "big", "big.csv", "0000042", ["isin"])
        got = sorted((m["values"]["isin"], tuple(m["matched_columns"])) for m in out["matches"])
        want = sorted([(f"GB00{i:08d}", ("sedol",)) for i in facts["planted"]] + [(f"GB00{facts['cross']:08d}", ("alt_id",))])
        check("search across all columns names the matching column", got == want, f"{secs:.1f}s")
        out, _ = timed(service.compare_security_records, "big", "big.csv", "big", "big.csv", "42", "sedol")
        check("compare across two large scans", out["comparison_status"] == "compared"
              and out["comparisons"][0]["changed_count"] == 0 and out["comparisons"][0]["unchanged_count"] == COLUMNS)

        print("Limits")
        peak = rss_mb()
        check("bounded memory", peak - baseline < 200, f"peak RSS {peak:.0f} MB, +{peak - baseline:.0f} MB over baseline "
                                                       f"for a {size / 1024**2:,.0f} MB file")
        control = ScanControl(max_seconds=1.0)
        t = time.monotonic()
        out = service.count_matching_rows(control, "big", "big.csv", "0000042", "sedol")
        took = time.monotonic() - t
        if out["scan"]["complete"]:
            check("time limit (file scanned in under the 1s limit; nothing to interrupt)", out["count"] == 3)
        else:
            check("time limit stops the scan and withholds the count",
                  out["count"] is None and out["scan"]["stop_reason"] == "time_limit" and took < 2.0, f"returned after {took:.2f}s")
            total, cursor, hops = None, out["next_cursor"], 0
            while cursor:
                out = service.count_matching_rows(ScanControl(max_seconds=1.0), "big", "big.csv", "0000042", "sedol", cursor=cursor)
                total, cursor, hops = out["count"], out["next_cursor"], hops + 1
            check("resuming with cursors reaches the exact count", total == 3, f"{hops} continuation(s)")

        control = ScanControl(max_seconds=600)
        box: dict = {}
        worker = threading.Thread(target=lambda: box.update(out=service.find_security_in_file(
            control, "big", "big.csv", "0000042", "sedol", ["isin"])))
        worker.start()
        time.sleep(0.3)
        t = time.monotonic()
        control.stop.set()
        worker.join()
        latency = time.monotonic() - t
        out = box["out"]
        if out["scan"]["complete"]:
            check("cancellation (scan finished before it could be cancelled)", True)
        else:
            check("cancellation is responsive and reported as partial",
                  latency < 0.5 and out["scan"]["stop_reason"] == "cancelled" and out["results_are_partial"],
                  f"stopped {latency * 1000:.0f} ms after cancel, {out['matches_returned']} partial match(es)")

        print("Read-only")
        check("source file unmodified (sha256, size, mtime)", (sha256(path), path.stat().st_mtime_ns, path.stat().st_size) == before)
        check("no files created beside the data", sorted(p.name for p in (root / "big").iterdir()) == ["big.csv"])

    print("\nRESULT:", "FAILED: " + "; ".join(failures) if failures else "all checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
