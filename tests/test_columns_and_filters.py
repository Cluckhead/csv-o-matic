"""One column-selection contract, faithful returned values, honest comparisons, and exact record filters."""

from __future__ import annotations

import csv

import pytest

from csv_o_matic.csvscan import ScanControl
from csv_o_matic.service import ToolFailure

from conftest import digest, make_service

A = "stage/a"
B = "stage/b"


def put(env, folder: str, name: str, text: str) -> None:
    (env[0] / folder / name).write_text(text)


def failure(call) -> ToolFailure:
    with pytest.raises(ToolFailure) as info:
        call()
    return info.value


def find(service, name, identifier, column, **kwargs):
    return service.find_security_in_file(ScanControl(), A, name, identifier, column, **kwargs)


def compare(service, name_a, name_b, identifier, column="ID", **kwargs):
    return service.compare_security_records(ScanControl(), A, name_a, A, name_b, identifier, column, **kwargs)


# ---------------------------------------------------------------- selecting columns

@pytest.fixture
def hashed(env):
    """A header literally named '#1' sits in position 3."""
    put(env, A, "hash.csv", "Code,Val,#1\nc1,v1,h1\nc2,v2,h2\n")
    put(env, A, "hash_b.csv", "Code,Val,#1\nc1,v1,DIFFERENT\n")


def test_a_literal_hash_header_and_positional_selection_coexist(env, service, hashed):
    # The plain string could mean two different columns: refused, with the way to say which.
    ambiguous = failure(lambda: find(service, "hash.csv", "h1", "#1"))
    assert ambiguous.kind == "ambiguous_column" and '{"name": "#1"}' in ambiguous.message and '{"position": N}' in ambiguous.message
    assert find(service, "hash.csv", "h1", {"name": "#1"})["matches"][0]["values"]["Code"] == "c1"
    assert find(service, "hash.csv", "c1", {"position": 1})["search"]["column"] == "Code"
    assert find(service, "hash.csv", "h1", {"position": 1})["outcome"] == "confirmed_absent"
    # Shorthand that has only one reading keeps working, as do plain names.
    assert find(service, "hash.csv", "v1", "#2")["search"]["column"] == "Val"
    assert find(service, "hash.csv", "h1", "#3")["search"]["column"] == "#1"
    assert find(service, "hash.csv", "v1", "Val")["outcome"] == "match_found"


def test_the_same_selector_means_the_same_column_in_every_argument(env, service, hashed):
    by_name, by_position = {"name": "#1"}, {"position": 1}
    assert find(service, "hash.csv", "c1", "Code", columns=[by_name, by_position])["matches"][0]["values"] == {"#1": "h1", "Code": "c1"}
    assert failure(lambda: find(service, "hash.csv", "c1", "Code", columns=["#1"])).kind == "ambiguous_column"
    sample = service.inspect_csv_headers(A, "hash.csv", sample_rows=1, sample_columns=[by_name])
    assert sample["sample_rows"][0]["values"] == {"#1": "h1"}
    assert failure(lambda: service.inspect_csv_headers(A, "hash.csv", sample_rows=1, sample_columns=["#1"])).kind == "ambiguous_column"
    assert find(service, "hash.csv", "c1", "Code", filters=[{"column": by_name, "value": "h1"}])["matches_returned"] == 1
    assert find(service, "hash.csv", "c1", "Code", filters=[{"column": by_position, "value": "h1"}])["matches_returned"] == 0
    assert failure(lambda: find(service, "hash.csv", "c1", "Code", filters=[{"column": "#1", "value": "h1"}])).kind == "ambiguous_column"
    assert service.count_matching_rows(ScanControl(), A, "hash.csv", "h2", by_name)["count"] == 1
    assert failure(lambda: service.count_matching_rows(ScanControl(), A, "hash.csv", "h2", "#1")).kind == "ambiguous_column"
    out = compare(service, "hash.csv", "hash_b.csv", "c1", by_position, compare_columns=[by_name], match_keys=[{"name": "Val"}])
    assert out["search"]["compared_columns"] == [{"a": "#1", "b": "#1"}] and out["comparisons"][0]["changed"][0]["value_b"] == "DIFFERENT"
    assert failure(lambda: compare(service, "hash.csv", "hash_b.csv", "c1", "Code", compare_columns=["#1"])).kind == "ambiguous_column"
    assert failure(lambda: compare(service, "hash.csv", "hash_b.csv", "c1", "Code", match_keys=["#1"])).kind == "ambiguous_column"
    assert failure(lambda: compare(service, "hash.csv", "hash_b.csv", "c1", "Code", column_mapping={"#1": "Val"})).kind == "ambiguous_column"
    assert failure(lambda: compare(service, "hash.csv", "hash_b.csv", "c1", "Code", compare_columns=["Val"],
                                   column_mapping=[{"a": "Val", "b": "#1"}])).kind == "ambiguous_column"


