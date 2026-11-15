from __future__ import annotations

import csv
import os

import pytest

from csv_o_matic.csvscan import ScanControl
from csv_o_matic.service import ToolFailure

from conftest import digest, make_service, wire_bytes

A = "stage/a"
B = "stage/b"
TRICKY = "a_20261112_20261112T180000.csv"


# ---------------------------------------------------------------- process maps and folders

def test_process_listing_then_full_map(service):
    listing = service.get_process_map()
    assert listing["processes"] == [{"id": "demo", "name": "Demo Process", "description": "Two-stage demo."}]
    for key in ("demo", "Demo Process", "DEMO"):
        full = service.get_process_map(key)["process"]
        assert full["purpose"] and full["run_semantics"]
    steps = {s["id"]: s for s in full["steps"]}
    assert steps["first"]["downstream"] == ["second", "third"]  # a branch
    assert steps["second"]["upstream"] == ["first"] and steps["third"]["upstream"] == ["first"]
    assert steps["third"]["branch_condition"] == "Only bonds."
    assert steps["second"]["filter_rules"] == ["Drops rows with Qty = 0."]
    folder = next(f for f in full["folders"] if f["id"] == "stage_a")
    assert folder["filename_convention"]["tokens"]["run_timestamp"]["meaning"] == "Run start, UTC"
    assert folder["identifier_columns"][0]["column"] == "ID"


def test_unknown_process_and_broken_map_are_reported(env, service):
    with pytest.raises(ToolFailure) as info:
        service.get_process_map("nope")
    assert info.value.kind == "process_not_found" and info.value.details["available_processes"]
    (env[1] / "broken.yaml").write_text("id: broken\nname: B\nsteps: []\ntypo_key: 1\n")
    listing = service.get_process_map()
    assert [p["id"] for p in listing["processes"]] == ["demo"]
    assert listing["process_map_load_errors"][0]["file"] == "broken.yaml"


def test_oversized_map_degrades_to_overview(env):
    path = env[1] / "demo.yaml"
    path.write_text(path.read_text().replace(
        'filter_rules: ["Drops rows with Qty = 0."]',
        "filter_rules:\n" + "".join(f'      - "Rule {i}: drops rows where some long condition number {i} holds."\n' for i in range(150))))
    small = make_service(env, max_output_bytes=16000)
    out = small.get_process_map("demo")
    assert out["truncated"] and wire_bytes(out) < 16000
    assert [s["id"] for s in out["process"]["steps"]] == ["first", "second", "third"]
    step = small.get_process_map("demo", step_id="second")
    assert step["step"]["filter_rules"] and {f["id"] for f in step["folders"]} == {"stage_a", "stage_b"}


def test_list_folders(service):
    out = service.list_folders()
    assert [f["folder"] for f in out["folders"]] == ["stage/a", "stage/b", "stage/semi"]
    a = out["folders"][0]
    assert a["exists_on_disk"] and a["description"] == "First stage output." and "description" not in a["used_by"][0]
    assert a["identifier_columns"] == [{"column": "ID", "identifier_type": "internal"}]
    roles = {(s["step_id"], s["role"]) for s in a["used_by"][0]["steps"]}
    assert roles == {("first", "writes"), ("second", "reads"), ("third", "reads")}
    assert service.list_folders("demo")["total_folders"] == 3


# ---------------------------------------------------------------- listing files

def test_list_csv_files_separates_filename_dates_from_mtime(env, service):
    old = env[0] / A / TRICKY
    os.utime(old, (1_600_000_000, 1_600_000_000))  # written to disk in 2020, business date 2026
    out = service.list_csv_files(A)
    names = [f["filename"] for f in out["files"]]
    assert "readme.txt" not in names and out["other_entries_ignored"] == 1
    item = next(f for f in out["files"] if f["filename"] == TRICKY)
    assert item["filename_tokens"] == {"business_date": "2026-11-12", "run_timestamp": "2026-11-12T18:00:00"}
    assert item["filesystem_modified_time"].startswith("2020-09-13")
    assert item["size_bytes"] == old.stat().st_size
    misc = next(f for f in out["files"] if f["filename"] == "misc_notes.csv")
    assert misc["matches_filename_convention"] is False and misc["filename_tokens"] is None


