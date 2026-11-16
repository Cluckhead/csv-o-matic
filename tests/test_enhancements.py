"""Record-key filters, approved subfolders and structured output, on realistic positions files.

The fixture is an end-of-day positions process whose outputs are nested by date
(positions/enriched/2026/11/15/...), with the same ISIN held by many portfolios.
"""

from __future__ import annotations

import csv
import json
import os

import anyio
import pytest
from mcp.client import Client

from conftest import check_schema, digest, strict
from csv_o_matic.config import Config, Limits
from csv_o_matic.csvscan import ScanControl
from csv_o_matic.server import build_server
from csv_o_matic.service import Service, ToolFailure

MAP = r"""
id: positions
name: Positions
description: Enriches custodian positions and produces a client report.
purpose: Test fixture.
run_semantics: Outputs are written under year/month/day folders; a later run timestamp supersedes an earlier one.
folders:
  - id: enriched
    path: positions/enriched
    description: Enriched positions, one row per portfolio and security, under YYYY/MM/DD.
    filename_convention:
      pattern: "positions_enriched_{business_date}_{run_timestamp}.csv"
      regex: '^positions_enriched_(?P<business_date>\d{8})_(?P<run_timestamp>\d{8}T\d{6})\.csv$'
      tokens:
        business_date: {format: "%Y%m%d", meaning: "Position date"}
        run_timestamp: {format: "%Y%m%dT%H%M%S", meaning: "Run start, UTC"}
    identifier_columns: [{column: isin, identifier_type: ISIN}, {column: sedol, identifier_type: SEDOL}]
    record_key_columns: [portfolio_code, isin]
  - id: report
    path: positions/report
    description: Client report extract; semicolon-delimited.
    csv_format: {delimiter: ";"}
    identifier_columns: [{column: ISIN, identifier_type: ISIN}]
    record_key_columns: [Portfolio, ISIN]
  - id: pricing
    path: pricing
    description: Prices. Mapped separately from positions.
steps:
  - id: enrich
    name: Enrich
    description: Writes enriched positions.
    writes: [enriched]
  - id: report
    name: Report
    description: Reads enriched, writes the report.
    reads: [enriched]
    writes: [report]
"""

ISIN = "GB1234567890"
DAY = "positions/enriched/2026/11/15"
RUN1 = "positions_enriched_20261115_20261115T200000.csv"
RUN2 = "positions_enriched_20261115_20261115T213000.csv"
REPORT = "client_holdings.csv"
N_PORTFOLIOS = 100


def portfolio(i: int) -> str:
    return f"PF{i:03d}"


def write_enriched(path, *, rerun: bool) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["portfolio_code", "isin", "sedol", "name", "quantity", "price", "market_value", "book"])
        for i in range(N_PORTFOLIOS):
            # The same ISIN in every portfolio; a different security alongside it.
            qty = 100 + i
            price = "101.25" if rerun else "101.20"
            w.writerow([portfolio(i), ISIN, "0123456", "Acme Holdings plc", qty, price, f"{qty * float(price):.2f}", "MAIN"])
            w.writerow([portfolio(i), "US0378331005", "2046251", "Apple Inc", 10, "190.00", "1900.00", "MAIN"])
        # One portfolio holds the security twice (two books): the record key is not unique there.
        w.writerow(["PF007", ISIN, "0123456", "Acme Holdings plc", 5, "101.20", "506.00", "HEDGE"])
        # Leading zeroes and padding matter.
        w.writerow(["00042", ISIN, "0123456", "Acme Holdings plc", 1, "101.20", "101.20", "MAIN"])
        w.writerow(["42", "DE0001102580", "5494205", "Bund", 1, "99.00", "99.00", "MAIN"])
        w.writerow(["ABC ", ISIN, "0123456", "Acme Holdings plc", 1, "101.20", "101.20", "MAIN"])
        w.writerow(["abc", ISIN, "0123456", "Acme Holdings plc", 1, "101.20", "101.20", "MAIN"])


@pytest.fixture
def env(tmp_path):
    data, maps = tmp_path / "data", tmp_path / "maps"
    maps.mkdir()
    (maps / "positions.yaml").write_text(MAP)
    day = data / DAY
    day.mkdir(parents=True)
    write_enriched(day / RUN1, rerun=False)
    write_enriched(day / RUN2, rerun=True)
    for other in ("2026/11/14", "2026/11/13", "2026/10/30", "2025/12/31"):
        folder = data / "positions/enriched" / other
        folder.mkdir(parents=True)
        stamp = other.replace("/", "")
        (folder / f"positions_enriched_{stamp}_{stamp}T200000.csv").write_text(
            f"portfolio_code,isin,sedol,name,quantity,price,market_value,book\nPF001,{ISIN},0123456,Acme,1,1,1,MAIN\n")
    (data / "positions/enriched/latest.csv").write_text("portfolio_code,isin\nPF001,XS0000000001\n")
    report = data / "positions/report"
    report.mkdir(parents=True)
    with open(report / REPORT, "w", newline="") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["Portfolio", "ISIN", "Name", "MarketValue"])
        for i in range(N_PORTFOLIOS):
            if i != 3:  # PF003 is dropped by the report step
                w.writerow([portfolio(i), ISIN, "Acme Holdings plc", f"{(100 + i) * 101.25:.2f}"])
    (data / "pricing").mkdir()
    (data / "pricing/prices.csv").write_text(f"isin,close\n{ISIN},101.25\n")
    # Outside every mapped folder.
    (data / "restricted").mkdir()
    (data / "restricted/file.csv").write_text(f"isin,secret\n{ISIN},do-not-read\n")
    (data / "restricted/deep").mkdir()
    (data / "restricted/deep/file.csv").write_text(f"isin,secret\n{ISIN},do-not-read\n")
    return data, maps


def make(env, **limits) -> Service:
    return Service(Config(data_root=env[0], process_maps_dir=env[1], limits=Limits(**limits)))


@pytest.fixture
def service(env) -> Service:
    return make(env)


def ctl(seconds: float = 30.0) -> ScanControl:
    return ScanControl(max_seconds=seconds)


def failure(fn) -> ToolFailure:
    with pytest.raises(ToolFailure) as info:
        fn()
    return info.value


# ================================================================== 1. record-key filters

