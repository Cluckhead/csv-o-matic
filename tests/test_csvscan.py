"""The streaming reader must agree with Python's csv module on every input, at every chunk size."""

from __future__ import annotations

import csv
import io
import random

import pytest

from csv_o_matic.csvscan import CsvParseError, ScanControl, iter_records, prefilter_needle

CHUNKS = [1, 2, 3, 7, 16, 64, 1024, 1 << 20]


def reference(data: bytes, delimiter: str = ",") -> list[list[str]]:
    text = data.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter))


def scan(data: bytes, chunk: int, needle: bytes | None = None, **kw):
    control = kw.pop("control", None) or ScanControl()
    records = list(iter_records(io.BytesIO(data), chunk_bytes=chunk, needle=needle, control=control,
                                max_record_bytes=kw.pop("max_record_bytes", 1 << 20), **kw))
    return records, control


CASES = {
    "plain": b"a,b,c\n1,2,3\n4,5,6\n",
    "crlf": b"a,b\r\n1,2\r\n3,4\r\n",
    "no_trailing_newline": b"a,b\n1,2",
    "quoted_comma": b'a,b\n"x, y",2\n',
    "embedded_newline": b'a,b\n"line1\nline2",2\n3,4\n',
    "embedded_crlf": b'a,b\r\n"line1\r\nline2",2\r\n3,4\r\n',
    "doubled_quotes": b'a,b\n"say ""hi""",2\n',
    "quote_all": b'"a","b"\n"1","2"\n"3","4"\n',
    "blank_lines": b"a,b\n\n1,2\n\n\n3,4\n",
    "empty_fields": b"a,b,c\n,,\n1,,3\n",
    "stray_quote_in_unquoted_field": b'a,b\n5" pipe,2\n3,4\n6" x,7" y\n',
    "text_after_closing_quote": b'a,b\n"abc"def,2\n3,4\n',
    "quote_opens_mid_line_after_stray": b'a,b,c\nabc,5"x,"hello\nworld",z\n1,2,3\n',
    "bom": b"\xef\xbb\xbfa,b\n1,2\n",
    "unicode": "a,b\nÄö,€\n\"日本, 語\",x\n".encode(),
    "multi_line_at_end_no_newline": b'a,b\n1,"x\ny"',
    "only_header": b"a,b,c\n",
    "empty": b"",
    "many_newlines_in_quotes": b'a\n"\n\n\n\n"\nb\n',
    "cr_only": b"a,b\r1,2\r3,4\r",
    "cr_only_no_final": b"a,b\r1,2\r3,4",
    "stray_cr_in_lf_file": b"a,b\n1,2\r3,4\n5,6\n",
    "cr_inside_quotes": b'a,b\n"x\ry",2\n3,4\n',
    "cr_then_blank": b"a\r\r\nb\n\rc",
    "ends_with_cr": b"a,b\n1,2\r",
}


@pytest.mark.parametrize("name", CASES)
@pytest.mark.parametrize("chunk", CHUNKS)
def test_matches_csv_module(name, chunk):
    data = CASES[name]
    records, control = scan(data, chunk)
    assert [r.fields for r in records] == reference(data)
    assert [r.number for r in records] == list(range(1, len(records) + 1))
    assert control.stop_reason == "end_of_file"
    # Offsets are record boundaries: rescanning from any of them yields the remaining records.
    for index, record in enumerate(records):
        rest, _ = scan(data, chunk, start_offset=record.offset, start_record=record.number - 1)
        assert [(r.number, r.fields) for r in rest] == [(r.number, r.fields) for r in records[index:]]
        assert record.end == (records[index + 1].offset if index + 1 < len(records) else len(data))


def nasty_cell(rng: random.Random) -> str:
    alphabet = ['a', 'B', '0', '1', ' ', ',', '"', '\n', '\r\n', '\r', ';', 'é', "'", '00123', 'KEY']
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 6)))


@pytest.mark.parametrize("seed", range(40))
def test_random_files_agree_with_csv_module(seed):
    rng = random.Random(seed)
    out = io.StringIO(newline="")
    writer = csv.writer(out, quoting=rng.choice([csv.QUOTE_MINIMAL, csv.QUOTE_ALL, csv.QUOTE_NONNUMERIC]),
                        lineterminator=rng.choice(["\n", "\r\n"]))
    width = rng.randint(1, 6)
    rows = [[nasty_cell(rng) or "h" for _ in range(width)]]
    for _ in range(rng.randint(0, 60)):
        rows.append([rng.choice(["KEY", "00123", nasty_cell(rng)]) for _ in range(width)])
    writer.writerows(rows)
    data = out.getvalue().encode()
    expected = reference(data)
    for chunk in (1, 5, 33, 4096):
        records, _ = scan(data, chunk)
        assert [r.fields for r in records] == expected
        # Needle mode may skip records, but never one that holds the needle as a whole field,
        # and record numbers stay identical to the full parse.
        for value in ("KEY", "00123"):
            found, _ = scan(data, chunk, needle=prefilter_needle(value, "utf-8"))
            got = {r.number: r.fields for r in found if value in r.fields}
            want = {i + 1: row for i, row in enumerate(expected) if value in row}
            assert got == want


