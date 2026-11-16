"""End-to-end over the MCP protocol, using the SDK's in-memory client."""

from __future__ import annotations

import csv
import json
import time

import anyio
import pytest
from mcp.client import Client

from conftest import check_schema, digest
from csv_o_matic.config import Config, Limits
from csv_o_matic.server import build_server
from csv_o_matic.service import Service

A = "stage/a"
TRICKY = "a_20261112_20261112T180000.csv"
TOOLS = {"get_process_map", "list_folders", "list_csv_files", "inspect_csv_headers", "search_column_names",
         "find_security_in_file", "search_across_columns", "count_matching_rows", "compare_security_records"}


def run(coro_fn):
    return anyio.run(coro_fn)


def config(env, **limits) -> Config:
    return Config(data_root=env[0], process_maps_dir=env[1], limits=Limits(**limits))


async def call(client, tool, **args):
    result = await client.call_tool(tool, args)
    assert len(result.content) == 1
    out = json.loads(result.content[0].text)
    # Every response, failures included, is also structured content: the same object, fitting the schema.
    assert result.structured_content == out
    check_schema(tool, out)
    return out, result


@pytest.fixture
def slow_file(env):
    """A file that is slow to scan: every record is quoted and contains the needle, so each one is parsed."""
    path = env[0] / A / "slow.csv"
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_ALL)
        w.writerow(["ID", "Other"] + [f"c{i}" for i in range(30)])
        row = ["x" * 8] * 30
        for i in range(250_000):
            w.writerow([f"S{i}", "NEEDLE"] + row)
    return path


def test_exactly_the_nine_read_only_tools(env):
    async def main():
        async with Client(build_server(config(env))) as client:
            tools = (await client.list_tools()).tools
            assert {t.name for t in tools} == TOOLS
            for tool in tools:
                assert tool.annotations.read_only_hint is True and tool.description
    run(main)


def test_investigation_flow(env):
    async def main():
        before = digest(env[0])
        async with Client(build_server(config(env))) as client:
            listing, _ = await call(client, "get_process_map")
            process = listing["processes"][0]["id"]
            full, _ = await call(client, "get_process_map", process=process)
            assert full["process"]["steps"][0]["downstream"] == ["second", "third"]
            folders, _ = await call(client, "list_folders", process=process)
            folder = folders["folders"][0]["folder"]
            files, _ = await call(client, "list_csv_files", folder=folder, date_from="2026-11-12", date_to="2026-11-12")
            name = files["files"][0]["filename"]
            headers, _ = await call(client, "inspect_csv_headers", folder=folder, filename=name)
            assert headers["columns"][0] == "ID"
            cols, _ = await call(client, "search_column_names", folder=folder, filename=name, term="id")
            assert [c["column"] for c in cols["matching_columns"]] == ["ID", "AltID"]
            found, result = await call(client, "find_security_in_file", folder=folder, filename=name,
                                       identifier="00123", identifier_column="ID")
            assert found["outcome"] == "match_found" and found["matches_returned"] == 2 and not result.is_error
            across, _ = await call(client, "search_across_columns", folder=folder, filename=name, identifier="00123")
            assert across["matches"][2]["matched_columns"] == ["ID", "AltID"]
            count, _ = await call(client, "count_matching_rows", folder=folder, filename="many.csv",
                                  identifier="DUP", identifier_column="ID")
            assert count["count"] == 100
            compared, _ = await call(client, "compare_security_records", folder_a="stage/b", filename_a="left.csv",
                                     folder_b="stage/b", filename_b="right.csv", identifier="00777",
                                     identifier_column_a="ID", identifier_column_b="sec_id")
            assert compared["comparison_status"] == "compared"
        assert digest(env[0]) == before
    run(main)


def test_failures_are_explained_with_the_request_echoed(env):
    async def main():
        async with Client(build_server(config(env))) as client:
            out, result = await call(client, "find_security_in_file", folder=A, filename=TRICKY,
                                     identifier="00123", identifier_column="Isin")
            assert result.is_error and out["outcome"] == "inconclusive" and out["error"]["type"] == "column_not_found"
            assert out["request"]["filename"] == TRICKY and out["request"]["identifier"] == "00123"
            out, result = await call(client, "count_matching_rows", folder=A, filename="missing.csv",
                                     identifier="1", identifier_column="ID")
            assert result.is_error and out["error"]["type"] == "file_not_found" and out["outcome"] == "inconclusive"
            out, result = await call(client, "list_csv_files", folder="/etc")
            assert result.is_error and out["error"]["type"] == "folder_not_found"
            out, result = await call(client, "find_security_in_file", folder=A, filename=TRICKY, identifier="1",
                                     identifier_column="ID", max_scan_seconds=-1)
            assert result.is_error and out["error"]["type"] == "invalid_argument"
    run(main)