def test_the_brief_example_isin_in_one_portfolio(service):
    """'Does ISIN GB1234567890 exist in portfolio ABC?' - the identifier alone is in 100+ rows."""
    alone = service.count_matching_rows(ctl(), DAY, RUN2, ISIN, "isin")
    assert alone["count"] == N_PORTFOLIOS + 4 and alone["search"]["filters"] == []

    out = service.find_security_in_file(ctl(), DAY, RUN2, ISIN, "isin", filters={"portfolio_code": "PF042"})
    assert out["outcome"] == "match_found" and out["matches_returned"] == 1 and out["more_matches"] == "none"
    assert out["matches"][0]["values"]["portfolio_code"] == "PF042" and out["matches"][0]["values"]["quantity"] == "142"
    assert out["search"]["filters"] == [{"column": "portfolio_code", "value": "PF042"}]

    # 'ABC' is not a portfolio: the ISIN is present, so only the filter makes this absent.
    out = service.find_security_in_file(ctl(), DAY, RUN2, ISIN, "isin", filters={"portfolio_code": "ABC"})
    assert out["outcome"] == "confirmed_absent" and out["scan"]["complete"] and out["matches"] == []
    assert "'ABC' in column 'portfolio_code'" in out["explanation"] and "same row" in out["explanation"]
    missed = out["near_misses"]["identifier_matched_but_filters_did_not"]
    assert missed["count"] == N_PORTFOLIOS + 4
    assert missed["examples"][0]["values"] == {"portfolio_code": "PF000"}


def test_filters_are_exact_case_sensitive_whole_field_text(service):
    def count(value):
        return service.count_matching_rows(ctl(), DAY, RUN2, ISIN, "isin", filters={"portfolio_code": value})["count"]

    assert count("00042") == 1  # leading zeroes are part of the value
    assert count("42") == 0  # ... '42' exists, but for a different security: same-row AND
    assert count("PF04") == 0  # part of a value is not a match
    assert count("pf042") == 0 and count("abc") == 1 and count("ABC") == 0 and count("ABC ") == 1
    # The other way round: the filter value exists in the file, the identifier in that row does not.
    out = service.find_security_in_file(ctl(), DAY, RUN2, "DE0001102580", "isin", filters={"portfolio_code": "00042"})
    assert out["outcome"] == "confirmed_absent"
    assert service.count_matching_rows(ctl(), DAY, RUN2, "DE0001102580", "isin", filters={"portfolio_code": "42"})["count"] == 1


def test_several_filters_all_apply_to_the_same_row(service):
    both = service.find_security_in_file(ctl(), DAY, RUN2, ISIN, "isin", filters={"portfolio_code": "PF007"})
    assert both["matches_returned"] == 2  # two books
    one = service.find_security_in_file(ctl(), DAY, RUN2, ISIN, "isin",
                                        filters={"portfolio_code": "PF007", "book": "HEDGE"})
    assert one["matches_returned"] == 1 and one["matches"][0]["values"]["quantity"] == "5"
    assert one["search"]["filters"] == [{"column": "portfolio_code", "value": "PF007"}, {"column": "book", "value": "HEDGE"}]
    # Each value exists somewhere for this ISIN, but never together in one row.
    none = service.count_matching_rows(ctl(), DAY, RUN2, ISIN, "isin", filters={"portfolio_code": "PF008", "book": "HEDGE"})
    assert none["count"] == 0 and none["outcome"] == "confirmed_absent" and none["count_is_exact"]
    # Positional references work like everywhere else, and any column may be a filter.
    by_position = service.count_matching_rows(ctl(), DAY, RUN2, ISIN, "isin", filters={"#1": "PF007", "#8": "HEDGE"})
    assert by_position["count"] == 1 and by_position["search"]["filters"] == one["search"]["filters"]
    assert service.count_matching_rows(ctl(), DAY, RUN2, ISIN, "isin", filters={"quantity": "142"})["count"] == 1


def test_search_across_columns_with_filters(service):
    out = service.search_across_columns(ctl(), DAY, RUN2, "0123456", filters={"portfolio_code": "PF010"})
    assert out["matches_returned"] == 1 and out["matches"][0]["matched_columns"] == ["sedol"]
    assert out["matches"][0]["values"]["portfolio_code"] == "PF010"
    out = service.search_across_columns(ctl(), DAY, RUN2, "0123456", filters={"portfolio_code": "NOPE"})
    assert out["outcome"] == "confirmed_absent" and "any column" in out["explanation"]
    assert out["near_misses"]["identifier_matched_but_filters_did_not"]["count"] == N_PORTFOLIOS + 4


def test_bad_filters_are_explicit_errors(env, service):
    def find(filters, column="isin"):
        return failure(lambda: service.find_security_in_file(ctl(), DAY, RUN2, ISIN, column, filters=filters))

    missing = find({"portfolio": "PF001"})
    assert missing.kind == "column_not_found" and "portfolio_code" in missing.details["similar_column_names"]
    assert find({"portfolio_code": "PF001", "#1": "PF001"}).kind == "duplicate_filter_column"
    assert find({"isin": ISIN}).kind == "invalid_argument"  # the identifier column itself
    assert find({"portfolio_code": 7}).kind == "invalid_argument"  # values are text
    assert find({f"#{i}": "x" for i in range(1, 12)}).kind == "invalid_argument"
    assert find({"portfolio_code": "x" * 500}).kind == "invalid_argument"
    # A repeated column name is ambiguous by name and must be chosen by position.
    (env[0] / DAY / "dupes.csv").write_text("isin,pf,pf\nX1,A,B\n")
    dup = failure(lambda: service.count_matching_rows(ctl(), DAY, "dupes.csv", "X1", "isin", filters={"pf": "A"}))
    assert dup.kind == "ambiguous_column"
    assert service.count_matching_rows(ctl(), DAY, "dupes.csv", "X1", "isin", filters={"#3": "B"})["count"] == 1
    assert service.count_matching_rows(ctl(), DAY, "dupes.csv", "X1", "isin", filters={"#2": "B"})["count"] == 0


def test_a_short_record_never_satisfies_a_filter(env, service):
    (env[0] / DAY / "short.csv").write_text("isin,pf,book\nX1\nX1,A\nX1,A,MAIN\n")
    assert service.count_matching_rows(ctl(), DAY, "short.csv", "X1", "isin", filters={"book": "MAIN"})["count"] == 1
    assert service.count_matching_rows(ctl(), DAY, "short.csv", "X1", "isin", filters={"book": ""})["count"] == 0


