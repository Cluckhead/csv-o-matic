"""Fixes brought in from the review of the parallel branches: what a cursor chain may conclude,
continuation past long records, comparison traps, confinement under a concurrent swap, error text."""

from __future__ import annotations

import csv
import io
import json
import os
import random
import threading
import time

import anyio
import pandas as pd
import pytest
from mcp.client import Client

from conftest import make_service
from csv_o_matic.config import Config, Limits
from csv_o_matic.csvscan import ScanControl
from csv_o_matic.server import build_server
from csv_o_matic.service import Service, ToolFailure

A, B, SEMI = "stage/a", "stage/b", "stage/semi"
SMALL = dict(chunk_bytes=1024, max_record_bytes=64 * 1024)


def ctl(seconds: float = 30.0) -> ScanControl:
    return ScanControl(max_seconds=seconds)


def chain(call, limit=10_000):
    """Follow next_cursor to the end; returns every response."""
    out = [call(None)]
    while out[-1]["next_cursor"]:
        assert len(out) < limit, "the cursor chain does not terminate"
        out.append(call(out[-1]["next_cursor"]))
    return out


# ---------------------------------------------------------------- what a cursor chain may conclude

def test_a_clean_last_page_cannot_certify_absence_after_an_earlier_page_saw_the_wrong_encoding(env):
    """The folder is declared latin-1, but the file is UTF-8: the identifier's UTF-8 bytes are in the first
    chunk only. Scanned in pieces, the last piece is clean - and must still not say 'absent' or give a count."""
    path = env[0] / SEMI / "mixed.csv"
    path.write_bytes(("id;name\nÄ1;first\n" + "".join(f"B{i};filler row {i}\n" for i in range(2000))).encode("utf-8"))
    service = make_service(env, **SMALL)

    whole = service.count_matching_rows(ctl(), SEMI, "mixed.csv", "Ä1", "id")
    assert whole["count"] is None and whole["outcome"] == "inconclusive"  # in one piece this was already right

    pages = chain(lambda c: service.count_matching_rows(ctl(0.0), SEMI, "mixed.csv", "Ä1", "id", cursor=c))
    assert len(pages) > 5 and pages[-1]["scan"]["complete"]
    last = pages[-1]
    assert last["count"] is None and last["count_is_exact"] is False and last["outcome"] == "inconclusive"
    assert any("encoding" in w for w in last["warnings"])

    found = chain(lambda c: service.find_security_in_file(ctl(0.0), SEMI, "mixed.csv", "Ä1", "id", cursor=c))
    assert found[-1]["outcome"] == "inconclusive" and found[-1]["more_matches"] == "unknown"


def test_near_misses_from_earlier_pages_are_not_forgotten_on_the_last_page(env):
    path = env[0] / A / "padded.csv"
    path.write_text("ID,Name\n 00123 ,padded\n" + "".join(f"S{i},filler row number {i}\n" for i in range(2000)))
    service = make_service(env, **SMALL)
    pages = chain(lambda c: service.find_security_in_file(ctl(0.0), A, "padded.csv", "00123", "ID", cursor=c))
    assert "near_misses" in pages[0] and len(pages) > 5
    last = pages[-1]
    assert last["outcome"] == "confirmed_absent"
    assert "No near misses" not in last["explanation"] and "earlier responses" in last["explanation"]


def test_every_continuation_gets_past_a_record_longer_than_one_read(env):
    """A record several chunks long with the time budget already spent: each call used to stop at the same byte."""
    path = env[0] / A / "long.csv"
    long_value = "x" * 20_000
    path.write_text(f'ID,Text\nS1,"{long_value}"\nS2,short\n' + f'S3,"{long_value}"\n' * 3 + "WANTED,end\n")
    service = make_service(env, **SMALL)
    pages = chain(lambda c: service.count_matching_rows(ctl(0.0), A, "long.csv", "WANTED", "ID", cursor=c), limit=50)
    stops = [p["scan"]["stopped_at_byte"] for p in pages]
    assert stops == sorted(set(stops)), "every response must stop at a later byte than the one before"
    assert pages[-1]["count"] == 1 and pages[-1]["count_is_exact"]


