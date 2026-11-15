"""Property tests judged by something other than this code base.

* Search results are compared with pandas, an independent CSV parser.
* Every response, whatever the file looks like, must fit the configured output limit.
"""

from __future__ import annotations

import csv
import io
import shutil
import tempfile
from pathlib import Path

import pytest

from conftest import wire_bytes

hypothesis = pytest.importorskip("hypothesis")
pd = pytest.importorskip("pandas")

from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from csv_o_matic.config import Config, Limits  # noqa: E402
from csv_o_matic.csvscan import ScanControl  # noqa: E402
from csv_o_matic.service import Service, ToolFailure  # noqa: E402

MAP = ("id: p\nname: p\ndescription: d\npurpose: p\nrun_semantics: r\n"
       "folders:\n  - {id: d, path: d, description: d}\n"
       "steps:\n  - {id: s, name: s, description: s, writes: [d]}\n")

IDENTIFIERS = ["00123", "123", "KEY", "é1", "a b", "x,y", 'q"q', "0"]
cell = st.one_of(
    st.sampled_from(IDENTIFIERS),
    st.sampled_from(IDENTIFIERS).map(lambda v: f" {v} "),
    st.sampled_from(IDENTIFIERS).map(lambda v: f"pre{v}post"),
    st.text(alphabet=st.sampled_from(list('ab01 ,;"\n\r\'é\u00a0')), max_size=8),
    st.just(""),
)


@st.composite
def tables(draw):
    width = draw(st.integers(1, 6))
    header = [f"c{i}" for i in range(width)]
    rows = draw(st.lists(st.lists(cell, min_size=width, max_size=width), max_size=40))
    # A row whose every cell is empty is a blank line to pandas; keep the comparison unambiguous.
    rows = [r for r in rows if any(r)] if width == 1 else rows
    quoting = draw(st.sampled_from([csv.QUOTE_MINIMAL, csv.QUOTE_ALL, csv.QUOTE_NONNUMERIC]))
    terminator = draw(st.sampled_from(["\n", "\r\n"]))
    chunk = draw(st.sampled_from([64, 257, 4096, 4 * 1024 * 1024]))
    return header, rows, quoting, terminator, chunk


class Workspace:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="csvomatic-prop-"))
        (self.root / "d").mkdir()
        (self.root / "m").mkdir()
        (self.root / "m" / "p.yaml").write_text(MAP)

    def service(self, **limits) -> Service:
        return Service(Config(self.root, self.root / "m", Limits(**limits)))

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


@pytest.fixture(scope="module")
def ws():
    workspace = Workspace()
    yield workspace
    workspace.close()


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(table=tables(), identifier=st.sampled_from(IDENTIFIERS))
def test_search_agrees_with_pandas(ws, table, identifier):
    header, rows, quoting, terminator, chunk = table
    out = io.StringIO(newline="")
    writer = csv.writer(out, quoting=quoting, lineterminator=terminator)
    writer.writerow(header)
    writer.writerows(rows)
    data = out.getvalue().encode("utf-8")
    (ws.root / "d" / "t.csv").write_bytes(data)

    frame = pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False, na_filter=False,
                        skip_blank_lines=False, engine="c")
    assert len(frame) == len(rows)  # the oracle itself read what was written
    svc = ws.service(chunk_bytes=chunk, max_record_bytes=max(chunk, 1 << 20), max_rows_per_response=7)

    for column in header:
        expected = [i for i, value in enumerate(frame[column]) if value == identifier]
        counted = svc.count_matching_rows(ScanControl(), "d", "t.csv", identifier, column)
        assert counted["count"] == len(expected) and counted["count_is_exact"]
        assert counted["outcome"] == ("match_found" if expected else "confirmed_absent")

        got, cursor = [], None
        while True:
            page = svc.find_security_in_file(ScanControl(), "d", "t.csv", identifier, column, cursor=cursor)
            got += page["matches"]
            if page["more_matches"] == "none":
                break
            assert page["results_are_partial"] and page["next_cursor"]
            cursor = page["next_cursor"]
        # Record numbers count physical records including the header, so they cannot be compared with
        # a row index when cells hold line breaks; compare the rows themselves, in order.
        assert [m["values"] for m in got] == [dict(zip(header, frame.iloc[i])) for i in expected]

    any_column = [i for i in range(len(frame)) if identifier in list(frame.iloc[i])]
    got, cursor = [], None
    while True:
        page = svc.search_across_columns(ScanControl(), "d", "t.csv", identifier, cursor=cursor)
        got += page["matches"]
        if page["more_matches"] == "none":
            break
        cursor = page["next_cursor"]
    assert [m["values"] for m in got] == [dict(zip(header, frame.iloc[i])) for i in any_column]
    for match, i in zip(got, any_column):
        assert match["matched_columns"] == [c for c in header if frame.iloc[i][c] == identifier]


name = st.one_of(
    st.sampled_from(["ID", "value", "value", "px", ""]),
    st.text(alphabet=st.sampled_from(list("abcXYZ_ 0123é\"")), min_size=1, max_size=30),
    st.integers(100, 3000).map(lambda n: "n" * n),
)
value = st.one_of(st.sampled_from(["A1", "", "x"]), st.integers(1, 6000).map(lambda n: "v" * n),
                  st.text(alphabet=st.sampled_from(list("ab é,\"\n")), max_size=40))


@st.composite
def awkward(draw):
    width = draw(st.integers(1, 400))
    names = ["ID"] + draw(st.lists(name, min_size=width, max_size=width))
    rows = draw(st.lists(st.lists(value, min_size=width, max_size=width), min_size=1, max_size=6))
    limit = draw(st.sampled_from([16000, 18000, 30000, 120000]))
    return names, [["A1"] + r for r in rows], limit


@settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow, HealthCheck.data_too_large])
@given(case=awkward(), data=st.data())
def test_no_response_exceeds_the_output_limit(ws, case, data):
    names, rows, limit = case
    with open(ws.root / "d" / "w.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(names)
        writer.writerows(rows)
    svc = ws.service(max_output_bytes=limit, max_cell_chars=data.draw(st.sampled_from([50, 2000])))
    picks = data.draw(st.lists(st.integers(1, len(names)), max_size=30))
    columns = [f"#{i}" for i in picks] or None
    control = ScanControl
    calls = [
        lambda: svc.inspect_csv_headers("d", "w.csv", sample_rows=data.draw(st.integers(0, 5))),
        lambda: svc.search_column_names("d", "w.csv", data.draw(st.sampled_from(["n", "value", "a", "ID"]))),
        lambda: svc.find_security_in_file(control(), "d", "w.csv", "A1", "#1", columns=columns),
        lambda: svc.search_across_columns(control(), "d", "w.csv", data.draw(st.sampled_from(["A1", "x", "v"])), columns=columns),
        lambda: svc.count_matching_rows(control(), "d", "w.csv", "x", "#1"),
        lambda: svc.compare_security_records(control(), "d", "w.csv", "d", "w.csv", "A1", "#1"),
        lambda: svc.compare_security_records(control(), "d", "w.csv", "d", "w.csv", "A1", "#1",
                                             compare_columns=columns, match_keys=columns[:3] if columns else None),
        lambda: svc.list_csv_files("d"),
        lambda: svc.list_folders(),
    ]
    for index, call in enumerate(calls):
        try:
            payload = call()
        except ToolFailure as failure:  # e.g. an ambiguous column: a legitimate, small answer
            payload = {"error": {"type": failure.kind, "message": failure.message, **failure.details}}
        size = wire_bytes(payload)
        assert size <= limit, f"call {index}: {size} bytes exceeds the limit of {limit}"