def test_filtered_results_page_correctly_and_cursors_are_bound_to_the_filters(service):
    args = dict(folder=DAY, filename=RUN2, identifier="MAIN", identifier_column="book")
    filters = {"isin": ISIN}
    seen, cursor, pages = [], None, 0
    while True:
        page = service.find_security_in_file(ctl(), **args, filters=filters, columns=["portfolio_code"],
                                             max_rows=7, cursor=cursor)
        seen += [m["values"]["portfolio_code"] for m in page["matches"]]
        pages += 1
        if not page["next_cursor"]:
            break
        assert page["results_are_partial"] and page["more_matches"] == "yes" and page["outcome"] == "match_found"
        cursor = page["next_cursor"]
    assert pages > 10 and len(seen) == N_PORTFOLIOS + 3 and len(set(seen)) == len(seen)
    assert page["more_matches"] == "none" and page["search"]["matches_in_earlier_responses"] == len(seen) - len(page["matches"])

    first = service.find_security_in_file(ctl(), **args, filters=filters, max_rows=2)["next_cursor"]
    for other in (None, {}, {"isin": "US0378331005"}, {"isin": ISIN, "portfolio_code": "PF001"}, {"sedol": ISIN}):
        bad = failure(lambda: service.find_security_in_file(ctl(), **args, filters=other, cursor=first))
        assert bad.kind == "invalid_cursor"
    assert failure(lambda: service.count_matching_rows(ctl(), **args, filters=filters, cursor=first)).kind == "invalid_cursor"
    # The same filter given by position is the same search.
    assert service.find_security_in_file(ctl(), **args, filters={"#2": ISIN}, cursor=first)["matches"]


def big_file(env, rows=120_000) -> str:
    """Every row holds the identifier, so each one is parsed: slow enough to interrupt."""
    with open(env[0] / DAY / "big.csv", "w", newline="") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_ALL)
        w.writerow(["portfolio_code", "isin", "quantity"])
        for i in range(rows):
            w.writerow([portfolio(i % 400), ISIN, i])
    return "big.csv"


def test_an_interrupted_filtered_scan_is_inconclusive_and_resumes_to_the_exact_count(env):
    name = big_file(env)
    service = make(env, chunk_bytes=64 * 1024, max_record_bytes=64 * 1024)
    filters = {"portfolio_code": "PF399"}
    out = service.count_matching_rows(ctl(0.0), DAY, name, ISIN, "isin", filters=filters)
    assert out["count"] is None and out["count_is_exact"] is False and out["scan"]["stop_reason"] == "time_limit"
    assert out["outcome"] in ("inconclusive", "match_found") and out["next_cursor"]
    total, cursor, calls = None, out["next_cursor"], 1
    while cursor:
        out = service.count_matching_rows(ctl(0.0), DAY, name, ISIN, "isin", filters=filters, cursor=cursor)
        total, cursor, calls = out["count"], out["next_cursor"], calls + 1
    assert total == 300 and calls > 5 and out["count_is_exact"]

    # A filter nothing satisfies: absence is only confirmed once the whole file has been scanned.
    out = service.find_security_in_file(ctl(0.0), DAY, name, ISIN, "isin", filters={"portfolio_code": "ABC"})
    assert out["outcome"] == "inconclusive" and out["explanation"].startswith("INCONCLUSIVE")
    while out["next_cursor"]:
        assert out["outcome"] == "inconclusive"
        out = service.find_security_in_file(ctl(0.0), DAY, name, ISIN, "isin", filters={"portfolio_code": "ABC"},
                                            cursor=out["next_cursor"])
    assert out["outcome"] == "confirmed_absent"


def test_filtered_scan_streams_a_large_file_in_bounded_memory(env):
    import tracemalloc

    name = big_file(env, rows=200_000)
    service = make(env, chunk_bytes=64 * 1024, max_record_bytes=64 * 1024)
    tracemalloc.start()
    out = service.count_matching_rows(ctl(), DAY, name, ISIN, "isin", filters={"portfolio_code": "PF001"})
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert out["count"] == 500
    assert peak < 3_000_000 < os.path.getsize(env[0] / DAY / name)


# ---------------------------------------------------------------- comparisons

def test_compare_one_portfolio_record_between_processing_stages(service):
    base = dict(folder_a=DAY, filename_a=RUN2, folder_b="positions/report", filename_b=REPORT, identifier=ISIN,
                identifier_column_a="isin", identifier_column_b="ISIN",
                column_mapping={"portfolio_code": "Portfolio", "market_value": "MarketValue", "name": "Name"})
    # Without filters the ISIN is in 100+ rows on each side: too many to line up, and nothing is guessed.
    plain = service.compare_security_records(ctl(), **base)
    assert plain["comparison_status"] == "inconclusive" and "comparisons" not in plain
    assert plain["presence"]["file_a"]["stop_reason"] == "too_many_matches" and "filters" in plain["explanation"]
    assert plain["search"]["filters_a"] == [] and plain["search"]["filters_b"] == []
    # Two records for the portfolio upstream (two books), one downstream: ambiguous, not guessed.
    books = service.compare_security_records(ctl(), **base, filters={"portfolio_code": "PF007"})
    assert books["comparison_status"] == "ambiguous_alignment" and "comparisons" not in books

    out = service.compare_security_records(ctl(), **base, filters={"portfolio_code": "PF042"},
                                           compare_columns=["portfolio_code", "name", "market_value"])
    assert out["comparison_status"] == "compared"
    assert out["search"]["filters_a"] == [{"column": "portfolio_code", "value": "PF042"}]
    assert out["search"]["filters_b"] == [{"column": "Portfolio", "value": "PF042"}]  # carried through the mapping
    assert out["presence"]["file_a"]["matching_records_found"] == out["presence"]["file_b"]["matching_records_found"] == 1
    [pair] = out["comparisons"]
    assert pair["changed"] == [] and pair["unchanged_count"] == 3

    # The report step dropped PF003: present upstream, confirmed absent downstream - for that record.
    gone = service.compare_security_records(ctl(), **base, filters={"portfolio_code": "PF003"})
    assert gone["comparison_status"] == "present_in_one_file_only"
    assert gone["presence"]["file_b"]["outcome"] == "confirmed_absent"
    assert "do not satisfy the filters" in gone["presence"]["file_b"]["near_misses"]
    assert "identifier and all filters" in gone["explanation"]

    neither = service.compare_security_records(ctl(), **base, filters={"portfolio_code": "ABC"})
    assert neither["comparison_status"] == "absent_from_both"