def test_explicit_selectors_are_validated_not_guessed(env, service, hashed):
    assert failure(lambda: find(service, "hash.csv", "x", {"position": 4})).kind == "column_not_found"
    assert failure(lambda: find(service, "hash.csv", "x", {"position": 0})).kind == "column_not_found"
    assert failure(lambda: find(service, "hash.csv", "x", "#9")).kind == "column_not_found"
    assert failure(lambda: find(service, "hash.csv", "x", {"name": "#2"})).kind == "column_not_found"  # a name is never a position
    assert failure(lambda: find(service, "hash.csv", "x", {"name": "Code", "position": 1})).kind == "invalid_argument"
    assert failure(lambda: find(service, "hash.csv", "x", {})).kind == "invalid_argument"
    assert failure(lambda: find(service, "hash.csv", "x", 3)).kind == "invalid_argument"
    assert failure(lambda: find(service, "hash.csv", "x", {"position": True})).kind == "invalid_argument"


def test_header_tools_hand_back_selectors_that_work(env, service, hashed):
    long_name = "L" * 300
    put(env, A, "awkward.csv", f"ID,Price,Price,{long_name},#2,,Plain\nK,1,2,long,hash,empty,p\n")
    out = service.inspect_csv_headers(A, "awkward.csv")
    assert "position 1+i" in out["column_selection"]
    awkward = {c["position"]: c["selector"] for c in out["columns_needing_a_position_selector"]}
    assert awkward == {i: {"position": i} for i in (2, 3, 4, 5, 6)}  # repeated, shortened, '#N'-shaped and empty names
    hits = service.search_column_names(A, "awkward.csv", "p")
    assert [(h["position"], h["selector"]) for h in hits["matching_columns"]] == [(2, {"position": 2}), (3, {"position": 3}), (7, "Plain")]
    # Every selector handed out, for every column, selects exactly that column.
    expected = ["K", "1", "2", "long", "hash", "empty", "p"]
    all_hits = [h for term in ("i", "p", "l", "#", "a")
                for h in service.search_column_names(A, "awkward.csv", term)["matching_columns"]]
    assert {h["position"] for h in all_hits} >= {1, 2, 3, 4, 5, 7}
    for hit in all_hits + [{"position": 6, "selector": awkward[6]}]:
        value = expected[hit["position"] - 1]
        assert find(service, "awkward.csv", value, hit["selector"])["outcome"] == "match_found", hit
    plain = service.inspect_csv_headers(A, "hash.csv")
    assert plain["columns_needing_a_position_selector"] == [{"position": 3, "column": "#1", "selector": {"position": 3}}]
    assert "columns_needing_a_position_selector" not in service.inspect_csv_headers(A, "many.csv")


# ---------------------------------------------------------------- every returned cell is kept

def test_an_overflow_field_never_overwrites_a_real_column_of_the_same_label(env, service):
    put(env, A, "extra.csv", "ID,[extra field 3]\nK,real,overflow,more\nJ,solo\n")
    row = find(service, "extra.csv", "K", "ID")["matches"][0]
    assert row["values"] == {"ID": "K", "[extra field 3]": "real", "[extra field 3]'": "overflow", "[extra field 4]": "more"}
    assert row["field_count_mismatch"] == {"header_columns": 2, "record_fields": 4}  # the malformed-row warning stays
    across = service.search_across_columns(ScanControl(), A, "extra.csv", "overflow")["matches"][0]
    assert across["matched_columns"] == ["[extra field 3]'"] and across["values"]["[extra field 3]"] == "real"
    assert find(service, "extra.csv", "J", "ID")["matches"][0]["values"] == {"ID": "J", "[extra field 3]": "solo"}


