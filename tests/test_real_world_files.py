"""File shapes that real exports produce. Each of these once produced a false or oversized answer."""

from __future__ import annotations

import csv
import io

import pytest

from csv_o_matic.csvscan import ScanControl, iter_records, prefilter_needle
from csv_o_matic.service import ToolFailure

from conftest import dumps, make_service

A = "stage/a"


def write(env, name: str, data: bytes) -> None:
    (env[0] / A / name).write_bytes(data)


# ---------------------------------------------------------------- line endings

@pytest.mark.parametrize("data", [
    b"ID,Name\r00123,Acme\r00456,Beta\r",                 # CR-only (classic Mac, some exports)
    b"ID,Name\r00123,Acme\r00456,Beta",                   # ... without a final terminator
    b"ID,Name\n00123,Acme\r00456,Beta\n00789,Gamma\n",    # one stray CR in an LF file
    b"ID,Name\r\n00123,Acme\r00456,Beta\r\n",             # one stray CR in a CRLF file
    b'ID,Name\r00123,"Ac\rme"\r00456,Beta\r',             # CR-only with a CR inside quotes
])
def test_bare_carriage_returns_end_records(env, service, data):
    write(env, "cr.csv", data)
    for chunk in (4 * 1024 * 1024, 5):
        svc = make_service(env, chunk_bytes=chunk, max_record_bytes=4 * 1024 * 1024)
        found = svc.find_security_in_file(ScanControl(), A, "cr.csv", "00456", "ID")
        assert found["outcome"] == "match_found" and found["matches"][0]["values"] == {"ID": "00456", "Name": "Beta"}
        assert found["matches"][0]["record_number"] == 3
        assert svc.count_matching_rows(ScanControl(), A, "cr.csv", "00456", "ID")["count"] == 1
        assert svc.search_across_columns(ScanControl(), A, "cr.csv", "Beta")["matches"][0]["matched_columns"] == ["Name"]
        assert svc.find_security_in_file(ScanControl(), A, "cr.csv", "0456", "ID")["outcome"] == "confirmed_absent"


@pytest.mark.parametrize("chunk", [1, 2, 3, 7, 64, 4096])
def test_mixed_line_endings_match_the_csv_module(chunk):
    data = b'a,b\r1,2\r\n3,"x\ry"\n\r5,6\r\r\n7,8\r'
    expected = list(csv.reader(io.StringIO(data.decode(), newline="")))
    got = [r.fields for r in iter_records(io.BytesIO(data), chunk_bytes=chunk, max_record_bytes=1 << 20)]
    assert got == expected
    for needle in ("5", "y", "7"):
        hits = [r for r in iter_records(io.BytesIO(data), chunk_bytes=chunk, max_record_bytes=1 << 20,
                                        needle=prefilter_needle(needle, "utf-8"))]
        assert {r.number for r in hits if needle in r.fields} == {i + 1 for i, row in enumerate(expected) if needle in row}


# ---------------------------------------------------------------- encodings

@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be", "utf-32"])
def test_utf16_files_are_refused_not_reported_as_absent(env, service, encoding):
    write(env, "wide_chars.csv", "ID,Name\r\n00123,Acme\r\n".encode(encoding))
    for call in (lambda: service.search_across_columns(ScanControl(), A, "wide_chars.csv", "00123"),
                 lambda: service.inspect_csv_headers(A, "wide_chars.csv"),
                 lambda: service.count_matching_rows(ScanControl(), A, "wide_chars.csv", "00123", "ID")):
        with pytest.raises(ToolFailure) as info:
            call()
        assert info.value.kind == "unsupported_encoding"