def test_a_cursor_is_bound_to_the_physical_file_not_just_its_size_and_time(env, service):
    path = env[0] / A / "many.csv"
    first = service.find_security_in_file(ctl(), A, "many.csv", "DUP", "ID", max_rows=2)
    assert first["next_cursor"]
    before = path.stat()
    replacement = path.with_name("many.tmp")
    replacement.write_bytes(path.read_bytes().replace(b"DUP", b"XYZ"))  # same size, different content
    os.replace(replacement, path)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    with pytest.raises(ToolFailure) as info:
        service.find_security_in_file(ctl(), A, "many.csv", "DUP", "ID", max_rows=2, cursor=first["next_cursor"])
    assert info.value.kind == "invalid_cursor"


# ---------------------------------------------------------------- comparisons

def test_a_comparison_with_no_paired_columns_does_not_claim_to_have_compared(env, service):
    (env[0] / B / "other.csv").write_text("sec_id,pf,px\n00777,P1,5\n")
    out = service.compare_security_records(ctl(), B, "left.csv", B, "other.csv", "00777", "ID", "sec_id")
    assert out["comparison_status"] == "no_comparable_columns" and "NO VALUES WERE COMPARED" in out["explanation"]
    assert out["presence"]["file_a"]["outcome"] == out["presence"]["file_b"]["outcome"] == "match_found"
    assert out["comparisons"][0]["changed_count"] == out["comparisons"][0]["unchanged_count"] == 0
    # With a mapping there is something to compare.
    out = service.compare_security_records(ctl(), B, "left.csv", B, "other.csv", "00777", "ID", "sec_id",
                                           column_mapping={"Price": "px"})
    assert out["comparison_status"] == "compared" and out["comparisons"][0]["unchanged_count"] == 1


def test_a_mapping_for_a_column_that_does_not_exist_is_reported(env, service):
    out = service.compare_security_records(ctl(), B, "left.csv", B, "right.csv", "00777", "ID", "sec_id",
                                           column_mapping={"Price": "px", "Portfollio": "pf"})
    assert out["comparison_status"] == "compared"
    assert any("Portfollio" in w and "no effect" in w for w in out["warnings"])
    clean = service.compare_security_records(ctl(), B, "left.csv", B, "right.csv", "00777", "ID", "sec_id",
                                             column_mapping={"Price": "px", "#2": "pf"})
    assert "warnings" not in clean


# ---------------------------------------------------------------- filters against a second parser

def test_filtered_counts_agree_with_pandas(env, service):
    rng = random.Random(20261116)
    ids, pfs, books = ["GB01", "GB02", "0001", "1", "gb01", "GB01 "], ["P1", "P2", "p1", "01", "1", ""], ["M", "H", 'a"b', "x,y"]
    for trial in range(40):
        rows = [[rng.choice(ids), rng.choice(pfs), rng.choice(books), str(rng.randint(0, 3))] for _ in range(rng.randint(0, 120))]
        buffer = io.StringIO()
        writer = csv.writer(buffer, quoting=rng.choice([csv.QUOTE_MINIMAL, csv.QUOTE_ALL]),
                            lineterminator=rng.choice(["\n", "\r\n"]))
        writer.writerow(["isin", "pf", "book", "n"])
        writer.writerows(rows)
        (env[0] / A / "fuzz.csv").write_text(buffer.getvalue(), newline="")
        frame = pd.read_csv(io.StringIO(buffer.getvalue()), dtype=str, keep_default_na=False)
        for _ in range(12):
            identifier = rng.choice(ids)
            filters = {name: rng.choice(values) for name, values in (("pf", pfs), ("book", books), ("n", "0123"))
                       if rng.random() < 0.6}
            expected = frame[frame["isin"] == identifier]
            for name, value in filters.items():
                expected = expected[expected[name] == value]
            out = service.count_matching_rows(ctl(), A, "fuzz.csv", identifier, "isin", filters=filters)
            assert out["count"] == len(expected), (trial, identifier, filters)
            found = service.find_security_in_file(ctl(), A, "fuzz.csv", identifier, "isin", filters=filters, max_rows=200)
            assert found["outcome"] == ("match_found" if len(expected) else "confirmed_absent")
            assert [m["record_number"] - 2 for m in found["matches"]] == list(expected.index)[:25]