def test_duplicate_and_shortened_header_labels_never_collide(env, service):
    long_a, long_b = "N" * 250 + "a", "N" * 250 + "b"  # identical once shortened
    put(env, A, "labels.csv", f"ID,Px,Px,Px [col 2],{long_a},{long_b}\nK,1,2,3,4,5\n")
    values = find(service, "labels.csv", "K", "ID")["matches"][0]["values"]
    assert len(values) == 6 and sorted(values.values()) == ["1", "2", "3", "4", "5", "K"]
    short = find(service, "labels.csv", "K", "ID", columns=[{"position": 6}, {"position": 2}])["matches"][0]
    assert sorted(short["values"].values()) == ["1", "5"]
    put(env, A, "short.csv", "ID,A,B\nK,1\n")
    missing = find(service, "short.csv", "K", "ID", columns=["A", "B"])["matches"][0]
    assert missing["values"] == {"A": "1"} and missing["requested_columns_missing_from_record"] == ["B"]


# ---------------------------------------------------------------- comparisons say what they covered

def test_two_files_with_no_paired_columns_are_not_reported_as_compared(env, service):
    put(env, A, "one.csv", "ID,Price,Qty\nK,10,5\n")
    put(env, A, "two.csv", "sec,px,quantity\nK,99,7\n")
    out = compare(service, "one.csv", "two.csv", "K", identifier_column_b="sec")
    assert out["comparison_status"] == "no_comparable_columns" and "comparisons" not in out
    assert out["search"]["compared_column_count"] == 0 and "NO columns are paired" in out["search"]["coverage_warning"]
    assert out["presence"]["file_a"]["outcome"] == "match_found" and out["presence"]["file_b"]["outcome"] == "match_found"
    assert "not a finding that the records agree" in out["explanation"]
    assert out["records_in_file_a"][0]["values"] == {"ID": "K"} and out["records_in_file_b"][0]["values"] == {"sec": "K"}
    # Presence findings stand on their own when the security is missing somewhere.
    gone = compare(service, "one.csv", "two.csv", "ZZ", identifier_column_b="sec")
    assert gone["comparison_status"] == "absent_from_both" and gone["search"]["compared_column_count"] == 0
    # With the correspondence spelled out there is something to compare.
    mapped = compare(service, "one.csv", "two.csv", "K", column_mapping={"ID": "sec", "Price": "px", "Qty": "quantity"})
    assert mapped["comparison_status"] == "compared" and mapped["search"]["compared_column_count"] == 3
    assert mapped["search"]["compared_columns_other_than_identifier_and_keys"] == 2 and mapped["comparisons"][0]["changed_count"] == 2
    assert "3 column(s) compared per pair" in mapped["explanation"] and "coverage_warning" not in mapped["search"]


def test_comparing_only_identifiers_is_called_out(env, service):
    put(env, A, "one.csv", "ID,Price\nK,10\n")
    put(env, A, "two.csv", "ID,px\nK,99\n")
    out = compare(service, "one.csv", "two.csv", "K")  # only 'ID' is common to both files
    assert out["comparison_status"] == "compared" and out["search"]["compared_column_count"] == 1
    assert out["search"]["compared_columns_other_than_identifier_and_keys"] == 0
    assert "ONLY columns compared are the identifier" in out["search"]["coverage_warning"]
    assert "ONLY identifier and match-key columns were compared" in out["explanation"]
    assert out["comparisons"][0]["columns_compared"] == 1 and out["record_pairs_compared"] == 1