def test_wrongly_declared_encoding_is_not_reported_as_absent(env, service):
    write(env, "cp.csv", "ID,Name\nÄ1,Müller\nB2,Plain\n".encode("cp1252"))  # folder is declared utf-8
    out = service.find_security_in_file(ScanControl(), A, "cp.csv", "Ä1", "ID")
    assert out["outcome"] == "inconclusive" and "encoding" in " ".join(out["warnings"])
    assert service.count_matching_rows(ScanControl(), A, "cp.csv", "Ä1", "ID")["count"] is None
    # ASCII identifiers are unaffected, and undecodable bytes in returned values are called out.
    plain = service.find_security_in_file(ScanControl(), A, "cp.csv", "B2", "ID")
    assert plain["outcome"] == "match_found" and "warnings" not in plain
    row = service.search_across_columns(ScanControl(), A, "cp.csv", "B2", max_rows=5)
    assert row["outcome"] == "match_found"
    # ... and the reverse: a UTF-8 file in a folder declared latin-1.
    (env[0] / "stage/semi" / "utf8.csv").write_bytes("id;name\nÄ1;x\n".encode("utf-8"))
    out = service.find_security_in_file(ScanControl(), "stage/semi", "utf8.csv", "Ä1", "id")
    assert out["outcome"] == "inconclusive" and "encoding" in " ".join(out["warnings"])
    # A genuinely absent non-ASCII identifier is still confirmed absent.
    assert service.find_security_in_file(ScanControl(), A, "cp.csv", "Ö9", "ID")["outcome"] == "confirmed_absent"


def test_undecodable_bytes_in_returned_rows_are_flagged(env, service):
    write(env, "cp.csv", "ID,Name\nB2,Müller\n".encode("cp1252"))
    out = service.find_security_in_file(ScanControl(), A, "cp.csv", "B2", "ID")
    assert out["matches"][0]["values"]["Name"] == "M�ller"
    assert "not valid utf-8" in " ".join(out["warnings"])


# ---------------------------------------------------------------- output bounds on awkward headers

def awkward_files(env):
    base = env[0] / A
    with open(base / "dups.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ID"] + ["value"] * 3000)
        w.writerow(["A1"] + ["x"] * 3000)
        w.writerow(["A1"] + ["A1"] * 3000)
    names = [f"{'n' * 150}_{i}" for i in range(600)]
    with open(base / "longnames.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ID"] + names)
        w.writerow(["A1"] + ["x"] * 600)
        w.writerow(["A1"] + ["y"] * 600)
    with open(base / "hugename.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ID", "h" * 200_000, "tail"])
        w.writerow(["A1", "v", "t"])
    return names


@pytest.mark.parametrize("limit", [8000, 12000, 60000])
def test_awkward_headers_never_exceed_the_output_limit(env, limit):
    names = awkward_files(env)
    svc = make_service(env, max_output_bytes=limit)
    c = ScanControl
    calls = {
        "dups find": lambda: svc.find_security_in_file(c(), A, "dups.csv", "A1", "ID"),
        "dups across": lambda: svc.search_across_columns(c(), A, "dups.csv", "A1"),
        "dups headers": lambda: svc.inspect_csv_headers(A, "dups.csv", sample_rows=2),
        "dups search names": lambda: svc.search_column_names(A, "dups.csv", "val"),
        "dups compare": lambda: svc.compare_security_records(c(), A, "dups.csv", A, "dups.csv", "A1", "ID", compare_columns=["ID"]),
        "long find all listed": lambda: svc.find_security_in_file(c(), A, "longnames.csv", "A1", "ID", columns=names),
        "long compare": lambda: svc.compare_security_records(c(), A, "longnames.csv", A, "longnames.csv", "A1", "ID"),
        "long compare keyed": lambda: svc.compare_security_records(c(), A, "longnames.csv", A, "longnames.csv", "A1", "ID", match_keys=[names[0]]),
        "long compare listed": lambda: svc.compare_security_records(c(), A, "longnames.csv", A, "wide.csv", "A1", "ID", compare_columns=["ID"]),
        "long headers": lambda: svc.inspect_csv_headers(A, "longnames.csv", sample_rows=1),
        "long names": lambda: svc.search_column_names(A, "longnames.csv", "n"),
        "huge find": lambda: svc.find_security_in_file(c(), A, "hugename.csv", "A1", "ID"),
        "huge headers": lambda: svc.inspect_csv_headers(A, "hugename.csv", sample_rows=1),
        "huge names": lambda: svc.search_column_names(A, "hugename.csv", "h"),
        "huge compare": lambda: svc.compare_security_records(c(), A, "hugename.csv", A, "hugename.csv", "A1", "ID"),
    }
    for label, call in calls.items():
        size = len(dumps(call()).encode()) + 60  # the server adds the tool name
        assert size <= limit, f"{label}: {size} bytes > {limit}"


