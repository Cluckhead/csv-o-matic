"""What a search may conclude: integrity across cursor chains, cursor-to-file binding, guaranteed
progress, and supervision of header reading and sampling."""

from __future__ import annotations

import io
import os
import threading

import pytest

from csv_o_matic.csvscan import CsvParseError, ScanControl, iter_records
from csv_o_matic.service import ToolFailure

from conftest import digest, make_service

A = "stage/a"


def instant() -> ScanControl:
    """A time budget that is already spent: the scan may only do the minimum that counts as progress."""
    return ScanControl(max_seconds=0)


def drain(call, first):
    """Follow next_cursor to the end; returns every response."""
    pages = [first]
    while pages[-1]["next_cursor"]:
        pages.append(call(pages[-1]["next_cursor"]))
        assert len(pages) < 10_000
    return pages


# ---------------------------------------------------------------- integrity carried through a cursor chain

@pytest.fixture
def mis_encoded(env):
    """Declared UTF-8, but the one non-ASCII value near the top is Windows-1252. Everything after is plain."""
    path = env[0] / A / "mis.csv"
    path.write_bytes("ID,Name\n".encode() + "Ä1,first\n".encode("cp1252") + b"".join(b"S%d,plain\n" % i for i in range(4000)))
    return path


def test_a_clean_final_page_does_not_erase_an_earlier_encoding_problem(env, mis_encoded):
    service = make_service(env, chunk_bytes=1024, max_record_bytes=1024)
    first = service.find_security_in_file(instant(), A, "mis.csv", "Ä1", "ID")
    assert first["outcome"] == "inconclusive" and first["scan"]["stop_reason"] == "time_limit" and first["next_cursor"]
    assert any("declared encoding is probably wrong" in w for w in first["warnings"])
    pages = drain(lambda c: service.find_security_in_file(ScanControl(), A, "mis.csv", "Ä1", "ID", cursor=c), first)
    last = pages[-1]
    assert len(pages) > 1 and last["scan"]["complete"] and last["scan"]["stop_reason"] == "end_of_file"
    # Reaching the end of the file is not the same as a reliable scan of it.
    assert last["outcome"] == "inconclusive" and last["more_matches"] == "unknown" and last["results_are_partial"]
    assert any("declared encoding is probably wrong" in w for w in last["warnings"])
    assert last["scan"]["integrity_problems_seen_in_earlier_responses"] is True
    assert "INCONCLUSIVE" in last["explanation"]


def test_a_count_is_never_certified_exact_after_an_earlier_integrity_problem(env, mis_encoded):
    service = make_service(env, chunk_bytes=1024, max_record_bytes=1024)
    first = service.count_matching_rows(instant(), A, "mis.csv", "Ä1", "ID")
    assert first["count"] is None and not first["count_is_exact"]
    for page in drain(lambda c: service.count_matching_rows(instant(), A, "mis.csv", "Ä1", "ID", cursor=c), first):
        assert page["count"] is None and page["count_is_exact"] is False and page["outcome"] != "confirmed_absent"
        assert any("declared encoding is probably wrong" in w for w in page["warnings"])
    assert page["scan"]["complete"] and "NOT available" in page["explanation"]
    # The same holds for a search across all columns.
    first = service.search_across_columns(instant(), A, "mis.csv", "Ä1")
    last = drain(lambda c: service.search_across_columns(ScanControl(), A, "mis.csv", "Ä1", cursor=c), first)[-1]
    assert last["outcome"] == "inconclusive"


def test_undecodable_bytes_seen_on_an_earlier_page_are_still_reported_at_the_end(env):
    path = env[0] / A / "bad.csv"
    path.write_bytes(b"ID,Name\nKEY,caf\xe9\n" + b"".join(b"S%d,plain\n" % i for i in range(4000)) + b"KEY,last\n")
    service = make_service(env, chunk_bytes=1024, max_record_bytes=1024)
    first = service.find_security_in_file(instant(), A, "bad.csv", "KEY", "ID", max_rows=1)
    assert any("not valid utf-8" in w for w in first["warnings"])
    last = drain(lambda c: service.find_security_in_file(ScanControl(), A, "bad.csv", "KEY", "ID", cursor=c), first)[-1]
    assert last["scan"]["complete"] and any("not valid utf-8" in w for w in last["warnings"])