def test_duplicate_price_columns_are_mapped_explicitly_by_position(env, service):
    put(env, A, "dup_a.csv", "ID,Price,Price\nK,1,2\n")
    put(env, A, "dup_b.csv", "ID,Price,Price\nK,1,3\n")
    default = compare(service, "dup_a.csv", "dup_b.csv", "K")
    assert default["search"]["compared_column_count"] == 1 and default["columns_not_compared"]["only_in_file_a_count"] == 2
    # Source-side positions work, with compare_columns omitted ...
    mapping = [{"a": {"position": 2}, "b": {"position": 2}}, {"a": {"position": 3}, "b": {"position": 3}}]
    out = compare(service, "dup_a.csv", "dup_b.csv", "K", column_mapping=mapping)
    pair = out["comparisons"][0]
    assert out["search"]["compared_column_count"] == 3 and pair["changed_count"] == 1 and pair["unchanged_count"] == 2
    assert pair["changed"] == [{"column_a": "Price [col 3]", "value_a": "2", "value_b": "3"}]
    # ... and given; a cross mapping compares what was asked for, not what the names suggest.
    crossed = compare(service, "dup_a.csv", "dup_b.csv", "K", compare_columns=[{"position": 2}],
                      column_mapping=[{"a": "#2", "b": "#3"}])
    assert crossed["comparisons"][0]["changed"] == [
        {"column_a": "Price [col 2]", "column_b": "Price [col 3]", "value_a": "1", "value_b": "3"}]
    # A repeated name without a mapping is refused for an explicit request, never guessed.
    assert failure(lambda: compare(service, "dup_a.csv", "dup_b.csv", "K", compare_columns=["Price"])).kind == "ambiguous_column"
    assert failure(lambda: compare(service, "dup_a.csv", "dup_b.csv", "K", compare_columns=[{"position": 2}])).kind == "ambiguous_column"


def test_a_mapping_that_is_invalid_or_would_be_ignored_is_refused(env, service):
    put(env, A, "one.csv", "ID,Price,Qty\nK,10,5\n")
    put(env, A, "two.csv", "ID,px,Qty\nK,99,5\n")
    assert failure(lambda: compare(service, "one.csv", "two.csv", "K", column_mapping={"Nope": "px"})).kind == "column_not_found"
    assert failure(lambda: compare(service, "one.csv", "two.csv", "K", column_mapping={"Price": "nope"})).kind == "column_not_found"
    assert failure(lambda: compare(service, "one.csv", "two.csv", "K", column_mapping=[{"a": "Price"}])).kind == "invalid_argument"
    assert failure(lambda: compare(service, "one.csv", "two.csv", "K", column_mapping="Price=px")).kind == "invalid_argument"
    twice = failure(lambda: compare(service, "one.csv", "two.csv", "K",
                                    column_mapping=[{"a": "Price", "b": "px"}, {"a": {"position": 2}, "b": "Qty"}]))
    assert twice.kind == "invalid_argument" and "more than once" in twice.message
    unused = failure(lambda: compare(service, "one.csv", "two.csv", "K", compare_columns=["Qty"], column_mapping={"Price": "px"}))
    assert unused.kind == "unused_column_mapping" and "Price" in unused.message and "nothing was compared" in unused.message
    used = compare(service, "one.csv", "two.csv", "K", compare_columns=["Qty", "Price"], column_mapping={"Price": "px"})
    assert used["search"]["compared_column_count"] == 2 and used["comparisons"][0]["changed_count"] == 1
    as_key = compare(service, "one.csv", "two.csv", "K", compare_columns=["Qty"], match_keys=["Price"], column_mapping={"Price": "px"})
    assert as_key["search"]["match_keys"] == [{"a": "Price", "b": "px"}]  # a mapping used by a match key is used


@pytest.fixture
def ragged(env):
    put(env, A, "rag_a.csv", "ID,Portfolio,Qty\nK\nK,P1,5\nK,,7\nK,P2\nK,P3,1\n")
    put(env, A, "rag_b.csv", "ID,Portfolio,Qty\nK\nK,P1,6\nK,,7\nK,P2,9\nK,P3\n")


