"""Typed MCP outputs: every tool publishes a schema, and every response - success, partial, error or
size backstop - conforms to it, in structured content and in the JSON text alike."""

from __future__ import annotations

import csv
import json

import anyio
import pytest
from jsonschema import Draft202012Validator
from mcp.client import Client

from csv_o_matic.config import Config, Limits
from csv_o_matic.models import RESULT_MODELS
from csv_o_matic.server import build_server, result_size
from csv_o_matic.service import Service

from conftest import digest

A = "stage/a"
B = "stage/b"
TRICKY = "a_20261112_20261112T180000.csv"


def config(env, **limits) -> Config:
    return Config(data_root=env[0], process_maps_dir=env[1], limits=Limits(**limits))


@pytest.fixture
def files(env):
    data = env[0]
    (data / A / "2026").mkdir()
    (data / A / "2026" / "sub.csv").write_text("ID,Portfolio\n00123,P1\n00123,P2\n")
    (data / A / "broken.csv").write_bytes(b'ID,Text\nA1,ok\nKEY,"' + b"never closed\n" * 8000)
    (data / A / "mis.csv").write_bytes("ID,Name\n".encode() + "Ä1,x\n".encode("cp1252") + b"S1,y\n")
    with open(data / A / "slowish.csv", "w", newline="") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_ALL)
        w.writerow(["ID", "Other"])
        for i in range(60_000):
            w.writerow([f"S{i}", "NEEDLE"])
    return data


async def checked_call(client, schemas, tool, limit=None, **args):
    """Call a tool and check everything that must hold for any response."""
    result = await client.call_tool(tool, args)
    assert len(result.content) == 1 and result.content[0].type == "text"
    structured = result.structured_content
    assert structured is not None, f"{tool}: structured content is missing"
    assert json.loads(result.content[0].text) == structured, f"{tool}: the two representations disagree"
    errors = [e.message for e in Draft202012Validator(schemas[tool]).iter_errors(structured)]
    assert not errors, f"{tool}: response does not match the advertised schema: {errors[:3]}"
    RESULT_MODELS[tool].model_validate(structured)
    assert structured["tool"] == tool
    if result.is_error:
        assert structured["error"]["type"] and structured["error"]["message"]
    if limit is not None:
        assert result_size(result) <= limit, f"{tool}: {result_size(result)} bytes exceeds {limit}"
    return structured, result


async def published(client) -> dict[str, dict]:
    return {tool.name: tool.output_schema for tool in (await client.list_tools()).tools}


def test_all_nine_tools_publish_a_specific_output_schema(env):
    async def main():
        async with Client(build_server(config(env))) as client:
            schemas = await published(client)
        assert set(schemas) == set(RESULT_MODELS) and len(schemas) == 9
        for name, schema in schemas.items():
            Draft202012Validator.check_schema(schema)
            assert schema["type"] == "object" and schema["additionalProperties"] is False, name  # not "any object"
            assert {"tool", "error"} <= set(schema["properties"]) and len(schema["properties"]) >= 8, name
            assert schema == RESULT_MODELS[name].model_json_schema(), name
        search = schemas["find_security_in_file"]
        assert set(search["properties"]["outcome"]["anyOf"][0]["enum"]) == {
            "match_found", "confirmed_absent", "no_match_within_filters", "inconclusive"}
        count = schemas["count_matching_rows"]["properties"]
        assert {"type": "null"} in count["count"]["anyOf"] and {"type": "integer"} in count["count"]["anyOf"]
        assert "count_is_exact" in count and "next_cursor" in count and "scan" in count
        status = schemas["compare_security_records"]["properties"]["comparison_status"]["anyOf"][0]["enum"]
        assert "no_comparable_columns" in status and "ambiguous_alignment" in status
    anyio.run(main)