def test_bounded_output_keeps_the_evidence_reachable(env):
    names = awkward_files(env)
    svc = make_service(env)
    out = svc.find_security_in_file(ScanControl(), A, "dups.csv", "A1", "ID")
    assert out["outcome"] == "match_found" and out["matches"][0]["values"]["ID"] == "A1"
    picked = svc.find_security_in_file(ScanControl(), A, "dups.csv", "A1", "ID", columns=["#3000"])
    assert picked["matches"][0]["values"] == {"value [col 3000]": "x"}
    huge = svc.find_security_in_file(ScanControl(), A, "hugename.csv", "A1", "ID")
    assert huge["matches"][0]["values"]["tail"] == "t"
    assert svc.find_security_in_file(ScanControl(), A, "hugename.csv", "v", "#2")["outcome"] == "match_found"
    last = svc.find_security_in_file(ScanControl(), A, "longnames.csv", "A1", "ID", columns=[names[-1]], max_rows=1)
    assert last["matches"][0]["values"] == {names[-1]: "x"} and last["more_matches"] == "yes"


def test_error_responses_are_bounded_too(env):
    from csv_o_matic.config import Config, Limits
    from csv_o_matic.server import build_server
    import anyio
    from mcp.client import Client

    names = awkward_files(env)

    async def main():
        cfg = Config(data_root=env[0], process_maps_dir=env[1], limits=Limits(max_output_bytes=8000))
        async with Client(build_server(cfg)) as client:
            result = await client.call_tool("find_security_in_file", {
                "folder": A, "filename": "longnames.csv", "identifier": "A1", "identifier_column": "nope",
                "columns": names})
            text = result.content[0].text
            assert len(text.encode()) <= 8000 and "column_not_found" in text and "response_too_large" not in text
    anyio.run(main)


def test_complete_scans_say_how_many_records_were_read(env, service):
    write(env, "header_only.csv", b"ID,Name\n")
    out = service.find_security_in_file(ScanControl(), A, "header_only.csv", "x", "ID")
    assert out["outcome"] == "confirmed_absent" and out["scan"]["records_after_header"] == 0
    assert "no data records" in out["explanation"]
    assert service.count_matching_rows(ScanControl(), A, "many.csv", "DUP", "ID")["scan"]["records_after_header"] == 300


# ---------------------------------------------------------------- found by independent adversarial review

def test_unbalanced_quote_in_the_header_is_an_error_not_an_absence(env, service):
    write(env, "badheader.csv", b'ID,"Name,Qty\n00123,Acme,10\nB2,Beta,5\n')
    for call in (lambda: service.find_security_in_file(ScanControl(), A, "badheader.csv", "00123", "ID"),
                 lambda: service.count_matching_rows(ScanControl(), A, "badheader.csv", "00123", "ID"),
                 lambda: service.search_across_columns(ScanControl(), A, "badheader.csv", "00123"),
                 lambda: service.inspect_csv_headers(A, "badheader.csv")):
        with pytest.raises(ToolFailure) as info:
            call()
        assert info.value.kind == "parse_error" and "never closed" in info.value.message


def test_doubled_carriage_returns_do_not_lose_matches(env, service):
    write(env, "crcrlf.csv", b'Name,ID\r\r\n"Acme, Inc.",00123\r\r\nBeta Corp,00123\r\r\nGamma,00123\r\r\n')
    assert service.count_matching_rows(ScanControl(), A, "crcrlf.csv", "00123", "ID")["count"] == 3
    names = [m["values"]["Name"] for m in service.find_security_in_file(ScanControl(), A, "crcrlf.csv", "00123", "ID")["matches"]]
    assert names == ["Acme, Inc.", "Beta Corp", "Gamma"]