def test_near_misses_from_earlier_pages_are_not_forgotten(env):
    path = env[0] / A / "near.csv"
    path.write_bytes(b"ID,Alt\n 00123 ,x\nzz,00123\n" + b"".join(b"S%d,plain\n" % i for i in range(4000)))
    service = make_service(env, chunk_bytes=1024, max_record_bytes=1024)
    first = service.find_security_in_file(instant(), A, "near.csv", "00123", "ID")
    assert first["near_misses"]["same_value_with_surrounding_whitespace"]["count"] == 1
    last = drain(lambda c: service.find_security_in_file(instant(), A, "near.csv", "00123", "ID", cursor=c), first)[-1]
    # No integrity problem, so absence IS confirmed - but the explanation must still mention what was seen.
    assert last["outcome"] == "confirmed_absent" and "No near misses" not in last["explanation"]
    assert last["near_misses"]["same_value_with_surrounding_whitespace"]["count"] == 1
    assert last["near_misses"]["exact_value_in_other_columns"] == {"Alt": 1}
    counted = drain(lambda c: service.count_matching_rows(instant(), A, "near.csv", "00123", "ID", cursor=c),
                    service.count_matching_rows(instant(), A, "near.csv", "00123", "ID"))[-1]
    assert counted["count"] == 0 and counted["count_is_exact"] and "worth checking" in counted["explanation"]


@pytest.mark.parametrize("budget", [None, 0])
@pytest.mark.parametrize("max_rows", [1, 7, 25])
def test_a_reliable_search_pages_without_losing_or_repeating_matches(env, budget, max_rows):
    service = make_service(env, chunk_bytes=1024, max_record_bytes=1024)
    before = digest(env[0])
    control = lambda: ScanControl(max_seconds=budget)  # noqa: E731
    first = service.find_security_in_file(control(), A, "many.csv", "DUP", "ID", max_rows=max_rows)
    pages = drain(lambda c: service.find_security_in_file(control(), A, "many.csv", "DUP", "ID", max_rows=max_rows, cursor=c), first)
    numbers = [m["record_number"] for page in pages for m in page["matches"]]
    assert numbers == [2 + 3 * i for i in range(100)]  # all 100, in order, none twice
    last = pages[-1]
    assert last["outcome"] == "match_found" and last["more_matches"] == "none" and not last["results_are_partial"]
    assert "100 in total" in last["explanation"] and "warnings" not in last
    counted = drain(lambda c: service.count_matching_rows(control(), A, "many.csv", "DUP", "ID", cursor=c),
                    service.count_matching_rows(control(), A, "many.csv", "DUP", "ID"))[-1]
    assert counted["count"] == 100 and counted["count_is_exact"]
    assert digest(env[0]) == before


# ---------------------------------------------------------------- a cursor belongs to one physical file

def first_page(service):
    page = service.find_security_in_file(ScanControl(), A, "many.csv", "DUP", "ID", max_rows=5)
    assert page["next_cursor"] and page["more_matches"] == "yes"
    return page["next_cursor"]


def rejected(service, cursor) -> ToolFailure:
    with pytest.raises(ToolFailure) as info:
        service.find_security_in_file(ScanControl(), A, "many.csv", "DUP", "ID", max_rows=5, cursor=cursor)
    assert info.value.kind == "invalid_cursor" and "Start the search again" in info.value.message
    return info.value