def test_compare_two_runs_of_the_same_stage(service):
    base = dict(folder_a=DAY, filename_a=RUN1, folder_b=DAY, filename_b=RUN2, identifier=ISIN, identifier_column_a="isin")
    out = service.compare_security_records(ctl(), **base, filters={"portfolio_code": "PF042"})
    [pair] = out["comparisons"]
    assert out["comparison_status"] == "compared"
    assert {c["column_a"]: (c["value_a"], c["value_b"]) for c in pair["changed"]} == {
        "price": ("101.20", "101.25"), "market_value": ("14370.40", "14377.50")}

    # PF007 holds it in two books: the filter alone still leaves two records per file.
    two = service.compare_security_records(ctl(), **base, filters={"portfolio_code": "PF007"})
    assert two["comparison_status"] == "ambiguous_alignment" and len(two["records_in_file_a"]) == 2
    assert "book" in two["columns_that_differ_between_matches"]["file_a"]
    # Either a second filter or a match key resolves it.
    one = service.compare_security_records(ctl(), **base, filters={"portfolio_code": "PF007", "book": "HEDGE"})
    assert one["comparison_status"] == "compared" and len(one["comparisons"]) == 1
    keyed = service.compare_security_records(ctl(), **base, filters={"portfolio_code": "PF007"}, match_keys=["book"])
    assert keyed["comparison_status"] == "compared" and len(keyed["comparisons"]) == 2


def test_compare_with_different_filters_on_each_side(service):
    base = dict(folder_a=DAY, filename_a=RUN1, folder_b=DAY, filename_b=RUN2, identifier=ISIN, identifier_column_a="isin")
    out = service.compare_security_records(ctl(), **base, filters={"portfolio_code": "PF001"},
                                           filters_b={"portfolio_code": "PF002"}, compare_columns=["portfolio_code", "quantity"])
    assert out["comparison_status"] == "compared"
    assert out["search"]["filters_b"] == [{"column": "portfolio_code", "value": "PF002"}]
    assert [(c["value_a"], c["value_b"]) for c in out["comparisons"][0]["changed"]] == [("PF001", "PF002"), ("101", "102")]
    # filters_b={} means: no filter on file B at all.
    loose = service.compare_security_records(ctl(), **base, filters={"portfolio_code": "PF001"}, filters_b={})
    assert loose["search"]["filters_b"] == [] and loose["presence"]["file_b"]["matching_records_found"] > 25 - 1
    # A filter given by position in file A is carried to file B by column name, not by position.
    moved = service.compare_security_records(ctl(), DAY, RUN2, "positions/report", REPORT, ISIN, "isin", "ISIN",
                                             column_mapping={"portfolio_code": "Portfolio"}, filters={"#1": "PF042"},
                                             compare_columns=["portfolio_code"])
    assert moved["search"]["filters_b"] == [{"column": "Portfolio", "value": "PF042"}]
    assert moved["comparison_status"] == "compared"
    # A filter column that does not exist in file B is an error, not a silent mismatch.
    bad = failure(lambda: service.compare_security_records(
        ctl(), DAY, RUN2, "positions/report", REPORT, ISIN, "isin", "ISIN", filters={"portfolio_code": "PF001"}))
    assert bad.kind == "column_not_found"


def test_an_interrupted_filtered_comparison_is_inconclusive(env):
    name = big_file(env)
    service = make(env, chunk_bytes=64 * 1024, max_record_bytes=64 * 1024)
    out = service.compare_security_records(ctl(0.0), DAY, name, DAY, name, ISIN, "isin", filters={"portfolio_code": "ABC"})
    assert out["comparison_status"] == "inconclusive" and "comparisons" not in out
    assert out["presence"]["file_a"]["outcome"] == "inconclusive" and out["presence"]["file_b"]["stop_reason"] == "not_scanned"


def test_calls_without_filters_are_unchanged(service):
    out = service.find_security_in_file(ctl(), "positions/enriched/2026/11/14",
                                        "positions_enriched_20261114_20261114T200000.csv", "nothing", "isin")
    assert out["explanation"] == "Complete scan: no record has 'nothing' as the exact value of column 'isin'. No near misses were seen either."
    out = service.count_matching_rows(ctl(), DAY, RUN2, "US0378331005", "isin")
    assert out["explanation"] == "Complete scan: exactly 100 record(s) have 'US0378331005' in column 'isin'."


# ================================================================== 2. approved subfolders

def test_nested_folders_are_discovered_one_level_at_a_time(service):
    roots = service.list_folders()
    assert [(f["folder"], f["kind"]) for f in roots["folders"]] == [
        ("positions/enriched", "mapped_root"), ("positions/report", "mapped_root"), ("pricing", "mapped_root")]
    assert roots["folders"][0]["record_key_columns"] == ["portfolio_code", "isin"]

    years = service.list_folders(folder="positions/enriched")
    assert years["kind"] == "mapped_root" and "parent_folder" not in years and years["total_folders"] == 2
    assert [f["folder"] for f in years["folders"]] == ["positions/enriched/2026", "positions/enriched/2025"]
    assert all(f["kind"] == "subfolder" and f["mapped_root"] == "positions/enriched" for f in years["folders"])

    months = service.list_folders(folder="positions/enriched/2026")
    assert months["kind"] == "subfolder" and months["mapped_root"] == "positions/enriched"
    assert months["parent_folder"] == "positions/enriched"
    assert [f["name"] for f in months["folders"]] == ["11", "10"]  # only this level: no days, no files
    assert months["record_key_columns"] == ["portfolio_code", "isin"]  # inherited from the mapped root

    days = service.list_folders(folder="positions/enriched/2026/11")
    assert [f["folder"] for f in days["folders"]] == [f"positions/enriched/2026/11/{d}" for d in (15, 14, 13)]
    assert service.list_folders(folder=DAY)["folders"] == []
    assert failure(lambda: service.list_folders(process="positions", folder=DAY)).kind == "invalid_argument"