def test_only_plain_csv_files_are_read(env, service):
    import gzip
    (env[0] / A / "pos.csv.gz").write_bytes(gzip.compress(b"ID\n00123\n"))
    (env[0] / A / ".env").write_text("DB_PASSWORD,hunter2\n")
    (env[0] / A / "zipped.csv").write_bytes(gzip.compress(b"ID\n00123\n"))  # compressed data under a .csv name
    for name, kind in (("pos.csv.gz", "not_a_csv_file"), (".env", "not_a_csv_file"), ("zipped.csv", "unsupported_encoding")):
        with pytest.raises(ToolFailure) as info:
            service.search_across_columns(ScanControl(), A, name, "00123")
        assert info.value.kind == kind
    with pytest.raises(ToolFailure) as info:
        service.inspect_csv_headers(A, "pos.csv")
    assert info.value.kind == "file_not_found" and "pos.csv.gz" not in info.value.details["closest_filenames"]


def test_wrong_delimiter_is_reported_not_treated_as_absent(env, service):
    write(env, "semi.csv", b"id;name;amount\n00123;Acme;2,5\n")
    out = service.search_across_columns(ScanControl(), A, "semi.csv", "00123")
    assert out["outcome"] == "inconclusive" and "probably delimited by ';'" in out["warnings"][0]
    assert service.count_matching_rows(ScanControl(), A, "semi.csv", "00123", "#1")["count"] is None
    assert "probably delimited by ';'" in service.inspect_csv_headers(A, "semi.csv")["warnings"][0]
    wide = b"|".join(b"col%d" % i for i in range(9000)) + b"\n" + b"|".join(b"v" for _ in range(9000)) + b"\n"
    write(env, "pipes.csv", wide)
    headers = service.inspect_csv_headers(A, "pipes.csv")
    assert len(dumps(headers)) < 60000 and "probably delimited by '|'" in headers["warnings"][0]
    # A genuine single-column file is not flagged.
    write(env, "one.csv", b"ID\n00123\n")
    assert service.find_security_in_file(ScanControl(), A, "one.csv", "9", "ID")["outcome"] == "confirmed_absent"


def test_headerless_file_is_flagged_when_the_value_is_in_row_one(env, service):
    write(env, "nohead.csv", b"00123,Acme,10\nB2,Beta,5\n00123,Again,1\n")
    out = service.search_across_columns(ScanControl(), A, "nohead.csv", "00123")
    assert out["outcome"] == "match_found" and out["results_are_partial"] and out["more_matches"] == "unknown"
    assert "may have no header row" in out["warnings"][0]
    count = service.count_matching_rows(ScanControl(), A, "nohead.csv", "00123", "#1")
    assert count["count"] is None and count["matches_seen_before_interruption"] == 1
    write(env, "nohead2.csv", b"00123,Acme,10\nB2,Beta,5\n")
    assert service.search_across_columns(ScanControl(), A, "nohead2.csv", "00123")["outcome"] == "inconclusive"


def test_unreliable_scans_never_claim_all_matches_were_returned(env, service):
    write(env, "eofquote.csv", b'ID,Name\n00123,Acme\nX9,"unterminated\n00123,Beta\n')
    out = service.find_security_in_file(ScanControl(), A, "eofquote.csv", "00123", "ID")
    assert out["outcome"] == "match_found" and out["matches_returned"] == 1
    assert out["more_matches"] == "unknown" and out["results_are_partial"] and "PARTIAL" in out["explanation"]
    hidden = out["near_misses"]["inside_a_multi_line_value_in_another_column"]  # the swallowed row is visible
    assert hidden["count"] == 1 and "00123,Beta" in hidden["examples"][0]["value_excerpt"]


def test_repeated_column_names_are_never_paired_by_position(env, service):
    base = env[0] / "stage/b"
    (base / "s1.csv").write_text("ID,Name,Price,Price\n00123,Acme,10.00,12.50\n")
    (base / "s2.csv").write_text("ID,Name,Ccy,Price,Price\n00123,Acme,USD,10.00,12.50\n")
    out = service.compare_security_records(ScanControl(), "stage/b", "s1.csv", "stage/b", "s2.csv", "00123", "ID")
    pair = out["comparisons"][0]
    assert pair["changed"] == [] and [u["column_a"] for u in pair["unchanged"]] == ["ID", "Name"]
    assert out["columns_not_compared"]["only_in_file_a"] == ["Price [col 3]", "Price [col 4]"]
    # They can be compared deliberately, by position.
    out = service.compare_security_records(ScanControl(), "stage/b", "s1.csv", "stage/b", "s2.csv", "00123", "ID",
                                           compare_columns=["#3", "#4"], column_mapping={"#3": "#4", "#4": "#5"})
    assert out["comparisons"][0]["changed"] == [] and out["comparisons"][0]["unchanged_count"] == 2