def test_a_cursor_is_rejected_after_the_file_is_replaced_by_one_of_the_same_size_and_time(env, service):
    path = env[0] / A / "many.csv"
    cursor = first_page(service)
    st = path.stat()
    replacement = path.with_name("incoming.tmp")
    replacement.write_bytes(path.read_bytes().replace(b"DUP", b"NEW"))  # same size, different records
    os.replace(replacement, path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    now = path.stat()
    assert (now.st_size, now.st_mtime_ns) == (st.st_size, st.st_mtime_ns) and now.st_ino != st.st_ino
    rejected(service, cursor)
    # A fresh search is about the new file, and only the new file.
    assert service.count_matching_rows(ScanControl(), A, "many.csv", "DUP", "ID")["count"] == 0


def test_a_cursor_is_rejected_after_the_file_is_rewritten_in_place_with_the_same_size_and_time(env, service):
    path = env[0] / A / "many.csv"
    cursor = first_page(service)
    st = path.stat()
    with open(path, "r+b") as fh:
        data = fh.read()
        fh.seek(0)
        fh.write(data.replace(b"DUP", b"NEW"))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    now = path.stat()
    assert (now.st_size, now.st_mtime_ns, now.st_ino) == (st.st_size, st.st_mtime_ns, st.st_ino)
    rejected(service, cursor)


def test_a_cursor_survives_when_the_file_is_untouched_and_is_bound_to_its_query(env, service):
    cursor = first_page(service)
    resumed = service.find_security_in_file(ScanControl(), A, "many.csv", "DUP", "ID", max_rows=5, cursor=cursor)
    assert resumed["matches"][0]["record_number"] == 17
    for other in (lambda: service.find_security_in_file(ScanControl(), A, "many.csv", "S1", "ID", cursor=cursor),
                  lambda: service.find_security_in_file(ScanControl(), A, "many.csv", "DUP", "Seq", cursor=cursor),
                  lambda: service.count_matching_rows(ScanControl(), A, "many.csv", "DUP", "ID", cursor=cursor),
                  lambda: service.find_security_in_file(ScanControl(), A, "wide.csv", "DUP", "ID", cursor=cursor)):
        with pytest.raises(ToolFailure) as info:
            other()
        assert info.value.kind == "invalid_cursor"


def test_a_file_replaced_during_the_scan_is_never_reported_complete(env, service, monkeypatch):
    path = env[0] / A / "many.csv"
    original = service._iter_matches

    def replacing(*args, **kwargs):
        yield from original(*args, **kwargs)
        other = path.with_name("incoming.tmp")
        other.write_bytes(path.read_bytes())
        os.replace(other, path)  # same content, size and (almost) time - but no longer the file that was read

    monkeypatch.setattr(service, "_iter_matches", replacing)
    out = service.count_matching_rows(ScanControl(), A, "many.csv", "nope", "ID")
    assert out["count"] is None and out["outcome"] == "inconclusive" and out["next_cursor"] is None
    assert any("modified or replaced" in w for w in out["warnings"])


# ---------------------------------------------------------------- every continuation advances

LONG = b'ID,Text\nA1,"' + b"x" * 5000 + b'"\nKEY,"' + b"y\n" * 3000 + b'"\nKEY,short\nB2,tail\n'


def test_the_scanner_always_gets_past_a_record_longer_than_a_chunk():
    offset, record, seen, calls = 8, 1, [], 0
    while True:
        control = instant()
        seen += [r.fields[0] for r in iter_records(io.BytesIO(LONG), needle=b"KEY", start_offset=offset, start_record=record,
                                                   control=control, chunk_bytes=256, max_record_bytes=1 << 20)]
        calls += 1
        if control.stop_reason == "end_of_file":
            break
        assert control.stop_reason == "time_limit" and control.resume_offset > offset, "a continuation must advance"
        offset, record = control.resume_offset, control.resume_record
    assert seen == ["KEY", "KEY"] and calls > 1 and control.resume_record == 5


def test_a_tight_deadline_and_long_records_still_terminate_with_the_right_answer(env):
    (env[0] / A / "long.csv").write_bytes(LONG)
    service = make_service(env, chunk_bytes=256, max_record_bytes=1 << 20)
    first = service.find_security_in_file(instant(), A, "long.csv", "KEY", "ID", columns=["ID"])
    pages = drain(lambda c: service.find_security_in_file(instant(), A, "long.csv", "KEY", "ID", columns=["ID"], cursor=c), first)
    stops = [p["scan"]["stopped_at_byte"] for p in pages]
    assert stops == sorted(set(stops)), "no response may hand back a cursor that does not advance"
    assert [m["record_number"] for p in pages for m in p["matches"]] == [3, 4]
    assert pages[-1]["more_matches"] == "none" and len(pages) >= 2
    counted = drain(lambda c: service.count_matching_rows(instant(), A, "long.csv", "KEY", "ID", cursor=c),
                    service.count_matching_rows(instant(), A, "long.csv", "KEY", "ID"))[-1]
    assert counted["count"] == 2


def test_guaranteed_progress_does_not_lift_the_record_size_protection_or_cancellation(env):
    runaway = b'ID,Text\nA1,"' + b"never closed\n" * 5000
    with pytest.raises(CsvParseError):  # still bounded: the over-long record is refused, deadline or not
        list(iter_records(io.BytesIO(runaway), needle=b"KEY", start_offset=8, start_record=1, control=instant(),
                          chunk_bytes=256, max_record_bytes=4096))
    cancelled = ScanControl()
    cancelled.stop.set()
    assert list(iter_records(io.BytesIO(LONG), start_offset=8, start_record=1, control=cancelled, chunk_bytes=256)) == []
    assert cancelled.stop_reason == "cancelled" and cancelled.resume_offset == 8  # cancellation is immediate
    (env[0] / A / "runaway.csv").write_bytes(runaway)
    out = make_service(env, chunk_bytes=256, max_record_bytes=4096).count_matching_rows(instant(), A, "runaway.csv", "KEY", "ID")
    assert out["count"] is None and out["error"]["type"] == "parse_error" and out["next_cursor"] is None


# ---------------------------------------------------------------- header reading and sampling are supervised

@pytest.fixture
def blank_tail(env):
    """A header, one data row, then a very long run of blank lines."""
    path = env[0] / A / "blank.csv"
    path.write_bytes(b"ID,Name\n1,only\n" + b"\n" * 3_000_000)
    return path


def test_sampling_stops_at_the_time_limit_and_says_the_sample_is_partial(env, service, blank_tail):
    control = ScanControl(max_seconds=0.05)
    out = service.inspect_csv_headers(A, "blank.csv", sample_rows=2, control=control)
    assert control.stop_reason == "time_limit" and control.bytes_done < blank_tail.stat().st_size  # not read to the end
    assert out["columns"] == ["ID", "Name"] and [r["values"]["ID"] for r in out["sample_rows"]] == ["1"]
    assert out["sample_complete"] is False and out["sample_stop_reason"] == "time_limit" and out["sample_rows_requested"] == 2
    assert out["sample_note"].startswith("PARTIAL SAMPLE: 1 of the 2 requested") and "not evidence" in out["sample_note"]


def test_direct_callers_without_a_control_get_the_server_limit_not_an_unlimited_read(env, blank_tail):
    out = make_service(env, max_scan_seconds=0.05).inspect_csv_headers(A, "blank.csv", sample_rows=2)
    assert out["sample_complete"] is False and out["sample_stop_reason"] == "time_limit"


def test_sampling_stops_reading_as_soon_as_the_requested_rows_are_in(env, service, blank_tail):
    control = ScanControl(max_seconds=30)
    out = service.inspect_csv_headers(A, "blank.csv", sample_rows=1, control=control)
    assert out["sample_complete"] and out["sample_stop_reason"] == "requested_rows_collected"
    assert control.bytes_done < 1_000_000  # the blank tail was not read to find out there is nothing more to add


def test_cancellation_reaches_sampling(env, service, blank_tail):
    control = ScanControl(max_seconds=60)
    timer = threading.Timer(0.05, control.stop.set)
    timer.start()
    try:
        out = service.inspect_csv_headers(A, "blank.csv", sample_rows=2, control=control)
    finally:
        timer.cancel()
    assert out["sample_stop_reason"] == "cancelled" and out["sample_complete"] is False
    assert control.bytes_done < blank_tail.stat().st_size
    already = ScanControl()
    already.stop.set()
    for call in (lambda: service.inspect_csv_headers(A, "blank.csv", control=already),
                 lambda: service.search_column_names(A, "blank.csv", "id", control=already)):
        with pytest.raises(ToolFailure) as info:
            call()
        assert info.value.kind == "cancelled"


def test_a_short_file_gives_a_complete_sample_with_fewer_rows(env, service):
    out = service.inspect_csv_headers(A, "misc_notes.csv", sample_rows=5)
    assert out["sample_complete"] and out["sample_stop_reason"] == "end_of_file" and len(out["sample_rows"]) == 1
    assert "only 1 data record" in out["sample_note"]
    plain = service.inspect_csv_headers(A, "misc_notes.csv")
    assert "sample_rows" not in plain and "sample_complete" not in plain  # headers only stays headers only


def test_a_huge_header_line_obeys_the_time_limit_in_both_header_tools(env):
    (env[0] / A / "runaway.csv").write_bytes(b'ID,"' + b"x" * 3_000_000)
    service = make_service(env)
    for call in (lambda: service.inspect_csv_headers(A, "runaway.csv", control=instant()),
                 lambda: service.search_column_names(A, "runaway.csv", "id", control=instant())):
        with pytest.raises(ToolFailure) as info:
            call()
        assert info.value.kind in ("header_not_read", "parse_error")