def test_needle_with_quotes_and_newlines_is_still_found():
    value = 'he said "hi"\nbye'
    out = io.StringIO(newline="")
    csv.writer(out).writerows([["id", "x"], ["1", "a"], [value, "b"], ["3", "c"]])
    data = out.getvalue().encode()
    found, _ = scan(data, 8, needle=prefilter_needle(value, "utf-8"))
    assert [r.number for r in found if r.fields[0] == value] == [3]
    assert prefilter_needle('"', "utf-8") is None  # nothing safe to prefilter on: parse everything


def test_unterminated_quote_at_eof_is_flagged():
    records, control = scan(b'a,b\n1,"never closed\n2,3\n', 16)
    assert control.unterminated_quote_at_eof
    assert records[-1].fields == ["1", "never closed\n2,3\n"]


def test_runaway_record_is_a_parse_error_not_unbounded_memory():
    data = b'a,b\n1,"never closed\n' + b"x,y\n" * 5000
    with pytest.raises(CsvParseError) as info:
        scan(data, 256, max_record_bytes=4096)
    assert info.value.record_number == 2
    with pytest.raises(CsvParseError):
        scan(b"a" * 10000, 256, max_record_bytes=4096)  # one endless line


def test_cancel_and_time_limit_report_a_resumable_position():
    data = b"id,v\n" + b"".join(b"%d,x\n" % i for i in range(5000))
    full, _ = scan(data, 64)

    control = ScanControl()
    seen = []
    for record in iter_records(io.BytesIO(data), chunk_bytes=64, control=control):
        seen.append(record)
        if len(seen) == 100:
            control.stop.set()
    assert control.stop_reason == "cancelled"
    assert len(seen) < len(full)
    rest, _ = scan(data, 64, start_offset=control.resume_offset, start_record=control.resume_record)
    assert [(r.number, r.fields) for r in seen + rest] == [(r.number, r.fields) for r in full]

    # An expired time limit still lets the first read through, so every call makes progress ...
    control = ScanControl(max_seconds=0.0)
    first = list(iter_records(io.BytesIO(data), chunk_bytes=64, control=control))
    assert control.stop_reason == "time_limit" and 0 < len(first) < 20 and control.resume_offset > 0
    rest, _ = scan(data, 64, start_offset=control.resume_offset, start_record=control.resume_record)
    assert [(r.number, r.fields) for r in first + rest] == [(r.number, r.fields) for r in full]
    # ... and a chain of such calls always reaches the end of the file.
    offset, record, hops = 0, 0, 0
    while True:
        control = ScanControl(max_seconds=0.0)
        list(iter_records(io.BytesIO(data), chunk_bytes=64, control=control, start_offset=offset, start_record=record))
        hops += 1
        if control.stop_reason == "end_of_file":
            break
        assert control.resume_offset > offset
        offset, record = control.resume_offset, control.resume_record
    assert control.resume_record == len(full) and hops > 100
    # Cancellation, unlike the time limit, stops before anything is read.
    control = ScanControl()
    control.stop.set()
    assert list(iter_records(io.BytesIO(data), chunk_bytes=64, control=control)) == []
    assert control.stop_reason == "cancelled" and control.resume_offset == 0


def test_other_delimiter_and_encoding():
    data = "id;name\nÄ1;\"Müller; Söhne\"\n".encode("latin-1")
    records, _ = scan(data, 5, delimiter=";", encoding="latin-1")
    assert [r.fields for r in records] == [["id", "name"], ["Ä1", "Müller; Söhne"]]
    found, _ = scan(data, 5, delimiter=";", encoding="latin-1", needle=prefilter_needle("Ä1", "latin-1"))
    assert [r.fields[0] for r in found] == ["Ä1"]