def test_successful_partial_and_failed_responses_all_conform(env, files):
    async def main():
        before = digest(env[0])
        async with Client(build_server(config(env, chunk_bytes=1024, max_record_bytes=65536))) as client:
            schemas = await published(client)
            call = lambda tool, **args: checked_call(client, schemas, tool, **args)  # noqa: E731

            # Complete, successful operations on every tool.
            for tool, args in [
                ("get_process_map", {}), ("get_process_map", {"process": "demo"}),
                ("get_process_map", {"process": "demo", "step_id": "second"}),
                ("list_folders", {}), ("list_folders", {"folder": A}),
                ("list_csv_files", {"folder": A}), ("list_csv_files", {"folder": "stage/a/2026"}),
                ("list_csv_files", {"folder": A, "date_from": "2026-11-13"}),
                ("inspect_csv_headers", {"folder": A, "filename": TRICKY, "sample_rows": 3}),
                ("search_column_names", {"folder": A, "filename": "wide.csv", "term": "px"}),
                ("search_across_columns", {"folder": A, "filename": TRICKY, "identifier": "00123"}),
                ("compare_security_records", {"folder_a": B, "filename_a": "left.csv", "folder_b": B, "filename_b": "right.csv",
                                              "identifier": "00123", "identifier_column_a": "ID", "match_keys": ["Portfolio"],
                                              "column_mapping": {"ID": "sec_id", "Portfolio": "pf", "Price": "px"}}),
            ]:
                out, result = await call(tool, **args)
                assert not result.is_error and "error" not in out, (tool, out.get("error"))

            found, result = await call("find_security_in_file", folder="stage/a/2026", filename="sub.csv", identifier="00123",
                                       identifier_column={"position": 1}, filters=[{"column": "Portfolio", "value": "P2"}])
            assert found["outcome"] == "match_found" and found["matches_returned"] == 1 and not result.is_error
            assert found["search"]["filters"] == [{"column": "Portfolio", "position": 2, "value": "P2"}]

            # Confirmed absence, and absence within filters.
            absent, _ = await call("find_security_in_file", folder=A, filename=TRICKY, identifier="NOPE", identifier_column="ID")
            assert absent["outcome"] == "confirmed_absent" and absent["next_cursor"] is None
            scoped, _ = await call("count_matching_rows", folder="stage/a/2026", filename="sub.csv", identifier="00123",
                                   identifier_column="ID", filters=[{"column": "Portfolio", "value": "P9"}])
            assert scoped["outcome"] == "no_match_within_filters" and scoped["count"] == 0 and scoped["count_is_exact"]

            # Partial evidence is a successful operation, not an execution error: isError stays false.
            page, result = await call("find_security_in_file", folder=A, filename="many.csv", identifier="DUP",
                                      identifier_column="ID", max_rows=4)
            assert page["more_matches"] == "yes" and page["results_are_partial"] and page["next_cursor"] and not result.is_error
            timed, result = await call("count_matching_rows", folder=A, filename="slowish.csv", identifier="NEEDLE",
                                       identifier_column="Other", max_scan_seconds=0.001)
            assert timed["count"] is None and timed["count_is_exact"] is False and not result.is_error
            assert timed["scan"]["stop_reason"] == "time_limit" and timed["next_cursor"]
            unreliable, result = await call("find_security_in_file", folder=A, filename="mis.csv", identifier="Ä1", identifier_column="ID")
            assert unreliable["outcome"] == "inconclusive" and unreliable["scan"]["complete"] and not result.is_error

            # A malformed file: the scan ran and failed part-way. The evidence so far is returned with the error.
            broken, result = await call("count_matching_rows", folder=A, filename="broken.csv", identifier="KEY", identifier_column="ID")
            assert broken["error"]["type"] == "parse_error" and broken["count"] is None and broken["scan"]["stop_reason"] == "error"
            assert broken["outcome"] == "inconclusive" and not result.is_error

            # Requests that cannot be carried out at all: MCP isError, and never a conclusion.
            for tool, args, kind in [
                ("find_security_in_file", dict(folder=A, filename=TRICKY, identifier="1", identifier_column="ID",
                                               filters=[{"column": "Nope", "value": "x"}]), "column_not_found"),
                ("find_security_in_file", dict(folder=A, filename=TRICKY, identifier="1", identifier_column="ID",
                                               filters=[{"column": "Qty", "value": "1"}, {"column": {"position": 5}, "value": "2"}]),
                 "invalid_argument"),
                ("count_matching_rows", dict(folder=A, filename="many.csv", identifier="DUP", identifier_column="ID",
                                             cursor=page["next_cursor"]), "invalid_cursor"),
                ("find_security_in_file", dict(folder=A, filename="many.csv", identifier="DUP", identifier_column="ID",
                                               cursor="bm90IGEgY3Vyc29y"), "invalid_cursor"),
                ("find_security_in_file", dict(folder=A, filename="many.csv", identifier="DUP", identifier_column="ID",
                                               cursor=page["next_cursor"], filters=[{"column": "Seq", "value": "3"}]), "invalid_cursor"),
                ("search_across_columns", dict(folder="/etc", filename="passwd.csv", identifier="root"), "folder_not_found"),
                ("compare_security_records", dict(folder_a=B, filename_a="left.csv", folder_b=B, filename_b="right.csv",
                                                  identifier="00123", identifier_column_a="ID", compare_columns=["Qty"],
                                                  column_mapping={"ID": "sec_id", "Price": "nope"}), "column_not_found"),
                ("inspect_csv_headers", dict(folder=A, filename="missing.csv"), "file_not_found"),
                ("search_column_names", dict(folder="stage/a/../../x", filename="f.csv", term="a"), "folder_not_found"),
                ("list_folders", dict(folder="/"), "folder_not_found"),
                ("list_csv_files", dict(folder=A, date_from="yesterday"), "invalid_argument"),
                ("get_process_map", dict(process="nope"), "process_not_found"),
            ]:
                out, result = await call(tool, **args)
                assert result.is_error and out["error"]["type"] == kind, (tool, out)
                assert out["request"], tool
                if "outcome" in out:
                    assert out["outcome"] == "inconclusive" and "no search was carried out" in out["explanation"]
                assert "count" not in out and "matches" not in out and "comparison_status" not in out
        assert digest(env[0]) == before
    anyio.run(main)