def test_column_mapping_to_a_position_is_honoured_without_compare_columns(env, service):
    base = env[0] / "stage/b"
    (base / "m1.csv").write_text("ID,Price\n00123,10\n")
    (base / "m2.csv").write_text("ID,px,px\n00123,11,12\n")
    out = service.compare_security_records(ScanControl(), "stage/b", "m1.csv", "stage/b", "m2.csv", "00123", "ID",
                                           column_mapping={"Price": "#2"})
    assert out["comparisons"][0]["changed"] == [{"column_a": "Price", "column_b": "px [col 2]", "value_a": "10", "value_b": "11"}]
    with pytest.raises(ToolFailure):  # a mapping that cannot be resolved is an error, not silently dropped
        service.compare_security_records(ScanControl(), "stage/b", "m1.csv", "stage/b", "m2.csv", "00123", "ID",
                                         column_mapping={"Price": "nope"})


def test_generated_labels_never_collide_with_real_names(env, service):
    write(env, "collide.csv", b"ID,Px,Px,Px [col 2]\n00123,1,2,3\n")
    values = service.find_security_in_file(ScanControl(), A, "collide.csv", "00123", "ID")["matches"][0]["values"]
    assert sorted(values.values()) == ["00123", "1", "2", "3"] and len(values) == 4


def test_conflicting_csv_formats_across_maps_are_reported(env, service):
    (env[1] / "other.yaml").write_text(
        "id: other\nname: Other\ndescription: d\npurpose: p\nrun_semantics: r\n"
        "folders:\n  - {id: b, path: stage/b, description: d, csv_format: {delimiter: ';', encoding: latin-1}}\n"
        "steps:\n  - {id: s, name: s, description: s, reads: [b]}\n")
    (env[0] / "stage/b" / "semi.csv").write_bytes("id;name\nÄ1;Müller\n".encode("latin-1"))
    # Only one map states a format for stage/b, so it applies.
    out = service.find_security_in_file(ScanControl(), "stage/b", "semi.csv", "Ä1", "id")
    assert out["matches"][0]["values"] == {"id": "Ä1", "name": "Müller"}
    # Two maps stating different formats is a conflict the reader will not guess about.
    (env[1] / "third.yaml").write_text(
        "id: third\nname: Third\ndescription: d\npurpose: p\nrun_semantics: r\n"
        "folders:\n  - {id: b, path: stage/b, description: d, csv_format: {delimiter: '|'}}\n"
        "steps:\n  - {id: s, name: s, description: s, reads: [b]}\n")
    with pytest.raises(ToolFailure) as info:
        service.find_security_in_file(ScanControl(), "stage/b", "semi.csv", "Ä1", "id")
    assert info.value.kind == "process_map_conflict"


def test_symlinks_are_neither_listed_nor_read(env, service):
    import os
    (env[0] / "outside.csv").write_text("ID,Secret\n00123,TOPSECRET\n")
    os.symlink(env[0] / "outside.csv", env[0] / A / "sneaky.csv")
    assert "sneaky.csv" not in [f["filename"] for f in service.list_csv_files(A)["files"]]
    with pytest.raises(ToolFailure) as info:
        service.search_across_columns(ScanControl(), A, "sneaky.csv", "00123")
    assert info.value.kind == "file_unreadable"