@pytest.mark.parametrize("seed", range(600))
def test_malformed_quoting_still_agrees_with_csv_module(seed):
    """Raw text with stray, unbalanced and misplaced quotes: skipping must never change the answer."""
    rng = random.Random(1000 + seed)
    alphabet = ['"', '"', ",", ",", "\n", "\n", "a", "b", "KEY", '""', '",', ',"', "\r\n", "\r", " "]
    data = ("h1,h2\n" + "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 120)))).encode()
    if rng.random() < 0.5:
        data += b"\n"
    expected = reference(data)
    for chunk in (1, 4, 17, 4096):
        records, _ = scan(data, chunk)
        assert [r.fields for r in records] == expected
        found, _ = scan(data, chunk, needle=b"KEY")
        got = {r.number: r.fields for r in found if "KEY" in r.fields}
        assert got == {i + 1: row for i, row in enumerate(expected) if "KEY" in row}


@pytest.mark.parametrize("name", CASES)
@pytest.mark.parametrize("chunk", [1, 3, 16, 4096])
def test_skipping_counts_every_record(name, chunk):
    """With a needle most records are skipped unparsed; the final record count must still be exact."""
    data = CASES[name]
    total = len(reference(data))
    for needle in (b"zzz-not-there", b"1", b"x"):
        _, control = scan(data, chunk, needle=needle)
        assert control.stop_reason == "end_of_file" and control.resume_record == total
        assert control.resume_offset == len(data) and control.bytes_done == len(data)


def test_parse_errors_say_where():
    data = b"a,b\n1,2\n3,4\n" + b'5,"never closed\n' + b"x,y\n" * 2000
    with pytest.raises(CsvParseError) as info:
        scan(data, 256, max_record_bytes=1024)
    assert (info.value.record_number, info.value.byte_offset) == (4, 12)
    assert "Record 4 starting at byte 12" in str(info.value)
    with pytest.raises(CsvParseError) as info:  # the same, found while skipping
        scan(data, 256, max_record_bytes=1024, needle=b"zzz")
    assert (info.value.record_number, info.value.byte_offset) == (4, 12)


def test_long_cr_only_file_is_read_in_chunks():
    """No LF anywhere: the reader must still cut at CRs, not buffer the whole file as one record."""
    rows = [[str(i), "x" * 20] for i in range(2000)]
    data = b"id,v\r" + b"".join(b"%s,%s\r" % (a.encode(), b.encode()) for a, b in rows)
    for needle in (None, b"1999"):
        records, control = scan(data, 64, needle=needle, max_record_bytes=256)
        assert control.resume_record == 2001
        assert [r.fields for r in records if r.fields[0] == "1999"] == [["1999", "x" * 20]]


def test_long_record_is_not_reparsed_once_per_chunk():
    """A 2 MB quoted field read in 1 KB chunks must cost a handful of parses, not thousands."""
    data = b'a,b\n1,"' + b"line\n" * 400_000 + b'"\n2,3\n'
    import time
    started = time.monotonic()
    records, _ = scan(data, 1024, max_record_bytes=4 * 1024 * 1024)
    assert [r.fields[0] for r in records] == ["a", "1", "2"] and len(records[1].fields[1]) == 2_000_000
    assert time.monotonic() - started < 5


def test_glued_quoted_and_unquoted_text_is_not_skipped():
    """csv reads "001"23 as 00123; the raw line does not contain 00123, so it must not be skipped."""
    data = b'ID,Name\n"001"23,Acme\nx, "00123" ,y\n9,"a"b"00123"\n'
    expected = reference(data)
    assert expected[1][0] == "00123"
    found, _ = scan(data, 4096, needle=b"00123")
    assert [r.fields for r in found if "00123" in r.fields] == [expected[1]]
    assert [r.number for r in found] == [2, 3, 4]  # the other two lines hold the text and are examined


def test_interruption_inside_a_chunk_of_candidates():
    """Thousands of candidate records in one chunk: a normal scan returns them all; an interrupted one
    stops within about a thousand records and says exactly where to resume."""
    data = b"id,v\n" + b"".join(b"%d,KEY\n" % i for i in range(5000))
    full, control = scan(data, 1 << 20, needle=b"KEY")
    assert len(full) == 5000 and control.stop_reason == "end_of_file" and control.resume_record == 5001

    control = ScanControl()
    seen = []
    for record in iter_records(io.BytesIO(data), chunk_bytes=1 << 20, needle=b"KEY", control=control):
        seen.append(record)
        if len(seen) == 1500:
            control.stop.set()
    assert control.stop_reason == "cancelled" and 1500 <= len(seen) < 1500 + 1100
    assert control.resume_offset == seen[-1].end and control.resume_record == seen[-1].number
    assert control.bytes_done == control.resume_offset
    rest, _ = scan(data, 1 << 20, needle=b"KEY", start_offset=control.resume_offset, start_record=control.resume_record)
    assert [(r.number, r.fields) for r in seen + rest] == [(r.number, r.fields) for r in full]


def test_field_larger_than_the_limit_is_reported_as_an_overlong_record():
    limit = 4 * 1024 * 1024
    data = b"a,b\n1,2\n" + b'3,"' + b"x" * 1000 + b"\n" + (b"y" * 99 + b"\n") * 90_000
    with pytest.raises(CsvParseError) as info:
        scan(data, 1 << 20, max_record_bytes=limit)
    assert "longer than max_record_bytes" in str(info.value) and "Record 3 starting at byte 8" in str(info.value)
    assert (info.value.record_number, info.value.byte_offset) == (3, 8)
    # Read in one piece, the record is complete before its length is known; the report must be the same.
    with pytest.raises(CsvParseError) as info:
        scan(data, 16 * 1024 * 1024, max_record_bytes=limit)
    assert "longer than max_record_bytes" in str(info.value) and (info.value.record_number, info.value.byte_offset) == (3, 8)


def test_other_encoding_hits_only_count_when_the_chunk_reads_as_that_encoding():
    def hits(data: bytes, declared: str, alternatives: dict[str, bytes]) -> set[str]:
        control = ScanControl(alt_needles=alternatives)
        list(iter_records(io.BytesIO(data), encoding=declared, needle=b"zzz", control=control))
        return control.alt_hits

    nbsp_cp1252, nbsp_utf8 = " ".encode("cp1252"), " ".encode()
    # Declared utf-8: the cp1252 byte inside a valid UTF-8 file is a coincidence ...
    assert hits("a,b\n ,x\n".encode(), "utf-8", {"cp1252": nbsp_cp1252}) == set()
    # ... but in a file that is not valid UTF-8 it is evidence.
    assert hits("a,b\n ,é\n".encode("cp1252"), "utf-8", {"cp1252": nbsp_cp1252}) == {"cp1252"}
    # Declared latin-1: UTF-8 bytes count only if the chunk really is UTF-8.
    assert hits("a,b\n ,x\n".encode(), "latin-1", {"utf-8": nbsp_utf8}) == {"utf-8"}
    assert hits(b"a,b\n\xc2\xa0,\xe9\n", "latin-1", {"utf-8": nbsp_utf8}) == set()
    # Two single-byte encodings cannot be told apart by validity: a hit always counts.
    assert hits(b"a,b\n\x801,x\n", "latin-1", {"cp1252": b"\x801"}) == {"cp1252"}
    assert hits(b"a,b\n\xc3\xa9,\x801\n", "latin-1", {"cp1252": b"\x801"}) == {"cp1252"}
    assert hits(b"a,b\n\xc2\x801,x\n", "latin-1", {"cp1252": b"\x801"}) == {"cp1252"}  # even if it reads as UTF-8


def test_record_limit_applies_to_records_that_arrive_whole():
    """A closed quoted field longer than the limit, read in a single chunk, is still refused."""
    data = b'a,b\n1,"' + b"x" * 5000 + b'"\n2,3\n'
    with pytest.raises(CsvParseError) as info:
        scan(data, 1 << 20, max_record_bytes=4096)
    assert (info.value.record_number, info.value.byte_offset) == (2, 4)
    records, _ = scan(data, 1 << 20, max_record_bytes=8192)
    assert [len(r.fields) for r in records] == [2, 2, 2]
    # ... and one scan's limit does not leak into another's.
    with pytest.raises(CsvParseError):
        scan(data, 1 << 20, max_record_bytes=4096)


class CountingFile(io.BytesIO):
    """Counts the bytes handed out, to show a scan stopped reading instead of buffering the file."""

    consumed = 0

    def read(self, size=-1):
        data = super().read(size)
        self.consumed += len(data)
        return data


@pytest.mark.parametrize("payload", [b'1,"never closed\n' + b"x,y\n" * 500_000, b"z" * 2_000_000])
@pytest.mark.parametrize("needle", [None, b"zzz-not-there"])
def test_runaway_record_stops_reading_early(payload, needle):
    """Memory guard: an unclosed quote or an endless line is abandoned soon after max_record_bytes,
    not after the whole remainder of the file has been buffered."""
    fh = CountingFile(b"a,b\n" + payload)
    with pytest.raises(CsvParseError):
        list(iter_records(fh, chunk_bytes=1024, max_record_bytes=8192, needle=needle))
    assert fh.consumed < 40_000