def test_the_whole_result_stays_within_the_output_limit(env, files):
    async def main():
        limit = 16000
        async with Client(build_server(config(env, max_output_bytes=limit))) as client:
            schemas = await published(client)
            awkward = env[0] / A / "escapes.csv"  # quotes and backslashes cost extra inside the JSON text block
            with open(awkward, "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["ID"] + [f'c"{i}\\' for i in range(40)])
                for _ in range(60):
                    w.writerow(["E1"] + ['"\\"\t\u00e9\u20ac' * 30] * 40)
            for tool, args in [
                ("get_process_map", {"process": "demo"}),
                ("list_folders", {}),
                ("list_csv_files", {"folder": A}),
                ("inspect_csv_headers", {"folder": A, "filename": "wide.csv", "sample_rows": 5}),
                ("inspect_csv_headers", {"folder": A, "filename": "escapes.csv", "sample_rows": 5}),
                ("search_column_names", {"folder": A, "filename": "wide.csv", "term": "col"}),
                ("find_security_in_file", {"folder": A, "filename": "wide.csv", "identifier": "W0001", "identifier_column": "ID"}),
                ("find_security_in_file", {"folder": A, "filename": "escapes.csv", "identifier": "E1", "identifier_column": "ID"}),
                ("search_across_columns", {"folder": A, "filename": "many.csv", "identifier": "DUP"}),
                ("search_across_columns", {"folder": A, "filename": "escapes.csv", "identifier": "E1"}),
                ("compare_security_records", {"folder_a": A, "filename_a": "wide.csv", "folder_b": A, "filename_b": "wide.csv",
                                              "identifier": "W0001", "identifier_column_a": "ID"}),
                ("compare_security_records", {"folder_a": A, "filename_a": "escapes.csv", "folder_b": A, "filename_b": "escapes.csv",
                                              "identifier": "E1", "identifier_column_a": "ID"}),
            ]:
                out, result = await checked_call(client, schemas, tool, limit=limit, **args)
                assert out.get("error", {}).get("type") != "response_too_large", tool
                assert result_size(result) > limit // 10, f"{tool}: the budget should be used, not wasted"
    anyio.run(main)


def test_rows_cut_for_size_are_returned_on_the_next_page_not_skipped(env, files):
    async def main():
        limit = 16000
        async with Client(build_server(config(env, max_output_bytes=limit, max_rows_per_response=500))) as client:
            schemas = await published(client)
            seen, cursor, pages = [], None, 0
            while True:
                args = {"folder": A, "filename": "many.csv", "identifier": "DUP", "identifier_column": "ID"}
                out, _ = await checked_call(client, schemas, "find_security_in_file", limit=limit,
                                            **({**args, "cursor": cursor} if cursor else args))
                seen += [m["record_number"] for m in out["matches"]]
                cursor, pages = out["next_cursor"], pages + 1
                if not cursor:
                    break
                assert out["scan"]["stop_reason"] == "output_size_limit" and out["more_matches"] == "yes"
            assert seen == [2 + 3 * i for i in range(100)] and pages > 1 and out["more_matches"] == "none"
    anyio.run(main)


class Oversized(Service):
    def count_matching_rows(self, control, *args, **kwargs):
        out = super().count_matching_rows(control, *args, **kwargs)
        out["warnings"] = ["x" * 1000] * 40  # a response the budgeting failed to bound
        return out

    def list_csv_files(self, *args, **kwargs):
        out = super().list_csv_files(*args, **kwargs)
        out["not_in_the_schema"] = True  # unknown, but everything promised is there: dropped, answer kept
        out["files"][0]["also_unknown"] = 1
        return out

    def search_column_names(self, *args, **kwargs):
        out = super().search_column_names(*args, **kwargs)
        out["matching_column_count"] = "two"  # a promised field with the wrong type: the answer is withheld
        return out


def test_the_size_backstop_and_schema_problems_are_handled_without_false_conclusions(env):
    async def main():
        limit = 16000
        cfg = config(env, max_output_bytes=limit)
        async with Client(build_server(cfg, Oversized(cfg))) as client:
            schemas = await published(client)
            out, result = await checked_call(client, schemas, "count_matching_rows", limit=limit, folder=A, filename="many.csv",
                                             identifier="DUP", identifier_column="ID")
            assert result.is_error and out["error"]["type"] == "response_too_large"
            # The count that was computed is withheld with everything else: a failed call must not look conclusive.
            assert out["outcome"] == "inconclusive" and "count" not in out and "count_is_exact" not in out
            assert set(out) == {"tool", "outcome", "explanation", "error"} and result_size(result) < 1500
            out, result = await checked_call(client, schemas, "list_csv_files", limit=limit, folder=A)
            assert not result.is_error and out.get("files"), out
            assert "not_in_the_schema" not in out
            assert "also_unknown" not in out["files"][0]
            out, result = await checked_call(client, schemas, "search_column_names", limit=limit, folder=A,
                                             filename=TRICKY, term="id")
            assert result.is_error and out["error"]["type"] == "internal_error" and "matching_columns" not in out
    anyio.run(main)


# ---------------------------------------------------------------- header inspection under supervision

class Watching(Service):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.controls, self.running, self.peak = [], 0, 0

    def inspect_csv_headers(self, *args, control=None, **kwargs):
        self.controls.append(control)
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            return super().inspect_csv_headers(*args, control=control, **kwargs)
        finally:
            self.running -= 1


@pytest.fixture
def blank_tail(env):
    path = env[0] / A / "blank.csv"
    path.write_bytes(b"ID,Name\n1,only\n" + b"\n" * 4_000_000)
    return path


def test_sampling_honours_the_time_limit_over_the_protocol(env, blank_tail):
    async def main():
        cfg = config(env)
        service = Watching(cfg)
        async with Client(build_server(cfg, service)) as client:
            schemas = await published(client)
            out, result = await checked_call(client, schemas, "inspect_csv_headers", folder=A, filename="blank.csv",
                                             sample_rows=2, max_scan_seconds=0.1)
            assert not result.is_error and out["sample_complete"] is False and out["sample_stop_reason"] == "time_limit"
            assert len(out["sample_rows"]) == 1 and out["sample_note"].startswith("PARTIAL SAMPLE")
            assert service.controls[0].bytes_done < blank_tail.stat().st_size
            quick, _ = await checked_call(client, schemas, "inspect_csv_headers", folder=A, filename=TRICKY, sample_rows=2)
            assert quick["sample_complete"] and quick["columns"][0] == "ID"  # ordinary inspections stay ordinary
    anyio.run(main)


def test_concurrent_sampling_calls_respect_the_scan_slot_limit(env, blank_tail):
    async def main():
        cfg = config(env, max_concurrent_scans=2, max_scan_seconds=0.15)
        service = Watching(cfg)
        async with Client(build_server(cfg, service)) as client:
            results = []

            async def one():
                result = await client.call_tool("inspect_csv_headers", {"folder": A, "filename": "blank.csv", "sample_rows": 2})
                results.append(result.structured_content["sample_stop_reason"])

            async with anyio.create_task_group() as tg:
                for _ in range(6):
                    tg.start_soon(one)
        # Six calls, never more than two reading at once; a queued call's wait did not eat its own time budget.
        assert service.peak == 2 and results == ["time_limit"] * 6
        assert all(0 < c.bytes_done for c in service.controls)
    anyio.run(main)


def test_cancelling_a_sampling_call_stops_the_read(env, blank_tail):
    async def main():
        cfg = config(env, max_concurrent_scans=1)
        service = Watching(cfg)
        async with Client(build_server(cfg, service)) as client:
            with anyio.move_on_after(0.2) as scope:
                await client.call_tool("inspect_csv_headers", {"folder": A, "filename": "blank.csv", "sample_rows": 2})
            assert scope.cancelled_caught
            with anyio.fail_after(3):
                while service.running:
                    await anyio.sleep(0.01)
            control = service.controls[0]
            assert control.stop.is_set() and control.stop_reason == "cancelled"
            assert 0 < control.bytes_done < blank_tail.stat().st_size
            result = await client.call_tool("search_column_names", {"folder": A, "filename": TRICKY, "term": "id"})
            assert result.structured_content["matching_column_count"] == 2  # the slot is free again
    anyio.run(main)


def test_progress_is_reported_while_sampling(env, blank_tail):
    async def main():
        updates = []

        async def on_progress(progress, total, message):
            updates.append((progress, total, message))

        async with Client(build_server(config(env, progress_interval_seconds=0.03, max_scan_seconds=0.5))) as client:
            await client.call_tool("inspect_csv_headers", {"folder": A, "filename": "blank.csv", "sample_rows": 2},
                                   progress_callback=on_progress)
        assert len(updates) >= 3 and any(m.startswith("sampling") for _, _, m in updates)
        done = [p for p, _, _ in updates]
        assert done == sorted(done) and done[-1] > done[0] and updates[-1][1] == blank_tail.stat().st_size
    anyio.run(main)


def test_published_schemas_state_what_a_response_without_an_error_contains(env):
    async def main():
        async with Client(build_server(config(env))) as client:
            schemas = await published(client)
        count = schemas["count_matching_rows"]
        assert count["if"] == {"not": {"required": ["error"]}}
        assert {"outcome", "count", "count_is_exact", "next_cursor", "scan"} <= set(count["then"]["required"])
        validator = Draft202012Validator(count)
        # Leaving out a promised field is a schema violation unless the response reports an error.
        assert not validator.is_valid({"tool": "count_matching_rows", "outcome": "confirmed_absent"})
        assert validator.is_valid({"tool": "count_matching_rows", "error": {"type": "x", "message": "y"}})
        assert all("then" in schemas[name] for name in RESULT_MODELS if name != "get_process_map")
    anyio.run(main)


def test_header_only_reads_do_not_wait_behind_long_scans(env, blank_tail, files):
    async def main():
        cfg = config(env, max_concurrent_scans=1)
        async with Client(build_server(cfg)) as client:
            async with anyio.create_task_group() as tg:
                tg.start_soon(client.call_tool, "count_matching_rows",
                              {"folder": A, "filename": "slowish.csv", "identifier": "NEEDLE", "identifier_column": "Other"})
                await anyio.sleep(0.05)  # the only slot is now taken
                with anyio.fail_after(1):
                    headers = await client.call_tool("inspect_csv_headers", {"folder": A, "filename": TRICKY})
                    names = await client.call_tool("search_column_names", {"folder": A, "filename": TRICKY, "term": "id"})
                assert headers.structured_content["columns"][0] == "ID" and names.structured_content["matching_column_count"] == 2
                # A sample may read much more, so it does wait for the slot.
                with anyio.move_on_after(0.2) as scope:
                    await client.call_tool("inspect_csv_headers", {"folder": A, "filename": "blank.csv", "sample_rows": 2})
                assert scope.cancelled_caught
                tg.cancel_scope.cancel()
    anyio.run(main)


def test_summary_text_mode_keeps_every_conclusion_and_the_full_structured_content(env, files):
    async def main():
        cfg = Config(data_root=env[0], process_maps_dir=env[1], limits=Limits(), text_content="summary")
        async with Client(build_server(cfg)) as client:
            schemas = await published(client)
            for tool, args in [
                ("find_security_in_file", {"folder": A, "filename": "many.csv", "identifier": "DUP", "identifier_column": "ID",
                                           "max_rows": 3, "filters": [{"column": "Text", "value": "x" * 50}]}),
                ("count_matching_rows", {"folder": A, "filename": "mis.csv", "identifier": "Ä1", "identifier_column": "ID"}),
                ("list_csv_files", {"folder": A}),
                ("search_across_columns", {"folder": "/etc", "filename": "x.csv", "identifier": "1"}),
            ]:
                result = await client.call_tool(tool, args)
                full = result.structured_content
                errors = [e.message for e in Draft202012Validator(schemas[tool]).iter_errors(full)]
                assert not errors
                text = json.loads(result.content[0].text)
                assert len(result.content[0].text) < len(json.dumps(full)) or tool == "search_across_columns"
                for key in ("outcome", "count", "count_is_exact", "more_matches", "results_are_partial", "next_cursor",
                            "warnings", "truncated", "error", "explanation"):
                    assert text.get(key) == full.get(key), (tool, key)
                if "filters" in full.get("search", {}):
                    assert text["search"]["filters"] == full["search"]["filters"]
                if "scan" in full:
                    assert text["scan"] == {k: full["scan"][k] for k in ("complete", "stop_reason")}
                assert "matches" not in text and "files" not in text and "structured content" in text["detail"]
    anyio.run(main)