def test_list_csv_files_filters(service):
    by_date = service.list_csv_files(A, date_from="2026-11-13", date_to="2026-11-13")
    # Two runs for the same business date: a retry, visible through distinct run timestamps.
    assert [f["filename_tokens"]["run_timestamp"] for f in by_date["files"]] == ["2026-11-13T19:30:00", "2026-11-13T18:00:00"]
    assert by_date["filters"]["date_field"] == "business_date"
    assert by_date["files_excluded_because_filename_has_no_parseable_date"] == 3
    assert [f["filename"] for f in service.list_csv_files(A, filename_contains="MISC")["files"]] == ["misc_notes.csv"]
    assert service.list_csv_files(A, date_from="2000-01-01", date_field="filesystem_modified")["total_matching_files"] == 6
    assert service.list_csv_files(A, date_to="2000-01-01", date_field="filesystem_modified")["total_matching_files"] == 0
    with pytest.raises(ToolFailure):
        service.list_csv_files(B, date_from="2026-11-13")  # folder has no filename convention
    with pytest.raises(ToolFailure):
        service.list_csv_files(A, date_from="13/11/2026")


def test_listing_is_paged(env):
    out = make_service(env, max_files_per_listing=2).list_csv_files(A, newest_first=False)
    assert out["files_returned"] == 2 and out["truncated"] and out["next_offset"] == 2 and out["total_matching_files"] == 6


def test_only_mapped_folders_and_plain_filenames_are_reachable(env, service):
    (env[0] / "secret").mkdir()
    (env[0] / "secret" / "s.csv").write_text("a\n1\n")
    for folder in ("secret", "stage/a/../../secret", "/etc", ""):
        with pytest.raises(ToolFailure) as info:
            service.list_csv_files(folder)
        assert info.value.kind == "folder_not_found"
    for name in ("../../secret/s.csv", "/etc/passwd", "..", ""):
        with pytest.raises(ToolFailure) as info:
            service.inspect_csv_headers(A, name)
        assert info.value.kind == "invalid_argument"
    os.symlink(env[0] / "secret" / "s.csv", env[0] / A / "link.csv")
    with pytest.raises(ToolFailure) as info:
        service.inspect_csv_headers(A, "link.csv")
    assert info.value.kind == "file_unreadable"
    with pytest.raises(ToolFailure) as info:
        service.inspect_csv_headers(A, "a_20261112_20261112T180001.csv")
    assert info.value.kind == "file_not_found" and info.value.details["closest_filenames"][0] == TRICKY


# ---------------------------------------------------------------- headers

def test_headers_without_and_with_sample(service):
    out = service.inspect_csv_headers(A, TRICKY)
    assert out["columns"] == ["ID", "Name", "Notes", "AltID", "Qty"] and "sample_rows" not in out
    out = service.inspect_csv_headers(A, TRICKY, sample_rows=2)
    assert [r["values"]["ID"] for r in out["sample_rows"]] == ["00123", "123"]
    assert out["sample_rows"][0]["values"]["Notes"] == "line1\nline2"
    out = service.inspect_csv_headers(A, TRICKY, sample_rows=1, sample_columns=["Qty"])
    assert out["sample_rows"][0]["values"] == {"Qty": "10"}
    with pytest.raises(ToolFailure):
        service.inspect_csv_headers(A, TRICKY, sample_rows=11)


def test_wide_headers(env, service):
    out = service.inspect_csv_headers(A, "wide.csv")
    assert out["column_count"] == 600 and len(out["columns"]) == 600 and out["columns"][0] == "ID"
    found = service.search_column_names(A, "wide.csv", "PX_")
    assert found["matching_column_count"] == 11
    assert found["matching_columns"][0] == {"column": "px_field_050", "position": 51, "selector": "px_field_050"}
    assert service.search_column_names(A, "wide.csv", "zzz")["matching_columns"] == []
    with open(env[0] / A / "wider.csv", "w") as fh:
        fh.write(",".join(f"column_{i:04d}" for i in range(2000)) + "\n")
    small = make_service(env, max_output_bytes=16000)
    page = small.inspect_csv_headers(A, "wider.csv")
    assert page["columns_truncated"] and wire_bytes(page) < 16000
    seen, offset = list(page["columns"]), page["next_column_offset"]
    while offset is not None:
        rest = small.inspect_csv_headers(A, "wider.csv", column_offset=offset)
        assert rest["columns"][0] == f"column_{offset:04d}"
        seen += rest["columns"]
        offset = rest.get("next_column_offset")
    assert seen == [f"column_{i:04d}" for i in range(2000)]
    sample = small.inspect_csv_headers(A, "wide.csv", sample_rows=1)
    assert sample["sample_truncated"] and wire_bytes(sample) < 16000