def test_files_in_a_subfolder_inherit_the_mapped_folders_metadata(service):
    top = service.list_csv_files("positions/enriched")
    assert [f["filename"] for f in top["files"]] == ["latest.csv"]  # files of subfolders are not pulled in
    assert top["subfolders"] == ["positions/enriched/2026", "positions/enriched/2025"] and top["subfolder_count"] == 2
    assert top["kind"] == "mapped_root" and "NOT included" in top["subfolders_note"]

    out = service.list_csv_files(DAY)
    assert out["folder"] == DAY and out["kind"] == "subfolder" and out["mapped_root"] == "positions/enriched"
    assert [f["filename"] for f in out["files"]] == [RUN2, RUN1] and out["subfolders"] == []
    assert out["files"][0]["filename_tokens"] == {"business_date": "2026-11-15", "run_timestamp": "2026-11-15T21:30:00"}
    assert out["filename_convention"]["pattern"].startswith("positions_enriched_")
    assert out["record_key_columns"] == ["portfolio_code", "isin"]
    # Existing filename and date filters apply in subfolders.
    assert service.list_csv_files(DAY, date_from="2026-11-16")["total_matching_files"] == 0
    assert service.list_csv_files(DAY, date_from="2026-11-15", date_to="2026-11-15")["total_matching_files"] == 2
    late = service.list_csv_files(DAY, date_from="2026-11-15T21:00:00", date_field="run_timestamp", filename_contains="T2130")
    assert [f["filename"] for f in late["files"]] == [RUN2]


def test_every_tool_works_on_a_file_in_a_subfolder(env, service):
    before = digest(env[0])
    head = service.inspect_csv_headers(DAY, RUN2, sample_rows=1)
    assert head["file"]["folder"] == DAY and head["file"]["mapped_root"] == "positions/enriched"
    assert head["columns"][:2] == ["portfolio_code", "isin"] and head["sample_rows"][0]["values"]["isin"] == ISIN
    assert service.search_column_names(DAY, RUN2, "code")["matching_columns"] == [{"column": "portfolio_code", "position": 1}]
    assert service.find_security_in_file(ctl(), DAY, RUN2, "DE0001102580", "isin")["matches_returned"] == 1
    assert service.search_across_columns(ctl(), DAY, RUN2, "2046251", max_rows=3)["matches_returned"] == 3
    assert service.count_matching_rows(ctl(), DAY, RUN2, "US0378331005", "isin")["count"] == 100
    out = service.compare_security_records(ctl(), DAY, RUN1, "positions/enriched/2026/11/14",
                                           "positions_enriched_20261114_20261114T200000.csv", ISIN, "isin",
                                           filters={"portfolio_code": "PF001"})
    assert out["comparison_status"] == "compared" and out["file_b"]["folder"] == "positions/enriched/2026/11/14"
    # The same folder can be named by its absolute path, with a trailing slash, or with backslashes.
    for alias in (str(env[0] / DAY), DAY + "/", DAY.replace("/", "\\"), "./" + DAY):
        assert service.list_csv_files(alias)["folder"] == DAY
    assert digest(env[0]) == before


def test_a_subfolder_csv_format_comes_from_its_mapped_root(env, service):
    nested = env[0] / "positions/report/2026"
    nested.mkdir()
    (nested / "r.csv").write_text(f"Portfolio;ISIN;Name\nPF001;{ISIN};Acme; plc\n")
    out = service.find_security_in_file(ctl(), "positions/report/2026", "r.csv", ISIN, "ISIN", filters={"Portfolio": "PF001"})
    assert out["matches_returned"] == 1 and out["matches"][0]["values"]["Name"] == "Acme"


def test_nothing_outside_the_mapped_folders_is_reachable(env, service):
    data = env[0]
    escapes = [
        "restricted", "restricted/deep", str(data / "restricted"), str(data), str(data.parent), "/", "/etc",
        "positions", "positions/enriched_other",  # a parent and a sibling of mapped folders
        "positions/enriched/../../restricted", "positions/enriched/../report/../../restricted",
        "positions/enriched/2026/../../../restricted", "positions\\enriched\\..\\..\\restricted",
        str(data / "positions/enriched/../../restricted"), "pricing/..", "pricing/../restricted", "..", "../data/restricted",
        "positions/enriched/2026/11/15/../../../../../restricted/deep",
        "positions/enriched/\x00", "positions/enriched/2026\x00/../..", "positions/enriched/" + "a/" * 40, "x" * 5000,
    ]
    for folder in escapes:
        for call in (lambda: service.list_csv_files(folder), lambda: service.list_folders(folder=folder),
                     lambda: service.inspect_csv_headers(folder, "file.csv"),
                     lambda: service.find_security_in_file(ctl(), folder, "file.csv", ISIN, "isin"),
                     lambda: service.compare_security_records(ctl(), DAY, RUN1, folder, "file.csv", ISIN, "isin")):
            assert failure(call).kind in ("folder_not_found", "folder_not_allowed", "invalid_argument"), folder
    # '..' is refused outright even where it would lead back inside a mapped folder.
    assert failure(lambda: service.list_csv_files("positions/enriched/2026/../2026")).kind == "folder_not_allowed"
    # The filename can never carry a path into or out of a subfolder.
    for name in ("11/15/" + RUN1, "../11/15/" + RUN1, "..", str(data / "restricted/file.csv")):
        assert failure(lambda: service.inspect_csv_headers("positions/enriched/2026", name)).kind == "invalid_argument"
    # A file, not a folder.
    assert failure(lambda: service.list_csv_files("positions/enriched/latest.csv")).kind == "folder_not_found"
    assert failure(lambda: service.list_csv_files("positions/enriched/2026/12")).kind == "folder_not_found"


def test_symbolic_links_never_lead_out_of_a_mapped_folder(env, service):
    data = env[0]
    root = data / "positions/enriched"
    os.symlink(data / "restricted", root / "escape")  # link to an unrelated folder
    os.symlink(data / "restricted", root / "2026/11/escape")  # ... at depth
    os.symlink(root / "2026", root / "loop")  # link back inside: still not followed
    os.symlink(data / "restricted/file.csv", data / DAY / "linked.csv")  # link to a file
    os.symlink(root, root / "2025/self")  # a cycle

    for folder in ("positions/enriched/escape", "positions/enriched/escape/deep", "positions/enriched/2026/11/escape",
                   "positions/enriched/loop", "positions/enriched/loop/11/15", "positions/enriched/2025/self/2026",
                   str(root / "escape")):
        for call in (lambda: service.list_csv_files(folder), lambda: service.list_folders(folder=folder),
                     lambda: service.count_matching_rows(ctl(), folder, "file.csv", ISIN, "isin")):
            bad = failure(call)
            assert bad.kind == "folder_not_allowed" and "symbolic link" in bad.message, folder
    # Links are not offered by discovery either.
    listing = service.list_folders(folder="positions/enriched")
    assert [f["name"] for f in listing["folders"]] == ["2026", "2025"] and listing["symbolic_links_ignored"] == 2
    assert service.list_csv_files("positions/enriched")["subfolders"] == ["positions/enriched/2026", "positions/enriched/2025"]
    assert "linked.csv" not in [f["filename"] for f in service.list_csv_files(DAY)["files"]]
    assert failure(lambda: service.inspect_csv_headers(DAY, "linked.csv")).kind == "file_unreadable"
    # A real subfolder swapped for a link after it was listed is refused when it is used.
    (root / "2026/10/30").rename(root / "2026/10/moved")
    os.symlink(data / "restricted", root / "2026/10/30")
    assert failure(lambda: service.list_csv_files("positions/enriched/2026/10/30")).kind == "folder_not_allowed"