def test_every_response_respects_the_output_limit(env):
    async def main():
        limit = 8000
        async with Client(build_server(config(env, max_output_bytes=limit))) as client:
            for tool, args in [
                ("get_process_map", {"process": "demo"}),
                ("list_folders", {}),
                ("list_csv_files", {"folder": A}),
                ("inspect_csv_headers", {"folder": A, "filename": "wide.csv", "sample_rows": 5}),
                ("search_column_names", {"folder": A, "filename": "wide.csv", "term": "col"}),
                ("find_security_in_file", {"folder": A, "filename": "wide.csv", "identifier": "W0001", "identifier_column": "ID"}),
                ("search_across_columns", {"folder": A, "filename": "many.csv", "identifier": "DUP"}),
                ("compare_security_records", {"folder_a": A, "filename_a": "wide.csv", "folder_b": A, "filename_b": "wide.csv",
                                              "identifier": "W0001", "identifier_column_a": "ID"}),
            ]:
                result = await client.call_tool(tool, args)
                assert len(result.content[0].text.encode()) <= limit, tool
                assert "response_too_large" not in result.content[0].text, tool
    run(main)


def test_progress_updates_during_a_long_scan(env, slow_file):
    async def main():
        updates = []

        async def on_progress(progress, total, message):
            updates.append((progress, total, message))

        async with Client(build_server(config(env, progress_interval_seconds=0.05))) as client:
            result = await client.call_tool(
                "count_matching_rows", {"folder": A, "filename": "slow.csv", "identifier": "S249999", "identifier_column": "ID"},
                progress_callback=on_progress)
            out = json.loads(result.content[0].text)
        assert out["count"] == 1 and out["count_is_exact"]
        assert len(updates) >= 3
        done = [u[0] for u in updates]
        assert done == sorted(done) and done[-1] > done[0] and all(u[1] == slow_file.stat().st_size for u in updates)
        assert "MB" in updates[-1][2]
    run(main)


def test_time_limit_over_the_protocol_returns_partial_results_and_a_cursor(env, slow_file):
    async def main():
        async with Client(build_server(config(env))) as client:
            args = dict(folder=A, filename="slow.csv", identifier="NEEDLE", max_rows=3)
            started = time.monotonic()
            page, _ = await call(client, "search_across_columns", **args)
            assert page["more_matches"] == "yes" and page["scan"]["stop_reason"] == "row_limit"
            out, result = await call(client, "count_matching_rows", folder=A, filename="slow.csv",
                                     identifier="NEEDLE", identifier_column="Other", max_scan_seconds=0.3)
            assert time.monotonic() - started < 5
            assert not result.is_error and out["count"] is None and out["count_is_exact"] is False
            assert out["scan"]["stop_reason"] == "time_limit" and out["matches_seen_before_interruption"] > 0
            total, cursor = None, out["next_cursor"]
            while cursor:
                out, _ = await call(client, "count_matching_rows", folder=A, filename="slow.csv",
                                    identifier="NEEDLE", identifier_column="Other", cursor=cursor)
                total, cursor = out["count"], out["next_cursor"]
            assert total == 250_000
    run(main)


class RecordingService(Service):
    """Remembers the ScanControl of each scan so tests can watch what the worker thread did."""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.controls = []
        self.running = 0
        self.peak = 0

    def count_matching_rows(self, control, *args, **kwargs):
        self.controls.append(control)
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            return super().count_matching_rows(control, *args, **kwargs)
        finally:
            self.running -= 1