def test_duplicate_column_names(env, service):
    (env[0] / A / "dupes.csv").write_text("ID,Val,Val\n7,first,second\n")
    out = service.find_security_in_file(ScanControl(), A, "dupes.csv", "7", "ID")
    assert out["matches"][0]["values"] == {"ID": "7", "Val [col 2]": "first", "Val [col 3]": "second"}
    with pytest.raises(ToolFailure) as info:
        service.find_security_in_file(ScanControl(), A, "dupes.csv", "first", "Val")
    assert info.value.kind == "ambiguous_column"
    assert service.find_security_in_file(ScanControl(), A, "dupes.csv", "second", "#3")["outcome"] == "match_found"


# ---------------------------------------------------------------- find / search / count

def test_find_exact_with_leading_zeroes_quotes_and_duplicates(service, ctl):
    out = service.find_security_in_file(ctl(), A, TRICKY, "00123", "ID")
    assert out["outcome"] == "match_found" and out["more_matches"] == "none" and not out["results_are_partial"]
    assert out["scan"]["complete"] and out["scan"]["stop_reason"] == "end_of_file"
    assert out["file"]["filename"] == TRICKY and out["search"]["identifier"] == "00123" and out["search"]["column"] == "ID"
    assert [m["record_number"] for m in out["matches"]] == [2, 4]
    assert out["matches"][0]["values"] == {"ID": "00123", "Name": "Acme, Inc.", "Notes": "line1\nline2", "AltID": "X1", "Qty": "10"}
    assert out["matches"][1]["values"]["Name"] == 'Say "hi"'
    # ' 00123 ' is reported as a near miss, never as a match; so is the value sitting in another column.
    near = out["near_misses"]
    assert near["same_value_with_surrounding_whitespace"] == {
        "count": 1, "examples": [{"record_number": 5, "column": "ID", "value": " 00123 "}]}
    assert near["exact_value_in_other_columns"] == {"AltID": 1} and "NOT exact matches" in near["note"]

    stripped = service.find_security_in_file(ctl(), A, TRICKY, "123", "ID")
    assert [m["values"]["Name"] for m in stripped["matches"]] == ["Beta Corp"]

    picked = service.find_security_in_file(ctl(), A, TRICKY, "00123", "ID", columns=["Qty", "Name"])
    assert picked["matches"][0]["values"] == {"Qty": "10", "Name": "Acme, Inc."}

    multi = service.find_security_in_file(ctl(), A, TRICKY, "quoted\nmulti\nline, with comma", "Name")
    assert [m["record_number"] for m in multi["matches"]] == [6]


def test_confirmed_absence_and_missing_column(service, ctl):
    out = service.find_security_in_file(ctl(), A, TRICKY, "0123", "ID")
    assert out["outcome"] == "confirmed_absent" and out["scan"]["complete"] and out["matches"] == []
    assert "Complete scan" in out["explanation"]
    # Present in the file, but not in the selected column.
    assert service.find_security_in_file(ctl(), A, TRICKY, "X1", "ID")["outcome"] == "confirmed_absent"
    with pytest.raises(ToolFailure) as info:
        service.find_security_in_file(ctl(), A, TRICKY, "00123", "id")
    assert info.value.kind == "column_not_found" and info.value.details["similar_column_names"] == ["ID"]
    with pytest.raises(ToolFailure):
        service.find_security_in_file(ctl(), A, TRICKY, "", "ID")


def test_search_across_columns_reports_each_row_once(service, ctl):
    out = service.search_across_columns(ctl(), A, TRICKY, "00123")
    assert [(m["record_number"], m["matched_columns"]) for m in out["matches"]] == [
        (2, ["ID"]), (3, ["AltID"]), (4, ["ID", "AltID"])]
    picked = service.search_across_columns(ctl(), A, TRICKY, "00123", columns=["Qty"])
    assert picked["matches"][2]["values"] == {"Qty": "7", "ID": "00123", "AltID": "00123"}
    assert service.search_across_columns(ctl(), A, TRICKY, "Acme")["outcome"] == "confirmed_absent"  # no substring matching