def test_a_subfolder_swapped_for_a_link_after_the_check_is_refused(env, service):
    """The folder is checked, then replaced by a link to an unrelated folder holding a file of the same name."""
    data = env[0]
    (data / "restricted" / RUN1).write_text(f"portfolio_code,isin,secret\nPF001,{ISIN},do-not-read\n")
    of = service._open(DAY, RUN1)
    day = data / DAY
    day.rename(day.with_name("15_moved"))
    os.symlink(data / "restricted", day)
    assert failure(lambda: service._fh(of)).kind == "file_unreadable"
    day.unlink()
    # ... or a directory higher up is the one that is swapped.
    month = data / "positions/enriched/2026/11"
    month.rename(month.with_name("11_moved"))
    (data / "restricted/15").mkdir()
    (data / "restricted/15" / RUN1).write_text(f"portfolio_code,isin,secret\nPF001,{ISIN},do-not-read\n")
    os.symlink(data / "restricted", month)
    assert failure(lambda: service._fh(of)).kind == "file_unreadable"
    assert failure(lambda: service.find_security_in_file(ctl(), DAY, RUN1, ISIN, "isin")).kind == "folder_not_allowed"


def test_a_subfolder_that_is_itself_mapped_uses_its_own_map_entry(env, service):
    """The supported way to override a subfolder's metadata: map it in its own right."""
    path = env[1] / "positions.yaml"
    path.write_text(path.read_text().replace("  - id: pricing", """  - id: special
    path: positions/enriched/2026/11/13
    description: A day with its own layout.
    csv_format: {delimiter: "|"}
    record_key_columns: [pf]
  - id: pricing"""))
    (env[0] / "positions/enriched/2026/11/13/x.csv").write_text("pf|isin\nA|X1\n")
    (env[0] / "positions/enriched/2026/11/13/am").mkdir()
    days = service.list_folders(folder="positions/enriched/2026/11")
    assert [(f["name"], f["kind"], f["mapped_root"]) for f in days["folders"]] == [
        ("15", "subfolder", "positions/enriched"), ("14", "subfolder", "positions/enriched"),
        ("13", "mapped_root", "positions/enriched/2026/11/13")]
    out = service.find_security_in_file(ctl(), "positions/enriched/2026/11/13", "x.csv", "X1", "isin", filters={"pf": "A"})
    assert out["matches_returned"] == 1 and out["file"]["mapped_root"] == "positions/enriched/2026/11/13"
    # Its own subfolders belong to it, however they are addressed.
    for alias in ("positions/enriched/2026/11/13/am", str(env[0] / "positions/enriched/2026/11/13/am")):
        nested = service.list_csv_files(alias)
        assert nested["mapped_root"] == "positions/enriched/2026/11/13" and nested["record_key_columns"] == ["pf"]


def test_subfolder_listings_are_bounded_and_paged(env):
    root = env[0] / "pricing"
    for i in range(700):
        (root / f"run_{i:04d}").mkdir()
    service = make(env, max_output_bytes=8000)
    seen, offset, pages = [], 0, 0
    while True:
        page = service.list_folders(folder="pricing", offset=offset)
        assert len(json.dumps(page)) < 8000 and page["total_folders"] == 700
        seen += [f["name"] for f in page["folders"]]
        pages += 1
        if not page["truncated"]:
            break
        offset = page["next_offset"]
    assert pages > 3 and seen == [f"run_{i:04d}" for i in reversed(range(700))]
    files = service.list_csv_files("pricing")
    assert len(json.dumps(files)) < 8000 and files["subfolder_count"] == 700 and 0 < len(files["subfolders"]) <= 50
    assert "list_folders" in files["subfolders_note"] and files["files_returned"] == 1
    # A page size limit applies as well as the byte limit.
    assert make(env, max_files_per_listing=5).list_folders(folder="pricing")["folders_returned"] == 5


# ================================================================== 3. structured output, over the protocol

SUCCESS_KEYS = {
    "get_process_map": {"processes"},
    "list_folders": {"scope", "total_folders", "folders", "truncated"},
    "list_csv_files": {"folder", "kind", "mapped_root", "files", "truncated", "subfolders"},
    "inspect_csv_headers": {"file", "column_count", "columns", "columns_truncated"},
    "search_column_names": {"file", "matching_columns", "truncated"},
    "find_security_in_file": {"file", "search", "scan", "outcome", "matches", "more_matches", "results_are_partial", "next_cursor"},
    "search_across_columns": {"file", "search", "scan", "outcome", "matches", "more_matches", "results_are_partial", "next_cursor"},
    "count_matching_rows": {"file", "search", "scan", "outcome", "count", "count_is_exact", "next_cursor"},
    "compare_security_records": {"file_a", "file_b", "search", "presence", "comparison_status", "explanation"},
}


async def call(client, schemas_by_tool, tool, **args):
    result = await client.call_tool(tool, args)
    text = json.loads(result.content[0].text)
    assert result.structured_content == text, "structured content and JSON text must be the same result"
    check_schema(tool, result.structured_content, schemas_by_tool[tool])  # the schema as the client received it
    return result.structured_content, result


def over_protocol(env, body, **limits):
    async def main():
        cfg = Config(data_root=env[0], process_maps_dir=env[1], limits=Limits(**limits))
        async with Client(build_server(cfg)) as client:
            tools = (await client.list_tools()).tools
            await body(client, {t.name: t.output_schema for t in tools}, tools)
    anyio.run(main)