def test_cancellation_stops_the_scan_promptly(env, slow_file):
    async def main():
        cfg = config(env, max_concurrent_scans=1)
        service = RecordingService(cfg)
        args = {"folder": A, "filename": "slow.csv", "identifier": "NEEDLE", "identifier_column": "Other"}
        async with Client(build_server(cfg, service)) as client:
            with anyio.move_on_after(0.3) as scope:  # the client gives up and sends notifications/cancelled
                await client.call_tool("count_matching_rows", args)
            assert scope.cancelled_caught
            control = service.controls[0]
            with anyio.fail_after(2):  # the worker thread must notice within moments, not at end of file
                while service.running:
                    await anyio.sleep(0.01)
            assert control.stop.is_set() and control.stop_reason == "cancelled"
            assert 0 < control.bytes_done < slow_file.stat().st_size
            # The single scan slot is free again and the server still answers.
            out, _ = await call(client, "count_matching_rows", **{**args, "identifier": "S7", "identifier_column": "ID"})
            assert out["count"] == 1
    run(main)


def test_scan_concurrency_is_capped(env, slow_file):
    async def main():
        cfg = config(env, max_concurrent_scans=2)
        service = RecordingService(cfg)
        async with Client(build_server(cfg, service)) as client:
            results = []

            async def one(i):
                out, _ = await call(client, "count_matching_rows", folder=A, filename="slow.csv",
                                    identifier=f"S{i}", identifier_column="ID")
                results.append(out["count"])

            async with anyio.create_task_group() as tg:
                for i in range(5):
                    tg.start_soon(one, i)
        assert results == [1] * 5 and service.peak == 2
    run(main)


def test_cancelled_scans_keep_their_slot_until_they_stop(env, slow_file):
    """Cancel several scans in a row: a cancelled scan must not let another start while it is still running."""
    async def main():
        cfg = config(env, max_concurrent_scans=1)
        service = RecordingService(cfg)
        args = {"folder": A, "filename": "slow.csv", "identifier": "NEEDLE", "identifier_column": "Other"}
        async with Client(build_server(cfg, service)) as client:
            async def cancelled_call():
                with anyio.move_on_after(0.15):
                    await client.call_tool("count_matching_rows", args)

            async with anyio.create_task_group() as tg:
                for _ in range(4):
                    tg.start_soon(cancelled_call)
            with anyio.fail_after(3):
                while service.running:
                    await anyio.sleep(0.01)
            assert service.peak == 1
            assert all(c.stop.is_set() for c in service.controls)
            out, _ = await call(client, "count_matching_rows", **{**args, "identifier": "S7", "identifier_column": "ID"})
            assert out["count"] == 1
    run(main)


def test_time_limit_is_honoured_inside_a_chunk_full_of_candidates(env, slow_file):
    async def main():
        async with Client(build_server(config(env))) as client:  # default 4 MiB chunks, every record a candidate
            started = time.monotonic()
            out, _ = await call(client, "count_matching_rows", folder=A, filename="slow.csv",
                                identifier="NEEDLE", identifier_column="Other", max_scan_seconds=0.2)
            assert time.monotonic() - started < 0.6 and out["scan"]["stop_reason"] == "time_limit"
            assert out["count"] is None and out["next_cursor"]
    run(main)


def test_lone_surrogate_in_an_argument_gets_a_structured_answer(env):
    async def main():
        async with Client(build_server(config(env))) as client:
            out, _ = await call(client, "find_security_in_file", folder=A, filename=TRICKY,
                                identifier="\ud800", identifier_column="ID")
            assert out["tool"] == "find_security_in_file" and out["outcome"] in ("confirmed_absent", "inconclusive")
    run(main)


def test_queued_scans_report_that_they_are_waiting(env, slow_file):
    async def main():
        messages = []

        async def on_progress(progress, total, message):
            messages.append(message)

        cfg = config(env, max_concurrent_scans=1, progress_interval_seconds=0.05)
        args = {"folder": A, "filename": "slow.csv", "identifier": "NEEDLE", "identifier_column": "Other"}
        async with Client(build_server(cfg)) as client, anyio.create_task_group() as tg:
            tg.start_soon(client.call_tool, "count_matching_rows", args)
            await anyio.sleep(0.2)
            result = await client.call_tool("count_matching_rows", {**args, "identifier": "S7", "identifier_column": "ID"},
                                            progress_callback=on_progress)
        assert json.loads(result.content[0].text)["count"] == 1
        assert any(m.startswith("queued") for m in messages) and any("MB" in m for m in messages)
    run(main)