def test_records_without_a_match_key_field_are_never_paired_on_nothing(env, service, ragged):
    out = compare(service, "rag_a.csv", "rag_b.csv", "K", match_keys=["Portfolio"])
    assert out["comparison_status"] == "partially_compared" and out["record_pairs_compared"] == 4
    assert out["unalignable_records"] == [
        {"file": "file_a", "record_number": 2, "missing_key_fields": ["Portfolio"], "reason": "the record is too short to have this match-key field"},
        {"file": "file_b", "record_number": 2, "missing_key_fields": ["Portfolio"], "reason": "the record is too short to have this match-key field"}]
    assert "2 without a usable match key" in out["explanation"]
    by_key = {c["key"]["Portfolio"]: c for c in out["comparisons"]}
    # An explicitly empty key is a value, and matches another empty key; a missing one is not.
    assert set(by_key) == {"P1", "", "P2", "P3"} and by_key[""]["changed_count"] == 0 and by_key[""]["unchanged_count"] == 3
    assert by_key["P1"]["changed"][0]["value_b"] == "6"


def test_structurally_missing_fields_are_reported_as_missing_not_as_agreement(env, service, ragged):
    out = compare(service, "rag_a.csv", "rag_b.csv", "K", match_keys=["Portfolio"])
    by_key = {c["key"]["Portfolio"]: c for c in out["comparisons"]}
    p2, p3 = by_key["P2"], by_key["P3"]
    assert p2["missing"] == [{"column_a": "Qty", "missing_in": ["file_a"], "value_b": "9"}]
    assert p2["missing_count"] == 1 and p2["changed_count"] == 0 and p2["unchanged_count"] == 2
    assert p3["missing"] == [{"column_a": "Qty", "missing_in": ["file_b"], "value_a": "1"}] and "not evidence of agreement" in p3["missing_note"]
    assert "2 pair(s) have fields missing from a record, which were not compared" in out["explanation"]
    # Both records short in the same place: still not "unchanged".
    put(env, A, "short_a.csv", "ID,Qty\nK\n")
    put(env, A, "short_b.csv", "ID,Qty\nK\n")
    both = compare(service, "short_a.csv", "short_b.csv", "K")["comparisons"][0]
    assert both["missing"] == [{"column_a": "Qty", "missing_in": ["file_a", "file_b"]}] and both["unchanged_count"] == 1
    # Without usable keys at all, nothing is paired and the status says so.
    put(env, A, "nokey_a.csv", "ID,Portfolio\nK\nK\n")
    put(env, A, "nokey_b.csv", "ID,Portfolio\nK\n")
    none = compare(service, "nokey_a.csv", "nokey_b.csv", "K", match_keys=["Portfolio"])
    assert none["comparison_status"] == "ambiguous_alignment" and len(none["unalignable_records"]) == 3 and "comparisons" not in none


# ---------------------------------------------------------------- exact record filters

POSITIONS = (
    "ID,Portfolio,TradeDate,Qty,Code\n"
    "00123,P1,2026-11-13,10,007\n"
    "00123,P2,2026-11-13,20,7\n"
    "00123,P1,2026-11-14,30, 007\n"
    "00123,P1,,40,007\n"
    "00123,P2\n"
    "123,P1,2026-11-13,1,007\n"
)


@pytest.fixture
def positions(env):
    put(env, A, "pos.csv", POSITIONS)


def qty(out) -> list[str]:
    return [m["values"].get("Qty") for m in out["matches"]]


def test_filters_pick_one_record_among_duplicates_by_portfolio_and_date(env, service, positions):
    before = digest(env[0])
    assert qty(find(service, "pos.csv", "00123", "ID")) == ["10", "20", "30", "40", None]
    out = find(service, "pos.csv", "00123", "ID", filters=[{"column": "Portfolio", "value": "P1"},
                                                          {"column": "TradeDate", "value": "2026-11-13"}])
    assert qty(out) == ["10"] and out["outcome"] == "match_found" and out["more_matches"] == "none"
    assert out["search"]["filters"] == [{"column": "Portfolio", "position": 2, "value": "P1"},
                                        {"column": "TradeDate", "position": 3, "value": "2026-11-13"}]
    assert "same record" in out["search"]["scope"] and "within the filters" in out["explanation"]
    assert out["identifier_matches_failing_filters"]["count"] == 4 and out["identifier_matches_failing_filters"]["count_is_exact"]
    assert service.count_matching_rows(ScanControl(), A, "pos.csv", "00123", "ID",
                                       filters=[{"column": "Portfolio", "value": "P1"}])["count"] == 3
    across = service.search_across_columns(ScanControl(), A, "pos.csv", "P1", filters=[{"column": "ID", "value": "123"}])
    assert qty(across) == ["1"]
    assert digest(env[0]) == before