def test_count(service, ctl):
    out = service.count_matching_rows(ctl(), A, "many.csv", "DUP", "ID")
    assert out["count"] == 100 and out["count_is_exact"] and out["outcome"] == "match_found" and "matches" not in out
    none = service.count_matching_rows(ctl(), A, "many.csv", "NOPE", "ID")
    assert none["count"] == 0 and none["count_is_exact"] and none["outcome"] == "confirmed_absent"


def test_row_limit_truncation_and_cursor_paging(service, ctl):
    seen, cursor, pages = [], None, 0
    while True:
        out = service.find_security_in_file(ctl(), A, "many.csv", "DUP", "ID", max_rows=30, cursor=cursor)
        pages += 1
        seen += [m["values"]["Seq"] for m in out["matches"]]
        assert out["outcome"] == "match_found"
        if out["more_matches"] == "none":
            assert out["next_cursor"] is None and not out["results_are_partial"] and out["scan"]["complete"]
            break
        assert out["results_are_partial"] and out["more_matches"] == "yes" and "PARTIAL" in out["explanation"]
        assert out["scan"]["stop_reason"] == "row_limit" and not out["scan"]["complete"]
        cursor = out["next_cursor"]
    assert pages == 4 and seen == [str(i) for i in range(0, 300, 3)]  # every match exactly once, in order


def test_server_row_limit_caps_the_request(env, ctl):
    out = make_service(env, max_rows_per_response=5).find_security_in_file(ctl(), A, "many.csv", "DUP", "ID", max_rows=1000)
    assert out["matches_returned"] == 5 and out["search"]["max_rows"] == 5 and out["more_matches"] == "yes"


def test_output_size_truncation(env, ctl):
    small = make_service(env, max_output_bytes=16000, max_rows_per_response=500)
    seen, cursor = [], None
    while True:
        out = small.find_security_in_file(ctl(), A, "many.csv", "DUP", "ID", cursor=cursor)
        assert wire_bytes(out) <= 16000
        seen += [m["values"]["Seq"] for m in out["matches"]]
        if out["more_matches"] == "none":
            break
        assert out["scan"]["stop_reason"] == "output_size_limit" and out["results_are_partial"]
        cursor = out["next_cursor"]
    assert seen == [str(i) for i in range(0, 300, 3)]
    # A single row wider than the whole budget is still returned, visibly cut down.
    wide = small.find_security_in_file(ctl(), A, "wide.csv", "W0003", "ID")
    row = wide["matches"][0]
    assert wire_bytes(wide) <= 16000 and row["values_omitted_for_size"] > 0 and row["values"]["ID"] == "W0003"
    assert small.find_security_in_file(ctl(), A, "wide.csv", "W0003", "ID", columns=["col_598"])["matches"][0]["values"] == {"col_598": "v3_598"}


def test_long_cells_are_cut_and_flagged(env, ctl):
    (env[0] / A / "long.csv").write_text("ID,Blob\n1," + "z" * 5000 + "\n")
    row = make_service(env, max_cell_chars=100).find_security_in_file(ctl(), A, "long.csv", "1", "ID")["matches"][0]
    assert row["truncated_cells"] == ["Blob"] and "5000 characters" in row["values"]["Blob"]


def test_cursor_is_bound_to_its_search_and_file(env, service, ctl):
    cursor = service.find_security_in_file(ctl(), A, "many.csv", "DUP", "ID", max_rows=1)["next_cursor"]
    for call in (
        lambda: service.find_security_in_file(ctl(), A, "many.csv", "S1", "ID", cursor=cursor),
        lambda: service.count_matching_rows(ctl(), A, "many.csv", "DUP", "ID", cursor=cursor),
        lambda: service.find_security_in_file(ctl(), A, "many.csv", "DUP", "ID", cursor="garbage"),
    ):
        with pytest.raises(ToolFailure) as info:
            call()
        assert info.value.kind == "invalid_cursor"
    with open(env[0] / A / "many.csv", "a") as fh:  # the ETL rewrites the file
        fh.write("DUP,999,late\n")
    with pytest.raises(ToolFailure) as info:
        service.find_security_in_file(ctl(), A, "many.csv", "DUP", "ID", cursor=cursor)
    assert info.value.kind == "invalid_cursor"