# ---------------------------------------------------------------- confinement under a concurrent swap

def test_a_subfolder_swapped_back_and_forth_with_a_link_never_leaks_an_outside_file(env, service):
    """While one thread keeps replacing a subfolder with a link to an outside folder (and back), every read
    either returns the genuine file or is refused. Not once does the outside file's content come back."""
    data = env[0]
    inside, parked, outside = data / A / "day", data / A / "day_parked", data / "outside"
    inside.mkdir()
    outside.mkdir()
    (inside / "f.csv").write_text("ID,Where\nK1,INSIDE\n")
    (outside / "f.csv").write_text("ID,Where\nK1,OUTSIDE-SECRET\n")
    stop = threading.Event()

    def swapper():
        while not stop.is_set():
            try:
                inside.rename(parked)
                os.symlink(outside, inside)
                time.sleep(0.0005)
                inside.unlink()
                parked.rename(inside)
                time.sleep(0.0005)
            except OSError:
                pass

    thread = threading.Thread(target=swapper)
    thread.start()
    genuine = refused = 0
    try:
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            for call in (lambda: service.find_security_in_file(ctl(), f"{A}/day", "f.csv", "K1", "ID"),
                         lambda: service.inspect_csv_headers(f"{A}/day", "f.csv", sample_rows=1),
                         lambda: service.list_csv_files(f"{A}/day")):
                try:
                    out = call()
                except ToolFailure as failure:
                    assert failure.kind in ("folder_not_allowed", "folder_not_found", "folder_unreadable",
                                            "file_unreadable", "file_not_found"), failure.kind
                    refused += 1
                    continue
                assert "OUTSIDE-SECRET" not in json.dumps(out)
                genuine += 1
    finally:
        stop.set()
        thread.join()
    assert genuine > 5 and refused > 5  # both sides of the race were exercised


# ---------------------------------------------------------------- errors over the protocol

class Exploding(Service):
    def list_folders(self, *args, **kwargs):
        raise RuntimeError("could not read /srv/secret/path: row was 'CONFIDENTIAL'")


def test_an_unexpected_failure_does_not_put_exception_text_in_the_response(env):
    async def main():
        cfg = Config(data_root=env[0], process_maps_dir=env[1], limits=Limits())
        async with Client(build_server(cfg, Exploding(cfg))) as client:
            result = await client.call_tool("list_folders", {})
            out = result.structured_content
            assert result.is_error and out["error"]["type"] == "internal_error" and "RuntimeError" in out["error"]["message"]
            assert "CONFIDENTIAL" not in result.content[0].text and "/srv/secret" not in result.content[0].text
            # A failed request echoes every argument it was given, cursor and time budget included.
            result = await client.call_tool("count_matching_rows", {
                "folder": A, "filename": "nope.csv", "identifier": "1", "identifier_column": "ID",
                "cursor": "abc", "max_scan_seconds": 5, "filters": {"Name": "x"}})
            assert result.structured_content["request"] == {
                "folder": A, "filename": "nope.csv", "identifier": "1", "identifier_column": "ID",
                "filters": {"Name": "x"}, "cursor": "abc", "max_scan_seconds": 5}
    anyio.run(main)


def test_listing_reads_sizes_only_for_the_page_it_returns(env, monkeypatch):
    for i in range(300):
        (env[0] / B / f"f{i:04d}.csv").write_text("ID\n1\n")
    service = make_service(env, max_files_per_listing=10)
    calls = []
    real = os.lstat
    monkeypatch.setattr(os, "lstat", lambda path, *a, **k: (calls.append(path), real(path, *a, **k))[1])
    out = service.list_csv_files(B, newest_first=False, offset=100)
    assert out["total_matching_files"] == 302 and out["files_returned"] == 10 and out["next_offset"] == 110
    assert out["files"][0]["filename"] == "f0100.csv" and out["files"][0]["size_bytes"] == 5
    assert len([c for c in calls if str(c).endswith(".csv")]) == 10