def test_conditions_met_on_different_rows_are_not_a_match(env, service, positions):
    # P2 exists for this security, and so does 2026-11-14 - but never in the same record.
    filters = [{"column": "Portfolio", "value": "P2"}, {"column": "TradeDate", "value": "2026-11-14"}]
    out = find(service, "pos.csv", "00123", "ID", filters=filters)
    assert out["outcome"] == "no_match_within_filters" and out["matches"] == [] and out["scan"]["complete"]
    assert "NOT that the identifier is absent from the file" in out["explanation"]
    assert "matched in 5 record(s) of the file that did not satisfy the filters" in out["explanation"]
    count = service.count_matching_rows(ScanControl(), A, "pos.csv", "00123", "ID", filters=filters)
    assert count["count"] == 0 and count["count_is_exact"] and count["outcome"] == "no_match_within_filters"
    assert count["identifier_matches_failing_filters"] == {**count["identifier_matches_failing_filters"], "count": 5, "count_is_exact": True}


def test_an_identifier_absent_from_the_file_is_distinguishable_from_one_outside_the_filters(env, service, positions):
    filters = [{"column": "Portfolio", "value": "P1"}]
    nowhere = find(service, "pos.csv", "99999", "ID", filters=filters)
    assert nowhere["outcome"] == "no_match_within_filters" and nowhere["identifier_matches_failing_filters"]["count"] == 0
    assert "did not match in any record outside the filters either" in nowhere["explanation"]
    elsewhere = find(service, "pos.csv", "00123", "ID", filters=[{"column": "Portfolio", "value": "P9"}])
    assert elsewhere["outcome"] == "no_match_within_filters" and elsewhere["identifier_matches_failing_filters"]["count"] == 5


def test_an_empty_value_matches_an_empty_field_but_not_a_missing_one(env, service, positions):
    assert qty(find(service, "pos.csv", "00123", "ID", filters=[{"column": "TradeDate", "value": ""}])) == ["40"]
    # Record 6 ('00123,P2') has no TradeDate field at all: it matches neither "" nor anything else.
    assert qty(find(service, "pos.csv", "00123", "ID", filters=[{"column": "Portfolio", "value": "P2"}])) == ["20", None]
    assert qty(find(service, "pos.csv", "00123", "ID", filters=[{"column": "Portfolio", "value": "P2"},
                                                               {"column": "Qty", "value": ""}])) == []


def test_filters_keep_leading_zeroes_whitespace_and_case(env, service, positions):
    code = lambda value: qty(find(service, "pos.csv", "00123", "ID", filters=[{"column": "Code", "value": value}]))  # noqa: E731
    assert code("007") == ["10", "40"] and code("7") == ["20"] and code(" 007") == ["30"] and code("07") == []
    assert qty(find(service, "pos.csv", "00123", "ID", filters=[{"column": "Portfolio", "value": "p1"}])) == []
    assert qty(find(service, "pos.csv", "00123", "ID", filters=[{"column": "TradeDate", "value": "2026-11"}])) == []  # no substrings
    assert qty(find(service, "pos.csv", "00123", "ID", filters=[{"column": "Qty", "value": "10.0"}])) == []  # no number conversion


def test_invalid_filters_are_argument_errors_not_evidence_of_absence(env, service, positions):
    bad = lambda filters: failure(lambda: find(service, "pos.csv", "00123", "ID", filters=filters))  # noqa: E731
    assert bad([{"column": "Portfolo", "value": "P1"}]).kind == "column_not_found"
    twice = bad([{"column": "Portfolio", "value": "P1"}, {"column": {"position": 2}, "value": "P2"}])
    assert twice.kind == "invalid_argument" and "more than once" in twice.message
    assert bad([{"column": "Portfolio", "value": "P1"}, {"column": "#2", "value": "P1"}]).kind == "invalid_argument"
    assert bad([{"column": "Qty", "value": 10}]).kind == "invalid_argument"
    assert bad([{"column": "Qty"}]).kind == "invalid_argument"
    assert bad([{"column": "Qty", "value": "1", "mode": "contains"}]).kind == "invalid_argument"
    assert bad("Portfolio=P1").kind == "invalid_argument"
    assert bad([{"column": "Qty", "value": str(i)} for i in range(11)]).kind == "invalid_argument"
    assert bad([{"column": "Qty", "value": "x" * 201}]).kind == "invalid_argument"
    put(env, A, "dupcol.csv", "ID,Px,Px\nK,1,2\n")
    assert failure(lambda: find(service, "dupcol.csv", "K", "ID", filters=[{"column": "Px", "value": "1"}])).kind == "ambiguous_column"