def test_time_limit_gives_inconclusive_then_resumable(env):
    service = make_service(env, chunk_bytes=512, max_record_bytes=4096)
    out = service.find_security_in_file(ScanControl(max_seconds=0.0), A, "many.csv", "NOPE", "ID")
    assert out["outcome"] == "inconclusive" and out["scan"]["stop_reason"] == "time_limit" and not out["scan"]["complete"]
    assert "INCONCLUSIVE" in out["explanation"] and out["more_matches"] == "unknown"
    resumed = service.find_security_in_file(ScanControl(), A, "many.csv", "NOPE", "ID", cursor=out["next_cursor"])
    assert resumed["outcome"] == "confirmed_absent"

    count = service.count_matching_rows(ScanControl(max_seconds=0.0), A, "many.csv", "DUP", "ID")
    assert count["count"] is None and count["count_is_exact"] is False and "NOT available" in count["explanation"]
    assert service.count_matching_rows(ScanControl(), A, "many.csv", "DUP", "ID", cursor=count["next_cursor"])["count"] == 100


def test_cancelled_scan_reports_partial_results(env):
    """Cancel mid-scan: what was found is returned, marked partial; the count is withheld."""

    def cancel_after_five_matches(svc):
        original = svc._iter_matches

        def cancelling(*args, **kwargs):
            control = next(a for a in args if isinstance(a, ScanControl))
            for n, item in enumerate(original(*args, **kwargs)):
                if n == 4:
                    control.stop.set()
                yield item

        svc._iter_matches = cancelling
        return svc

    small = cancel_after_five_matches(make_service(env, chunk_bytes=512, max_record_bytes=4096))
    out = small.find_security_in_file(ScanControl(), A, "many.csv", "DUP", "ID")
    assert out["scan"]["stop_reason"] == "cancelled" and not out["scan"]["complete"]
    assert out["outcome"] == "match_found" and out["results_are_partial"] and out["more_matches"] == "unknown"
    assert 0 < out["matches_returned"] < 100 and "PARTIAL" in out["explanation"]

    count = small.count_matching_rows(ScanControl(), A, "many.csv", "DUP", "ID")
    assert count["scan"]["stop_reason"] == "cancelled"
    assert count["count"] is None and not count["count_is_exact"] and 0 < count["matches_seen_before_interruption"] < 100

    nothing = small.find_security_in_file(ScanControl(), A, "many.csv", "NOPE", "ID")  # no matches, so never cancelled
    assert nothing["outcome"] == "confirmed_absent"
    control = ScanControl()
    control.stop.set()
    with pytest.raises(ToolFailure) as info:  # cancelled before anything was read: no evidence at all
        small.find_security_in_file(control, A, "many.csv", "NOPE", "ID")
    assert info.value.kind == "cancelled"


def test_parse_failure_is_explained_not_reported_as_absent(env, ctl):
    (env[0] / A / "broken.csv").write_text('ID,Name\n1,ok\n2,"never closed\n' + "3,x\n" * 3000)
    small = make_service(env, chunk_bytes=1024, max_record_bytes=4096)
    out = small.find_security_in_file(ctl(), A, "broken.csv", "3", "ID")
    assert out["outcome"] == "inconclusive" and out["error"]["type"] == "parse_error" and out["scan"]["stop_reason"] == "error"
    assert "unterminated quote" in out["error"]["message"] and out["next_cursor"] is None
    partial = small.find_security_in_file(ctl(), A, "broken.csv", "1", "ID")
    assert partial["outcome"] == "match_found" and partial["results_are_partial"] and partial["more_matches"] == "unknown"
    assert small.count_matching_rows(ctl(), A, "broken.csv", "3", "ID")["count"] is None

    (env[0] / A / "eof_quote.csv").write_text('ID,Name\n1,ok\n2,"never closed\n3,x\n')
    out = make_service(env).find_security_in_file(ctl(), A, "eof_quote.csv", "3", "ID")
    assert out["outcome"] == "inconclusive" and "unterminated quote" in out["warnings"][0]

    (env[0] / A / "empty.csv").write_text("")
    with pytest.raises(ToolFailure) as info:
        make_service(env).find_security_in_file(ctl(), A, "empty.csv", "3", "ID")
    assert info.value.kind == "parse_error"


def test_unreadable_file(env, service, ctl):
    path = env[0] / A / "locked.csv"
    path.write_text("ID\n1\n")
    path.chmod(0)
    if os.access(path, os.R_OK):
        pytest.skip("running as a user that ignores file permissions")
    with pytest.raises(ToolFailure) as info:
        service.find_security_in_file(ctl(), A, "locked.csv", "1", "ID")
    assert info.value.kind == "file_unreadable"


