from __future__ import annotations

import csv
import io

import anyio
import pytest
from mcp import Client

from conftest import make_service
from csv_o_matic.csvscan import iter_records
from csv_o_matic.output import Budget, size, some
from csv_o_matic.server import build_server
from csv_o_matic.service import Service, ToolFailure
from test_server import TRICKY, A, call, config


def test_budget_spends_only_what_fits():
    budget = Budget(10)
    assert budget.take(4) and budget.take(6) and budget.room == 0
    assert not budget.take(1) and budget.room == 0  # a refusal costs nothing
    assert budget.take(5, force=True) and budget.room == -5
    budget.give_back(8)
    assert budget.room == 3


def test_budget_leading_stops_at_the_first_item_that_does_not_fit():
    items = ["aa", "b" * 50, "cc"]
    assert Budget(20).leading(items) == ["aa"]  # "cc" would fit, but order is kept
    assert Budget(1000).leading(iter(items)) == items
    names = [f"column_{i}" for i in range(100)]
    for limit in (0, 2, 13, 14, 200, 5000):
        shown = some(names, limit)
        assert shown == names[:len(shown)] and (not shown or size(shown) <= limit)
    assert some(names, 5000) == names


def test_date_bounds_must_be_whole_iso_dates(env):
    service = make_service(env)
    assert service.list_csv_files(A, date_from="2026-11-13T09:30:00")["filters"]["date_from"] == "2026-11-13"
    assert service.list_csv_files(A, date_to=" 2026-11-13 ")["filters"]["date_to"] == "2026-11-13"
    for junk in ("2026-11-13xyz", "2026-11-13 or so", "2026-13-01", "tomorrow"):
        with pytest.raises(ToolFailure) as info:
            service.list_csv_files(A, date_from=junk)
        assert info.value.kind == "invalid_argument"


def test_scanning_never_lowers_the_csv_field_limit():
    before = csv.field_size_limit()
    try:
        csv.field_size_limit(10)
        long_field = b"a,b\n" + b"x" * 5000 + b",1\n"
        assert len(list(iter_records(io.BytesIO(long_field)))) == 2  # raised as far as this scan needs
        assert 10 < csv.field_size_limit() <= before
        csv.field_size_limit(2**30)
        list(iter_records(io.BytesIO(long_field)))
        assert csv.field_size_limit() == 2**30  # a higher limit set by the host process is kept
    finally:
        csv.field_size_limit(before)


def test_internal_errors_do_not_leak_exception_text(env):
    class Broken(Service):
        def search_column_names(self, *args, **kwargs):
            raise RuntimeError("/secret/path/on/the/server")

    async def main():
        cfg = config(env)
        async with Client(build_server(cfg, Broken(cfg))) as client:
            out, result = await call(client, "search_column_names", folder=A, filename=TRICKY, term="x")
            assert result.is_error and out["error"]["type"] == "internal_error"
            assert "RuntimeError" in out["error"]["message"] and "secret" not in str(out)
    anyio.run(main)


def test_failed_requests_echo_every_argument(env):
    async def main():
        async with Client(build_server(config(env))) as client:
            out, _ = await call(client, "find_security_in_file", folder=A, filename="missing.csv", identifier="1",
                                identifier_column="ID", cursor="abc", max_scan_seconds=5)
            assert out["request"]["cursor"] == "abc" and out["request"]["max_scan_seconds"] == 5
            out, _ = await call(client, "list_csv_files", folder="no/such/folder", descending=False)
            assert out["request"]["descending"] is False
    anyio.run(main)