def test_every_predicate_is_echoed_and_shortening_is_flagged(env, service, positions):
    long_value = "v" * 150
    filters = [{"column": name, "value": long_value if name == "Code" else "x"} for name in ("Code", "Qty", "TradeDate", "Portfolio")]
    echo = find(service, "pos.csv", "00123", "ID", filters=filters)["search"]["filters"]
    assert [f["column"] for f in echo] == ["Portfolio", "TradeDate", "Qty", "Code"]  # all of them, in column order
    assert echo[3] == {"column": "Code", "position": 5, "value": "v" * 80, "value_shortened_for_display": True, "value_length": 150}
    assert "value_shortened_for_display" not in echo[0]


def test_behaviour_without_filters_is_unchanged(env, service, positions):
    for out in (find(service, "pos.csv", "00123", "ID"), find(service, "pos.csv", "00123", "ID", filters=[]),
                find(service, "pos.csv", "00123", "ID", filters=None)):
        assert "filters" not in out["search"] and "scope" not in out["search"] and "identifier_matches_failing_filters" not in out
        assert out["matches_returned"] == 5
    assert find(service, "pos.csv", "99999", "ID")["outcome"] == "confirmed_absent"
    assert service.count_matching_rows(ScanControl(), A, "pos.csv", "99999", "ID")["count"] == 0


@pytest.fixture
def busy(env):
    """One security with 1,000 records; exactly two of them are for portfolio PX."""
    with open(env[0] / A / "busy.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ID", "Portfolio", "Seq", "Price"])
        for i in range(1000):
            w.writerow(["SEC", "PX" if i in (400, 900) else f"P{i % 7}", i, "10.5"])
    put(env, A, "busy_b.csv", "sec,pf,n,px\nSEC,TARGET,400,10.5\nSEC,TARGET,900,11\nSEC,OTHER,1,1\n")


def test_filtered_matches_page_without_gaps_or_repeats_and_the_cursor_is_bound_to_the_filters(env, busy):
    service = make_service(env, chunk_bytes=1024, max_record_bytes=1024)
    filters = [{"column": "Portfolio", "value": "P3"}, {"column": "Price", "value": "10.5"}]
    expected = [str(i) for i in range(1000) if i % 7 == 3 and i not in (400, 900)]
    for budget in (None, 0):
        call = lambda cursor=None: service.find_security_in_file(  # noqa: E731
            ScanControl(max_seconds=budget), A, "busy.csv", "SEC", "ID", columns=["Seq"], max_rows=9, cursor=cursor, filters=filters)
        pages = [call()]
        while pages[-1]["next_cursor"]:
            pages.append(call(pages[-1]["next_cursor"]))
        assert [m["values"]["Seq"] for p in pages for m in p["matches"]] == expected
        assert pages[-1]["more_matches"] == "none" and pages[-1]["identifier_matches_failing_filters"]["count"] == 1000 - len(expected)
    cursor = pages[0]["next_cursor"]
    again = lambda f: service.find_security_in_file(ScanControl(), A, "busy.csv", "SEC", "ID", cursor=cursor, filters=f)  # noqa: E731
    # The same predicates in another order, or naming the column another way, are the same search.
    assert again(list(reversed(filters)))["matches"][0]["values"]["Seq"] == expected[9]
    assert again([{"column": {"position": 4}, "value": "10.5"}, {"column": "#2", "value": "P3"}])["search"]["resumed_from_cursor"]
    for changed in ([{"column": "Portfolio", "value": "P4"}, filters[1]], [filters[0]], None,
                    [{"column": "Seq", "value": "P3"}, filters[1]], filters + [{"column": "Seq", "value": "3"}]):
        assert failure(lambda: again(changed)).kind == "invalid_cursor"
    counted = service.count_matching_rows(ScanControl(max_seconds=0), A, "busy.csv", "SEC", "ID", filters=filters)
    while counted["next_cursor"]:
        assert counted["count"] is None
        counted = service.count_matching_rows(ScanControl(max_seconds=0), A, "busy.csv", "SEC", "ID",
                                              filters=filters, cursor=counted["next_cursor"])
    assert counted["count"] == len(expected) and counted["count_is_exact"]