def test_ragged_rows_are_flagged(env, service, ctl):
    (env[0] / A / "ragged.csv").write_text("ID,A,B\nR1,only\nR2,a,b,extra\n")
    short = service.find_security_in_file(ctl(), A, "ragged.csv", "R1", "ID")["matches"][0]
    assert short["values"] == {"ID": "R1", "A": "only"} and short["field_count_mismatch"] == {"header_columns": 3, "record_fields": 2}
    long = service.search_across_columns(ctl(), A, "ragged.csv", "extra")["matches"][0]
    assert long["matched_columns"] == ["[extra field 4]"] and long["values"]["[extra field 4]"] == "extra"


def test_delimiter_and_encoding_from_process_map(service, ctl):
    out = service.find_security_in_file(ctl(), "stage/semi", "semi.csv", "Ä1", "id")
    assert out["matches"][0]["values"] == {"id": "Ä1", "name": "Müller; Söhne", "amount": "1,5"}


# ---------------------------------------------------------------- compare

def test_compare_single_records_with_mapping(service, ctl):
    out = service.compare_security_records(
        ctl(), B, "left.csv", B, "right.csv", "00777", "ID", identifier_column_b="sec_id",
        compare_columns=["Price", "Name", "Qty"], column_mapping={"Price": "px"})
    assert out["comparison_status"] == "compared"
    assert out["presence"]["file_a"]["outcome"] == out["presence"]["file_b"]["outcome"] == "match_found"
    (pair,) = out["comparisons"]
    assert pair["changed"] == [{"column_a": "Name", "value_a": "Solo", "value_b": "Solo ", "note": "equal after trimming whitespace"}]
    assert pair["unchanged"] == [{"column_a": "Price", "column_b": "px", "value": "5"}, {"column_a": "Qty", "value": "1"}]
    assert out["file_a"]["filename"] == "left.csv" and out["search"]["identifier_column_b"] == "sec_id"


def test_compare_presence(service, ctl):
    args = dict(identifier_column_a="ID", column_mapping={"ID": "sec_id"})
    one = service.compare_security_records(ctl(), B, "left.csv", B, "right.csv", "00999", **args)
    assert one["comparison_status"] == "present_in_one_file_only"
    assert one["presence"]["file_a"]["outcome"] == "match_found" and one["presence"]["file_b"]["outcome"] == "confirmed_absent"
    assert one["records_in_file_a"][0]["values"]["Name"] == "OnlyLeft"
    none = service.compare_security_records(ctl(), B, "left.csv", B, "right.csv", "123", **args)  # no leading-zero stripping
    assert none["comparison_status"] == "absent_from_both"


def test_compare_ambiguous_then_resolved_with_keys(service, ctl):
    args = dict(identifier_column_a="ID", column_mapping={"ID": "sec_id", "Portfolio": "pf", "Price": "px"})
    amb = service.compare_security_records(ctl(), B, "left.csv", B, "right.csv", "00123", **args)
    assert amb["comparison_status"] == "ambiguous_alignment" and "comparisons" not in amb
    assert amb["presence"]["file_a"]["matching_records_found"] == 2 and amb["presence"]["file_b"]["matching_records_found"] == 3
    assert "Portfolio" in amb["columns_that_differ_between_matches"]["file_a"]
    assert len(amb["records_in_file_b"]) == 3

    out = service.compare_security_records(ctl(), B, "left.csv", B, "right.csv", "00123", match_keys=["Portfolio"], **args)
    assert out["comparison_status"] == "partially_compared"
    by_key = {c["key"]["Portfolio"]: c for c in out["comparisons"]}
    assert set(by_key) == {"P1", "P2"}
    p1 = {c["column_a"]: c for c in by_key["P1"]["changed"]}
    assert set(p1) == {"Name"} and p1["Name"]["note"] == "equal ignoring letter case"
    p2 = {c["column_a"]: c for c in by_key["P2"]["changed"]}
    assert p2["Price"] == {"column_a": "Price", "column_b": "px", "value_a": "100.50", "value_b": "100.5",
                           "note": "numerically equal; the text formatting differs"}
    assert p2["Qty"]["value_a"] == "20" and p2["Qty"]["value_b"] == "25" and "note" not in p2["Qty"]
    assert out["unmatched_records_in_file_b"] == [{"record_number": 4, "key": {"pf": "P3"}}]
    assert out["columns_not_compared"]["only_in_file_b"] == ["Extra"]

    still = service.compare_security_records(ctl(), B, "left.csv", B, "right.csv", "00123", match_keys=["Price"], **args)
    assert still["comparison_status"] == "ambiguous_alignment" and still["ambiguous_keys"]