def test_all_nine_tools_publish_an_object_output_schema(env):
    async def body(client, published, tools):
        assert set(published) == set(SUCCESS_KEYS) and len(tools) == 9
        for tool in tools:
            schema = tool.output_schema
            assert schema and schema["type"] == "object" and "tool" in schema["required"], tool.name
            assert SUCCESS_KEYS[tool.name] <= set(schema["properties"]), tool.name
            assert {"error", "request"} <= set(schema["properties"]), tool.name
            if "filters" in tool.input_schema["properties"] and tool.name not in ("list_csv_files",):
                assert "filters" not in tool.input_schema.get("required", [])
        outcomes = published["find_security_in_file"]["properties"]["outcome"]
        assert outcomes["enum"] == ["match_found", "confirmed_absent", "inconclusive"]
        statuses = published["compare_security_records"]["properties"]["comparison_status"]["enum"]
        assert {"compared", "partially_compared", "ambiguous_alignment", "inconclusive"} <= set(statuses)
        assert published["count_matching_rows"]["properties"]["count"]["anyOf"] == [{"type": "integer"}, {"type": "null"}]
    over_protocol(env, body)


def test_the_strict_schema_check_really_rejects_undescribed_fields():
    with pytest.raises(AssertionError):
        check_schema("list_folders", {"tool": "list_folders", "surprise": 1})
    with pytest.raises(AssertionError):
        check_schema("count_matching_rows", {"tool": "count_matching_rows", "outcome": "absent", "explanation": "x"})
    assert strict({"properties": {}, "additionalProperties": True})["additionalProperties"] is True


def test_end_to_end_investigation_with_structured_evidence_only(env):
    """Definition of done: discover nested files, pin down one security/portfolio record, compare it between
    stages - using structured content alone, with nothing outside the mapped folders touched."""
    before = digest(env[0])

    async def body(client, published, _tools):
        async def use(tool, **args):
            out, result = await call(client, published, tool, **args)
            assert not result.is_error and SUCCESS_KEYS[tool] <= set(out), (tool, out)
            return out

        process = (await use("get_process_map"))["processes"][0]["id"]
        full, _ = await call(client, published, "get_process_map", process=process)
        assert full["process"]["folders"][0]["record_key_columns"] == ["portfolio_code", "isin"]
        folders = (await use("list_folders", process=process))["folders"]
        enriched = next(f for f in folders if f["folder"].endswith("enriched"))
        assert enriched["kind"] == "mapped_root" and enriched["record_key_columns"] == ["portfolio_code", "isin"]
        folder = enriched["folder"]
        while True:  # walk down year / month / day, newest first, one level per call
            level = await use("list_folders", folder=folder)
            if not level["folders"]:
                break
            assert level["folders"][0]["kind"] == "subfolder" and level["folders"][0]["mapped_root"] == enriched["folder"]
            folder = level["folders"][0]["folder"]
        assert folder == DAY
        files = await use("list_csv_files", folder=folder, date_from="2026-11-15")
        latest = files["files"][0]["filename"]
        assert latest == RUN2 and files["mapped_root"] == enriched["folder"]
        await use("inspect_csv_headers", folder=folder, filename=latest, sample_rows=2)
        assert (await use("search_column_names", folder=folder, filename=latest, term="portfolio"))["matching_column_count"] == 1

        key = {files["record_key_columns"][0]: "PF042"}
        found = await use("find_security_in_file", folder=folder, filename=latest, identifier=ISIN,
                          identifier_column="isin", filters=key)
        assert found["outcome"] == "match_found" and found["matches_returned"] == 1 and found["results_are_partial"] is False
        assert found["search"]["filters"] == [{"column": "portfolio_code", "value": "PF042"}]
        assert found["file"] == {"folder": DAY, "mapped_root": "positions/enriched", "filename": RUN2,
                                 "size_bytes": found["file"]["size_bytes"],
                                 "filesystem_modified_time": found["file"]["filesystem_modified_time"]}
        absent = await use("find_security_in_file", folder=folder, filename=latest, identifier=ISIN,
                           identifier_column="isin", filters={"portfolio_code": "ABC"})
        assert absent["outcome"] == "confirmed_absent" and absent["scan"]["complete"] is True and absent["next_cursor"] is None
        assert absent["near_misses"]["identifier_matched_but_filters_did_not"]["count"] == N_PORTFOLIOS + 4
        count = await use("count_matching_rows", folder=folder, filename=latest, identifier=ISIN,
                          identifier_column="isin", filters={"portfolio_code": "PF007"})
        assert count["count"] == 2 and count["count_is_exact"] is True
        across = await use("search_across_columns", folder=folder, filename=latest, identifier="0123456", filters=key)
        assert across["matches"][0]["matched_columns"] == ["sedol"]

        stages = dict(folder_a=folder, filename_a=latest, folder_b="positions/report", filename_b=REPORT, identifier=ISIN,
                      identifier_column_a="isin", identifier_column_b="ISIN",
                      column_mapping={"portfolio_code": "Portfolio", "market_value": "MarketValue"})
        compared = await use("compare_security_records", **stages, filters=key, compare_columns=["market_value"])
        assert compared["comparison_status"] == "compared" and compared["comparisons"][0]["changed_count"] == 0
        ambiguous = await use("compare_security_records", **stages, filters={"portfolio_code": "PF007"})
        assert ambiguous["comparison_status"] == "ambiguous_alignment" and "comparisons" not in ambiguous
        assert len(ambiguous["records_in_file_a"]) == 2 and len(ambiguous["records_in_file_b"]) == 1
        unfiltered = await use("compare_security_records", **stages)
        assert unfiltered["comparison_status"] == "inconclusive"
        partial = await use("compare_security_records", folder_a=folder, filename_a=RUN1, folder_b=folder, filename_b=RUN2,
                            identifier="PF007", identifier_column_a="portfolio_code", match_keys=["isin"],
                            compare_columns=["price"])
        assert partial["comparison_status"] == "partially_compared" and partial["ambiguous_keys"]
        dropped = await use("compare_security_records", **stages, filters={"portfolio_code": "PF003"})
        assert dropped["comparison_status"] == "present_in_one_file_only"
        assert dropped["presence"]["file_b"] ["outcome"] == "confirmed_absent"

    over_protocol(env, body)
    assert digest(env[0]) == before


