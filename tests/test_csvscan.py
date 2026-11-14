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
    alphabet = ['a', 'B', '0', '1', ' ', ',', '"', '\n', '\r\n', ';', 'é', "'", '00123', 'KEY']
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

    control = ScanControl(max_seconds=0.0)
    assert list(iter_records(io.BytesIO(data), chunk_bytes=64, control=control)) == []
    assert control.stop_reason == "time_limit"


def test_other_delimiter_and_encoding():
    data = "id;name\nÄ1;\"Müller; Söhne\"\n".encode("latin-1")
    records, _ = scan(data, 5, delimiter=";", encoding="latin-1")
    assert [r.fields for r in records] == [["id", "name"], ["Ä1", "Müller; Söhne"]]
    found, _ = scan(data, 5, delimiter=";", encoding="latin-1", needle=prefilter_needle("Ä1", "latin-1"))
    assert [r.fields[0] for r in found] == ["Ä1"]


@pytest.mark.parametrize("seed", range(300))
def test_malformed_quoting_still_agrees_with_csv_module(seed):
    """Raw text with stray, unbalanced and misplaced quotes: skipping must never change the answer."""
    rng = random.Random(1000 + seed)
    alphabet = ['"', '"', ",", ",", "\n", "\n", "a", "b", "KEY", '""', '",', ',"', "\r\n", " "]
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