def test_compare_interrupted_and_bad_columns(env, service):
    with open(env[0] / B / "left.csv", "a") as fh:
        fh.write("".join(f"9{i:04d},P1,1,Filler,1\n" for i in range(500)))
    tiny = make_service(env, chunk_bytes=512, max_record_bytes=4096)
    out = tiny.compare_security_records(ScanControl(max_seconds=0.0), B, "left.csv", B, "right.csv", "00123", "ID", "sec_id")
    assert out["comparison_status"] == "inconclusive" and out["presence"]["file_b"]["stop_reason"] == "not_scanned"
    with pytest.raises(ToolFailure) as info:
        service.compare_security_records(ScanControl(), B, "left.csv", B, "right.csv", "00123", "ID")
    assert info.value.kind == "column_not_found" and "right.csv" in info.value.message


def test_compare_respects_output_limit(env, ctl):
    with open(env[0] / B / "wide2.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ID"] + [f"col_{i:03d}" for i in range(1, 600)])
        w.writerow(["W0003"] + ["changed"] * 599)
    small = make_service(env, max_output_bytes=16000)
    out = small.compare_security_records(ctl(), A, "wide.csv", B, "wide2.csv", "W0003", "ID")
    pair = out["comparisons"][0]
    assert wire_bytes(out) <= 16000 and pair["truncated"] and pair["changed_count"] == 588 and pair["unchanged_count"] == 1
    assert len(pair["changed"]) < 588 and pair["columns_omitted_for_size"]


# ---------------------------------------------------------------- read-only

def test_source_files_are_never_modified(env, service, ctl):
    before = digest(env[0])
    service.list_csv_files(A)
    service.inspect_csv_headers(A, TRICKY, sample_rows=3)
    service.search_column_names(A, "wide.csv", "px")
    service.find_security_in_file(ctl(), A, TRICKY, "00123", "ID")
    service.search_across_columns(ctl(), A, TRICKY, "00123")
    service.count_matching_rows(ctl(), A, "many.csv", "DUP", "ID")
    service.compare_security_records(ctl(), B, "left.csv", B, "right.csv", "00777", "ID", "sec_id")
    assert digest(env[0]) == before
    assert sorted(p.name for p in env[0].rglob("*") if p.is_file()) == sorted(os.path.basename(k) for k in before)


def test_near_misses_explain_an_absence(env, service, ctl):
    """A lost leading zero, a partial name and a wrong column all come back as hints, not as matches."""
    (env[0] / A / "master.csv").write_text(
        "sedol,name,alt\n0263494,BAE Systems PLC,X\n2073390,Berkshire,0263494\n1111111,Other,263494\n")
    lost_zero = service.find_security_in_file(ctl(), A, "master.csv", "263494", "sedol")
    assert lost_zero["outcome"] == "confirmed_absent" and lost_zero["matches"] == []
    assert lost_zero["near_misses"]["identifier_inside_a_longer_value"] == {
        "count": 1, "examples": [{"record_number": 2, "column": "sedol", "value": "0263494"}]}
    assert lost_zero["near_misses"]["exact_value_in_other_columns"] == {"alt": 1}
    assert "see near_misses" in lost_zero["explanation"]

    partial = service.search_across_columns(ctl(), A, "master.csv", "BAE Systems")
    assert partial["outcome"] == "confirmed_absent"
    assert partial["near_misses"]["identifier_inside_a_longer_value"]["examples"][0]["value"] == "BAE Systems PLC"

    count = service.count_matching_rows(ctl(), A, "master.csv", "0263494", "name")
    assert count["count"] == 0 and count["near_misses"]["exact_value_in_other_columns"] == {"sedol": 1, "alt": 1}

    clean = service.find_security_in_file(ctl(), A, "master.csv", "9999999", "sedol")
    assert clean["outcome"] == "confirmed_absent" and "near_misses" not in clean
    compared = service.compare_security_records(ctl(), A, "master.csv", A, "master.csv", "263494", "sedol")
    assert compared["comparison_status"] == "absent_from_both"
    assert "inside a longer value" in compared["presence"]["file_a"]["near_misses"]