def test_a_filtered_comparison_succeeds_where_the_unfiltered_one_overflows(env, service, busy):
    mapping = {"ID": "sec", "Seq": "n", "Price": "px"}
    plain = compare(service, "busy.csv", "busy_b.csv", "SEC", column_mapping=mapping, match_keys=["Seq"])
    assert plain["comparison_status"] == "inconclusive" and plain["presence"]["file_a"]["stop_reason"] == "too_many_matches"
    assert plain["presence"]["file_a"]["matching_records_found"] == 25  # the cap, not the 1,000 records there are
    # Each side is filtered on its own column and its own value; only two records per file are ever collected.
    out = compare(service, "busy.csv", "busy_b.csv", "SEC", column_mapping=mapping, match_keys=["Seq"],
                  filters_a=[{"column": "Portfolio", "value": "PX"}], filters_b=[{"column": "pf", "value": "TARGET"}])
    assert out["comparison_status"] == "compared" and out["record_pairs_compared"] == 2
    assert out["presence"]["file_a"]["matching_records_found"] == 2 and out["presence"]["file_a"]["identifier_matches_failing_filters"] == 998
    assert out["presence"]["file_b"]["matching_records_found"] == 2 and out["presence"]["file_b"]["identifier_matches_failing_filters"] == 1
    assert out["search"]["filters_a"] == [{"column": "Portfolio", "position": 2, "value": "PX"}]
    assert out["search"]["filters_b"] == [{"column": "pf", "position": 2, "value": "TARGET"}]
    changed = {c["key"]["Seq"]: c["changed"] for c in out["comparisons"]}
    assert changed["400"] == [] and changed["900"][0]["value_b"] == "11"
    # A side's filters are evaluated against that side's columns: A's column name is not carried over to B.
    assert failure(lambda: compare(service, "busy.csv", "busy_b.csv", "SEC", column_mapping=mapping, match_keys=["Seq"],
                                   filters_b=[{"column": "Portfolio", "value": "PX"}])).kind == "column_not_found"


def test_absent_within_a_filtered_comparison_is_not_absent_from_the_file(env, service, busy):
    mapping = {"ID": "sec", "Seq": "n", "Price": "px"}
    out = compare(service, "busy.csv", "busy_b.csv", "SEC", column_mapping=mapping, match_keys=["Seq"],
                  filters_a=[{"column": "Portfolio", "value": "PX"}], filters_b=[{"column": "pf", "value": "NOBODY"}])
    assert out["comparison_status"] == "present_in_one_file_only"
    assert out["presence"]["file_b"]["outcome"] == "no_match_within_filters" and out["presence"]["file_b"]["identifier_matches_failing_filters"] == 3
    assert "not the same as being absent from that file" in out["explanation"] and "not absent from the file" in out["search"]["scope"]
    neither = compare(service, "busy.csv", "busy_b.csv", "SEC", column_mapping=mapping, match_keys=["Seq"],
                      filters_a=[{"column": "Portfolio", "value": "NOBODY"}], filters_b=[{"column": "pf", "value": "NOBODY"}])
    assert neither["comparison_status"] == "absent_from_both" and "within its filters" in neither["explanation"]
    # Unfiltered sides keep the plain meaning.
    one = compare(service, "busy_b.csv", "busy.csv", "OTHERSEC", "sec", identifier_column_b="ID", compare_columns=["sec"],
                  column_mapping={"sec": "ID"})
    assert one["presence"]["file_a"]["outcome"] == "confirmed_absent" and one["explanation"] == "Complete scans: the security is in neither file."