def test_failures_are_structured_too_and_keep_the_folder_boundary(env):
    os.symlink(env[0] / "restricted", env[0] / "positions/enriched/escape")

    async def body(client, published, _tools):
        for tool, args, kind in [
            ("list_csv_files", {"folder": "restricted"}, "folder_not_found"),
            ("list_csv_files", {"folder": "positions/enriched/../../restricted"}, "folder_not_allowed"),
            ("list_folders", {"folder": "positions/enriched/escape"}, "folder_not_allowed"),
            ("list_folders", {"folder": str(env[0])}, "folder_not_found"),
            ("inspect_csv_headers", {"folder": "positions/enriched/escape", "filename": "file.csv"}, "folder_not_allowed"),
            ("search_column_names", {"folder": DAY, "filename": "../../../../../restricted/file.csv", "term": "x"}, "invalid_argument"),
            ("get_process_map", {"process": "nope"}, "process_not_found"),
            ("find_security_in_file", {"folder": DAY, "filename": RUN2, "identifier": ISIN, "identifier_column": "isin",
                                       "filters": {"portfolio": "ABC"}}, "column_not_found"),
            ("search_across_columns", {"folder": "/etc", "filename": "passwd.csv", "identifier": "root"}, "folder_not_found"),
            ("count_matching_rows", {"folder": DAY, "filename": RUN2, "identifier": ISIN, "identifier_column": "isin",
                                     "filters": {"isin": ISIN}}, "invalid_argument"),
            ("compare_security_records", {"folder_a": DAY, "filename_a": RUN2, "folder_b": "restricted/deep",
                                          "filename_b": "file.csv", "identifier": ISIN, "identifier_column_a": "isin"},
             "folder_not_found"),
        ]:
            out, result = await call(client, published, tool, **args)
            assert result.is_error and out["error"]["type"] == kind and out["request"], tool
            assert "do-not-read" not in json.dumps(out)
            if tool in ("find_security_in_file", "search_across_columns", "count_matching_rows", "compare_security_records"):
                assert out["outcome"] == "inconclusive" and out["explanation"].startswith("INCONCLUSIVE")
        out, _ = await call(client, published, "find_security_in_file", folder=DAY, filename=RUN2, identifier=ISIN,
                            identifier_column="isin", filters={"portfolio": "ABC"})
        assert out["request"]["filters"] == {"portfolio": "ABC"}
        # Filter values must be text; a number is rejected before any file is touched.
        result = await client.call_tool("count_matching_rows", {"folder": DAY, "filename": RUN2, "identifier": ISIN,
                                                               "identifier_column": "isin", "filters": {"portfolio_code": 42}})
        assert result.is_error

    over_protocol(env, body)


def test_partial_truncated_and_interrupted_results_fit_the_schema_within_the_size_limit(env):
    name = big_file(env)
    limit = 8000

    async def body(client, published, _tools):
        async def sized(tool, **args):
            out, result = await call(client, published, tool, **args)
            assert len(result.content[0].text.encode()) <= limit, tool
            assert len(json.dumps(result.structured_content, ensure_ascii=False).encode()) <= limit, tool
            assert "response_too_large" not in result.content[0].text, tool
            return out

        # Truncated by rows / size, then continued by cursor with the filters still applied.
        args = dict(folder=DAY, filename=name, identifier=ISIN, identifier_column="isin", filters={"portfolio_code": "PF123"})
        page = await sized("find_security_in_file", **args)
        assert page["results_are_partial"] is True and page["more_matches"] == "yes" and page["next_cursor"]
        assert page["scan"]["complete"] is False and page["scan"]["stop_reason"] in ("row_limit", "output_size_limit")
        got = page["matches_returned"]
        while page["next_cursor"]:
            page = await sized("find_security_in_file", **args, cursor=page["next_cursor"])
            got += page["matches_returned"]
            assert all(m["values"]["portfolio_code"] == "PF123" for m in page["matches"])
        assert got == 300 and page["more_matches"] == "none"
        # A cursor from one filter is refused for another: an error, in the same structure.
        first, _ = await call(client, published, "find_security_in_file", **args)
        out, result = await call(client, published, "find_security_in_file", **{**args, "filters": {"portfolio_code": "PF124"}},
                                 cursor=first["next_cursor"])
        assert result.is_error and out["error"]["type"] == "invalid_cursor" and out["outcome"] == "inconclusive"

        # Interrupted by the time limit.
        cut = await sized("count_matching_rows", folder=DAY, filename=name, identifier=ISIN, identifier_column="isin",
                          filters={"portfolio_code": "ABC"}, max_scan_seconds=0.001)
        assert cut["count"] is None and cut["count_is_exact"] is False and cut["outcome"] == "inconclusive"
        assert cut["scan"]["stop_reason"] == "time_limit" and cut["next_cursor"]
        cut = await sized("compare_security_records", folder_a=DAY, filename_a=name, folder_b=DAY, filename_b=RUN2,
                          identifier=ISIN, identifier_column_a="isin", max_scan_seconds=0.001,
                          filters={"portfolio_code": "ABC"})
        assert cut["comparison_status"] == "inconclusive" and cut["presence"]["file_b"]["stop_reason"] == "not_scanned"
        # Too many matches for one comparison, wide listings, clipped samples.
        many = await sized("compare_security_records", folder_a=DAY, filename_a=RUN1, folder_b=DAY, filename_b=RUN2,
                           identifier=ISIN, identifier_column_a="isin", match_keys=["portfolio_code"])
        assert many["comparison_status"] == "inconclusive" and many["presence"]["file_a"]["stop_reason"] == "too_many_matches"
        keyed = await sized("compare_security_records", folder_a=DAY, filename_a=RUN1, folder_b=DAY, filename_b=RUN2,
                            identifier="MAIN", identifier_column_a="book", match_keys=["portfolio_code"],
                            filters={"isin": "US0378331005", "quantity": "10"}, filters_b={"sedol": "2046251"})
        assert keyed["presence"]["file_a"]["stop_reason"] == "too_many_matches"
        await sized("inspect_csv_headers", folder=DAY, filename=name, sample_rows=10)
        await sized("list_folders")
        await sized("list_folders", folder="positions/enriched/2026/11")
        await sized("list_csv_files", folder=DAY)
        await sized("get_process_map", process="positions")

    over_protocol(env, body, max_output_bytes=limit, chunk_bytes=64 * 1024, max_record_bytes=64 * 1024)


def test_text_only_clients_still_get_the_whole_result(env):
    async def body(client, _published, _tools):
        result = await client.call_tool("find_security_in_file", {
            "folder": DAY, "filename": RUN2, "identifier": ISIN, "identifier_column": "isin",
            "filters": {"portfolio_code": "PF042"}})
        assert len(result.content) == 1 and result.content[0].type == "text"
        text = json.loads(result.content[0].text)
        assert text["tool"] == "find_security_in_file" and text["outcome"] == "match_found"
        assert text["matches"][0]["values"]["quantity"] == "142" and text["search"]["filters"]
    over_protocol(env, body)