def test_a_file_swapped_for_a_link_after_the_check_is_refused(env, service):
    import os
    (env[0] / "outside.csv").write_text("ID,Secret\n00123,TOPSECRET\n")
    target = env[0] / A / "swap.csv"
    target.write_text("ID,Name\n1,x\n")
    of = service._open(A, "swap.csv")
    target.unlink()
    os.symlink(env[0] / "outside.csv", target)  # the swap happens between the check and the open
    with pytest.raises(ToolFailure) as info:
        service._fh(of)
    assert info.value.kind == "file_unreadable"
    target.unlink()
    target.write_text("ID,Name\n2,y\n")  # ... or it is replaced by a different regular file
    with pytest.raises(ToolFailure):
        service._fh(of)


def test_header_reading_obeys_the_time_limit(env):
    import time
    write(env, "runaway.csv", b'"Holdings extract,generated\n' + b"00123,Acme,10\n" * 400_000)
    svc = make_service(env)
    started = time.monotonic()
    with pytest.raises(ToolFailure) as info:
        svc.find_security_in_file(ScanControl(max_seconds=0.3), A, "runaway.csv", "00123", "#1")
    assert time.monotonic() - started < 3 and info.value.kind in ("header_not_read", "parse_error")
    started = time.monotonic()
    with pytest.raises(ToolFailure):
        svc.inspect_csv_headers(A, "runaway.csv")
    assert time.monotonic() - started < 5


def test_oversized_step_detail_is_cut_not_refused(env):
    path = env[1] / "demo.yaml"
    mappings = "".join(f"      - {{from_folder: stage_a, from_column: c{i}, to_folder: stage_b, to_column: d{i}, rule: 'rule number {i}'}}\n"
                       for i in range(400))
    path.write_text(path.read_text().replace("    reads: [stage_a]\n    writes: [stage_b]\n",
                                             "    reads: [stage_a]\n    writes: [stage_b]\n    field_mappings:\n" + mappings))
    svc = make_service(env, max_output_bytes=8000)
    out = svc.get_process_map("demo", step_id="second")
    assert len(dumps(out)) < 8000 and out["truncated"] and "of 400 shown" in out["truncation_note"]
    assert len(out["step"]["field_mappings"]) > 5 and out["step"]["filter_rules"]


def test_non_breaking_space_in_a_valid_utf8_file_is_not_an_encoding_problem(env, service):
    """The cp1252 byte for NBSP (A0) is also the second byte of its UTF-8 form: no false alarm."""
    write(env, "nbsp.csv", "ID,Name\n ,one\nx, \né1,two\n".encode("utf-8"))
    out = service.find_security_in_file(ScanControl(), A, "nbsp.csv", " ", "ID")
    assert out["outcome"] == "match_found" and out["more_matches"] == "none" and "warnings" not in out
    assert service.count_matching_rows(ScanControl(), A, "nbsp.csv", " ", "ID")["count"] == 1
    assert service.find_security_in_file(ScanControl(), A, "nbsp.csv", "  ", "ID")["outcome"] == "confirmed_absent"


def test_paging_continues_when_a_warning_makes_the_scan_unreliable(env):
    """A layout warning must not strand matches behind a missing cursor."""
    write(env, "nohead.csv", b"DUP,x\n" + b"DUP,y\n" * 9)
    svc = make_service(env, max_rows_per_response=4)
    seen, cursor = 0, None
    while True:
        out = svc.find_security_in_file(ScanControl(), A, "nohead.csv", "DUP", "#1", cursor=cursor)
        seen += out["matches_returned"]
        cursor = out["next_cursor"]
        if not cursor:
            break
    assert seen == 9 and out["more_matches"] == "unknown" and "may have no header row" in out["warnings"][0]


def test_cp1252_file_in_a_latin1_folder(env, service):
    """The euro sign exists in cp1252 but not in latin-1: its bytes in the file are flagged, not ignored."""
    (env[0] / "stage/semi" / "euro.csv").write_bytes("id;name\n€1;x\nB2;y\n".encode("cp1252"))
    out = service.find_security_in_file(ScanControl(), "stage/semi", "euro.csv", "€1", "id")
    assert out["outcome"] == "inconclusive" and "cp1252" in out["warnings"][0]
    assert service.find_security_in_file(ScanControl(), "stage/semi", "euro.csv", "B2", "id")["outcome"] == "match_found"
