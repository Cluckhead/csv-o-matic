#!/usr/bin/env python3
"""Refresh the example responses embedded in a guide page (docs/guide.html by default).

The guide's examples are real output: this script generates the invented sample data, runs
each example call through the MCP server, and writes the responses into the guide between
its EXAMPLES markers. Run it after changing the server or the sample data.

    python scripts/build_guide.py                    # update docs/guide.html in place
    python scripts/build_guide.py --guide docs/field-guide.html   # update another page with the same markers
    python scripts/build_guide.py --fragment out.html   # also write the page without its <html> wrapper

A page can ask for extra examples with a comment such as <!--EXAMPLES:SET field-guide--> (see EXAMPLE_SETS).
docs/field-guide.html also carries a miniature of the server's rules in JavaScript, which its figures run:

    python scripts/build_guide.py --guide docs/field-guide.html --check-model   # needs node

runs that miniature and the real service over the same few thousand searches and comparisons of the sample
data and reports any difference. The page is not rewritten by --check-model.
"""

from __future__ import annotations

import argparse
import contextlib
import itertools
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from mcp.client import Client  # noqa: E402

import make_sample_data  # noqa: E402
from csv_o_matic.config import Config, Limits  # noqa: E402
from csv_o_matic.csvscan import ScanControl  # noqa: E402
from csv_o_matic.server import build_server  # noqa: E402
from csv_o_matic.service import Service, ToolFailure  # noqa: E402

GUIDE = ROOT / "docs" / "guide.html"
START, END = "<!--EXAMPLES:START-->", "<!--EXAMPLES:END-->"
SET_MARK = re.compile(r"<!--EXAMPLES:SET ([\w-]+)-->")
MODEL_START, MODEL_END = "/*MODEL:START*/", "/*MODEL:END*/"
COPIED_AT = 1794622200  # 2026-11-14 02:10 UTC: every sample file "was copied" then
SMALL_READS = 48  # chunk_bytes for examples marked "small_reads": a 234-byte file then takes four reads

VENDOR = "security_master/01_vendor_raw"
NORMALISED = "security_master/02_normalised"
GOLDEN = "security_master/04_golden"
CUSTODIAN = "positions/01_custodian_raw"
ENRICHED = "positions/02_enriched"
CLIENT = "positions/03a_client_report"
GOLD_1830 = "security_master_20261113_20261113T183000.csv"
GOLD_2115 = "security_master_20261113_20261113T211500.csv"
VENDOR_1802 = "vendor_ref_20261113_20261113T180200.csv"
VENDOR_2047 = "vendor_ref_20261113_20261113T204700.csv"
CUST_13 = "custodian_positions_20261113_20261113T200000.csv"
ENR_13 = "positions_enriched_20261113_20261113T200000.csv"
CLIENT_13 = "client_holdings_20261113_20261113T200000.csv"

# id -> (tool, arguments). "cursor_from" continues the named example with its next_cursor.
EXAMPLES: dict[str, tuple[str, dict]] = {
    "pm_list": ("get_process_map", {}),
    "pm_security_master": ("get_process_map", {"process": "security_master"}),
    "pm_eod": ("get_process_map", {"process": "eod_positions"}),
    "pm_step": ("get_process_map", {"process": "eod_positions", "step_id": "enrich"}),
    "folders": ("list_folders", {"process": "eod_positions"}),
    "files_golden": ("list_csv_files", {"folder": GOLDEN, "date_from": "2026-11-13", "date_to": "2026-11-13"}),
    "files_normalised": ("list_csv_files", {"folder": NORMALISED}),
    "files_vendor": ("list_csv_files", {"folder": VENDOR, "date_from": "2026-11-13", "date_to": "2026-11-13"}),
    "files_client": ("list_csv_files", {"folder": CLIENT, "date_from": "2026-11-13", "date_to": "2026-11-13"}),
    "headers": ("inspect_csv_headers", {"folder": VENDOR, "filename": VENDOR_1802}),
    "headers_sample": ("inspect_csv_headers", {"folder": VENDOR, "filename": VENDOR_1802, "sample_rows": 2,
                                               "sample_columns": ["ISIN", "SEDOL", "SecurityName", "Status"]}),
    "column_search": ("search_column_names", {"folder": ENRICHED, "filename": ENR_13, "term": "price"}),
    "find_match": ("find_security_in_file", {"folder": GOLDEN, "filename": GOLD_2115, "identifier": "0417382",
                                             "identifier_column": "sedol"}),
    "find_lost_zero": ("find_security_in_file", {"folder": GOLDEN, "filename": GOLD_2115, "identifier": "417382",
                                                 "identifier_column": "sedol"}),
    "find_columns": ("find_security_in_file", {"folder": CUSTODIAN, "filename": CUST_13, "identifier": "PF002",
                                               "identifier_column": "PortfolioCode", "columns": ["SecurityId", "Quantity"]}),
    "find_multiline": ("find_security_in_file", {"folder": VENDOR, "filename": VENDOR_1802, "identifier": "XS2048815523",
                                                 "identifier_column": "ISIN", "columns": ["SecurityName", "ClosePrice", "MaturityDate"]}),
    "find_absent": ("find_security_in_file", {"folder": GOLDEN, "filename": GOLD_1830, "identifier": "GB00BPNDL008",
                                              "identifier_column": "isin"}),
    "across": ("search_across_columns", {"folder": CUSTODIAN, "filename": CUST_13, "identifier": "417382"}),
    "across_name": ("search_across_columns", {"folder": GOLDEN, "filename": GOLD_2115, "identifier": "Halvorsen"}),
    "across_both": ("search_across_columns", {"folder": ENRICHED, "filename": ENR_13, "identifier": "0417382",
                                              "columns": ["portfolio_code", "name"]}),
    "count": ("count_matching_rows", {"folder": VENDOR, "filename": VENDOR_1802, "identifier": "XLON",
                                      "identifier_column": "ExchangeCode"}),
    "count_zero": ("count_matching_rows", {"folder": ENRICHED, "filename": ENR_13, "identifier": "PF003",
                                           "identifier_column": "portfolio_code"}),
    "truncated": ("find_security_in_file", {"folder": VENDOR, "filename": VENDOR_1802, "identifier": "XLON",
                                            "identifier_column": "ExchangeCode", "columns": ["ISIN", "SecurityName"], "max_rows": 3}),
    "page1": ("find_security_in_file", {"folder": CUSTODIAN, "filename": CUST_13, "identifier": "PF001",
                                        "identifier_column": "PortfolioCode", "max_rows": 2}),
    "page2": ("find_security_in_file", {"folder": CUSTODIAN, "filename": CUST_13, "identifier": "PF001",
                                        "identifier_column": "PortfolioCode", "max_rows": 2, "cursor_from": "page1"}),
    "time_limit": ("count_matching_rows", {"folder": VENDOR, "filename": VENDOR_1802, "identifier": "XLON",
                                           "identifier_column": "ExchangeCode", "max_scan_seconds": 1e-9}),
    "time_limit_resume": ("count_matching_rows", {"folder": VENDOR, "filename": VENDOR_1802, "identifier": "XLON",
                                                  "identifier_column": "ExchangeCode", "cursor_from": "time_limit"}),
    "compare_runs": ("compare_security_records", {"folder_a": GOLDEN, "filename_a": GOLD_1830, "folder_b": GOLDEN,
                                                  "filename_b": GOLD_2115, "identifier": "US5128471093",
                                                  "identifier_column_a": "isin"}),
    "compare_ambiguous": ("compare_security_records", {
        "folder_a": CUSTODIAN, "filename_a": CUST_13, "folder_b": ENRICHED, "filename_b": ENR_13,
        "identifier": "0417382", "identifier_column_a": "SecurityId", "identifier_column_b": "sedol",
        "compare_columns": ["PortfolioCode", "Quantity"],
        "column_mapping": {"PortfolioCode": "portfolio_code", "Quantity": "quantity"}}),
    "compare_keys": ("compare_security_records", {
        "folder_a": CUSTODIAN, "filename_a": CUST_13, "folder_b": ENRICHED, "filename_b": ENR_13,
        "identifier": "0417382", "identifier_column_a": "SecurityId", "identifier_column_b": "sedol",
        "compare_columns": ["PortfolioCode", "Quantity"],
        "column_mapping": {"PortfolioCode": "portfolio_code", "Quantity": "quantity"},
        "match_keys": ["PortfolioCode"]}),
    "compare_one_side": ("compare_security_records", {
        "folder_a": VENDOR, "filename_a": VENDOR_2047, "folder_b": NORMALISED,
        "filename_b": "sm_normalised_20261113_20261113T211500.csv", "identifier": "GB0006291573",
        "identifier_column_a": "ISIN", "column_mapping": {"ISIN": "isin"},
        "compare_columns": ["ISIN"]}),
    "find_wrong_column": ("find_security_in_file", {"folder": CUSTODIAN, "filename": CUST_13, "identifier": "PF003",
                                                     "identifier_column": "SecurityId"}),
    "err_column": ("find_security_in_file", {"folder": GOLDEN, "filename": GOLD_2115, "identifier": "0417382",
                                             "identifier_column": "SEDOL"}),
    "err_file": ("count_matching_rows", {"folder": GOLDEN, "filename": "security_master_20261113_20261113T211600.csv",
                                         "identifier": "0417382", "identifier_column": "sedol"}),
    "err_folder": ("list_csv_files", {"folder": "security_master/05_archive"}),
    "bad_utf16": ("search_across_columns", {"folder": CUSTODIAN, "filename": "custodian_positions_20261114_20261114T200000.csv",
                                            "identifier": "0417382"}),
    "bad_quote": ("find_security_in_file", {"folder": CUSTODIAN, "filename": "custodian_positions_20261115_20261115T200000.csv",
                                            "identifier": "0417382", "identifier_column": "SecurityId"}),
    "bad_delimiter": ("search_across_columns", {"folder": CUSTODIAN, "filename": "custodian_positions_20261116_20261116T200000.csv",
                                                "identifier": "0417382"}),
    # Extra calls used only by the two worked investigations.
    "inv_client_pf003": ("search_across_columns", {"folder": CLIENT, "filename": CLIENT_13, "identifier": "PF003"}),
    "inv_custodian_pf003": ("search_across_columns", {"folder": CUSTODIAN, "filename": CUST_13, "identifier": "PF003"}),
    "inv_gold_1830_lost_zero": ("find_security_in_file", {"folder": GOLDEN, "filename": GOLD_1830, "identifier": "417382",
                                                          "identifier_column": "sedol", "columns": ["isin", "sedol", "name"]}),
    "inv_gold_1830_match": ("find_security_in_file", {"folder": GOLDEN, "filename": GOLD_1830, "identifier": "0417382",
                                                      "identifier_column": "sedol", "columns": ["isin", "sedol", "name"]}),
    "inv_enriched_larkspur": ("find_security_in_file", {"folder": ENRICHED, "filename": ENR_13, "identifier": "US5128471093",
                                                        "identifier_column": "isin",
                                                        "columns": ["portfolio_code", "quantity", "price", "market_value"]}),
    "inv_vendor_1802_larkspur": ("find_security_in_file", {"folder": VENDOR, "filename": VENDOR_1802, "identifier": "US5128471093",
                                                           "identifier_column": "ISIN", "columns": ["SecurityName", "ClosePrice"]}),
    "inv_vendor_2047_larkspur": ("find_security_in_file", {"folder": VENDOR, "filename": VENDOR_2047, "identifier": "US5128471093",
                                                           "identifier_column": "ISIN", "columns": ["SecurityName", "ClosePrice"]}),
}

BAD_UTF16 = "custodian_positions_20261114_20261114T200000.csv"
BAD_QUOTE = "custodian_positions_20261115_20261115T200000.csv"
BAD_DELIMITER = "custodian_positions_20261116_20261116T200000.csv"
_PENDLE_CHAIN = {"folder": CUSTODIAN, "filename": CUST_13, "identifier": "0058213", "identifier_column": "SecurityId",
                 "max_scan_seconds": 1e-9, "small_reads": True}
_STALL = {**_PENDLE_CHAIN, "filename": BAD_QUOTE}
_POSITIONS = {"folder_a": CUSTODIAN, "filename_a": CUST_13, "folder_b": ENRICHED, "filename_b": ENR_13,
              "identifier_column_a": "SecurityId", "identifier_column_b": "sedol",
              "compare_columns": ["PortfolioCode", "Quantity"],
              "column_mapping": {"PortfolioCode": "portfolio_code", "Quantity": "quantity", "SecurityId": "sedol"}}

# Extra examples for pages that ask for them with <!--EXAMPLES:SET name-->. "small_reads" runs the call on a
# second server whose reads are SMALL_READS bytes, so that the tiny sample file needs several reads.
EXAMPLE_SETS: dict[str, dict[str, tuple[str, dict]]] = {
    "field-guide": {
        # The one small file the page's figures use, searched on the real server.
        "mini_find": ("find_security_in_file", {"folder": CUSTODIAN, "filename": CUST_13, "identifier": "0417382",
                                                "identifier_column": "SecurityId"}),
        "mini_lost_zero": ("find_security_in_file", {"folder": CUSTODIAN, "filename": CUST_13, "identifier": "417382",
                                                     "identifier_column": "SecurityId"}),
        "mini_lower_case": ("find_security_in_file", {"folder": CUSTODIAN, "filename": CUST_13, "identifier": "b7k2mn4",
                                                      "identifier_column": "SecurityId"}),
        "mini_across_pendle": ("search_across_columns", {"folder": CUSTODIAN, "filename": CUST_13, "identifier": "0058213"}),
        # A real chain of cursors: four reads, and a time budget that only ever allows one of them per call.
        "chain_1": ("find_security_in_file", _PENDLE_CHAIN),
        "chain_2": ("find_security_in_file", {**_PENDLE_CHAIN, "cursor_from": "chain_1"}),
        "chain_3": ("find_security_in_file", {**_PENDLE_CHAIN, "cursor_from": "chain_2"}),
        "chain_4": ("find_security_in_file", {**_PENDLE_CHAIN, "cursor_from": "chain_3"}),
        # The same chain on the file with an unclosed quote: the record is longer than a read, and the chain stalls.
        "stall_1": ("find_security_in_file", _STALL),
        "stall_2": ("find_security_in_file", {**_STALL, "cursor_from": "stall_1"}),
        "stall_3": ("find_security_in_file", {**_STALL, "cursor_from": "stall_2"}),
        # More comparisons between the custodian file and the enriched positions.
        "compare_bad_key": ("compare_security_records", {**_POSITIONS, "identifier": "0417382", "match_keys": ["SecurityId"]}),
        "compare_tidewater": ("compare_security_records", {**_POSITIONS, "identifier": "B7K2MN4", "match_keys": ["PortfolioCode"]}),
        "compare_pendle": ("compare_security_records", {**_POSITIONS, "identifier": "0058213"}),
        "compare_larkspur": ("compare_security_records", {**_POSITIONS, "identifier": "2619045"}),
        # The three broken files, searched for a security that sits in their last row.
        "bad_quote_pendle": ("search_across_columns", {"folder": CUSTODIAN, "filename": BAD_QUOTE, "identifier": "0058213"}),
        "bad_delimiter_pendle": ("search_across_columns", {"folder": CUSTODIAN, "filename": BAD_DELIMITER, "identifier": "0058213"}),
        "bad_utf16_pendle": ("search_across_columns", {"folder": CUSTODIAN, "filename": BAD_UTF16, "identifier": "0058213"}),
        # Extra calls for the worked cases.
        "pm_step_normalise": ("get_process_map", {"process": "security_master", "step_id": "normalise"}),
        "inv_vendor_corbel": ("find_security_in_file", {"folder": VENDOR, "filename": VENDOR_2047, "identifier": "GB0006291573",
                                                        "identifier_column": "ISIN", "columns": ["SecurityName", "Status"]}),
        "inv_enriched_pendle": ("find_security_in_file", {"folder": ENRICHED, "filename": ENR_13, "identifier": "0058213",
                                                          "identifier_column": "sedol"}),
        "inv_gold_2115_pendle": ("find_security_in_file", {"folder": GOLDEN, "filename": GOLD_2115, "identifier": "0058213",
                                                           "identifier_column": "sedol", "columns": ["isin", "sedol", "name"]}),
    },
}


def add_broken_files(data: Path) -> None:
    """Three files that are wrong in ways real exports are wrong, for the troubleshooting section."""
    folder = data / CUSTODIAN
    good = (folder / CUST_13).read_text(encoding="utf-8")
    (folder / "custodian_positions_20261114_20261114T200000.csv").write_bytes(good.encode("utf-16"))
    lines = good.splitlines()
    lines[3] = 'PF001,"2619045,40,USD'  # a quote that is opened and never closed
    (folder / "custodian_positions_20261115_20261115T200000.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (folder / "custodian_positions_20261116_20261116T200000.csv").write_text(good.replace(",", ";"), encoding="utf-8")


async def collect(data: Path, examples: dict[str, tuple[str, dict]]) -> dict[str, dict]:
    def config(chunk_bytes: int) -> Config:
        return Config(data_root=data, process_maps_dir=ROOT / "process_maps", limits=Limits(chunk_bytes=chunk_bytes))

    out: dict[str, dict] = {}
    async with contextlib.AsyncExitStack() as stack:
        client = await stack.enter_async_context(Client(build_server(config(8192))))
        small = None  # a second server with tiny reads, started only if an example asks for it
        for name, (tool, arguments) in examples.items():
            arguments = dict(arguments)
            source = arguments.pop("cursor_from", None)
            target = client
            if arguments.pop("small_reads", False):
                small = small or await stack.enter_async_context(Client(build_server(config(SMALL_READS))))
                target = small
            if source:
                arguments["cursor"] = out[source]["response"]["next_cursor"]
            result = await target.call_tool(tool, arguments)
            out[name] = {"tool": tool, "arguments": arguments, "response": json.loads(result.content[0].text),
                         "is_error": bool(result.is_error)}
    return out


# --------------------------------------------------------------------------- --check-model
# The field guide's figures run a JavaScript miniature of the server's rules (between the MODEL markers).
# This runs the miniature under node and the real service over the same cases and compares the answers.

_NODE_RUNNER = r"""
const fs = require("fs");
const CSVM = new Function(fs.readFileSync(process.argv[2], "utf8") + "; return CSVM;")();
const label = (c, i) => (i < c.columns.length ? c.columns[i] : "[extra field " + (i + 1) + "]");
const pick = (v) => (v === undefined ? null : v);
function call(c) {
  const h = c.hints, s = c.scan || {};
  return { outcome: c.outcome, explanation: c.explanation, error: c.error ? c.error.type : null,
    complete: pick(s.complete), stop_reason: pick(s.stop_reason), started: pick(s.started_at_byte), stopped: pick(s.stopped_at_byte),
    records_after_header: pick(s.records_after_header), matches_returned: pick(c.matches_returned),
    records: (c.matches || []).map((m) => m.record_number), matched: (c.matches || []).map((m) => m.matched.map((i) => label(c, i))),
    more_matches: pick(c.more_matches), partial: pick(c.results_are_partial), has_cursor: !!c.next_cursor,
    count: pick(c.count), count_is_exact: pick(c.count_is_exact), seen: pick(c.matches_seen_before_interruption),
    near: h ? [h.elsewhere, h.padded.length, h.longer.length, h.spanning.length] : [{}, 0, 0, 0], warnings: c.warnings || [] };
}
const out = JSON.parse(fs.readFileSync(process.argv[3], "utf8")).map((q) => {
  if (q.kind === "search") return CSVM.investigate(q, 12).calls.map(call);
  if (q.kind === "compare") {
    const r = CSVM.compareRecords(q), n = (x) => x.number;
    return { status: r.status, explanation: r.explanation, a: r.recsA.map(n), b: r.recsB.map(n),
      pairs: r.comparisons.map((c) => [c.a.number, c.b.number, c.changed.length, c.unchanged.length]),
      ambiguous: r.ambiguous_keys.length, only_a: r.unmatched_a.map(n), only_b: r.unmatched_b.map(n) };
  }
  return CSVM.fileFor(q.file, q.fault).text;
});
fs.writeFileSync(process.argv[4], JSON.stringify(out));
"""


class _ReadsClock(ScanControl):
    """A time budget counted in reads, so that an interrupted scan stops in the same place every time."""

    def __init__(self, reads: float):
        super().__init__(max_seconds=None)
        self.reads, self.checks = reads, 0

    def interruption(self) -> str | None:
        self.checks += 1
        return "time_limit" if self.checks > self.reads else None


def check_model(page: str, data: Path, work: Path) -> int:
    if MODEL_START not in page or MODEL_END not in page:
        print(f"The page has no {MODEL_START} ... {MODEL_END} block to check.")
        return 2
    files = {"none": CUST_13, "utf16": BAD_UTF16, "quote": BAD_QUOTE, "semicolon": BAD_DELIMITER}
    values = ["0417382", "417382", "PF001", "PF003", "0058213", "GB0004173829", "B7K2MN4", "b7k2mn4", "0629157",
              "SecurityId", "GBP", "USD"]
    columns = ["PortfolioCode", "SecurityId", "Quantity", "SettlementCcy", None]
    budgets = [0, 1, 2, 3, 4, 5, None]  # reads allowed per call; None is no limit
    base = {"kind": "search", "file": "custodian", "fault": "none", "count": False, "maxRows": None, "chunkBytes": SMALL_READS}
    cases: list[dict] = []
    for value, column, rows, reads in itertools.product(values, columns, [None, 1, 2, 3, 4, 5], budgets):
        cases.append({**base, "identifier": value, "column": column, "maxRows": rows, "reads": reads})
    for value, column, reads in itertools.product(values, columns[:4], budgets):
        cases.append({**base, "identifier": value, "column": column, "count": True, "reads": reads})
    for fault, value, column, count in itertools.product(["quote", "semicolon", "utf16"], values, columns, [False, True]):
        if not (count and column is None):
            cases.append({**base, "fault": fault, "identifier": value, "column": column, "count": count, "reads": None,
                          "chunkBytes": 8192})
    for value, column, reads in [("0058213", "SecurityId", r) for r in (0, 1, 2)] + [("PF001", "PortfolioCode", r) for r in (0, 1, 2)]:
        cases.append({**base, "identifier": value, "column": column, "reads": reads, "useCursor": False})
    keys = {"none": [], "PortfolioCode": [["PortfolioCode", "portfolio_code"]], "SecurityId": [["SecurityId", "sedol"]],
            "Quantity": [["Quantity", "quantity"]]}
    for value, key in itertools.product(["0417382", "2619045", "B7K2MN4", "0058213", "417382", "0629157", "BF8QT61"], keys):
        cases.append({"kind": "compare", "identifier": value, "columnA": "SecurityId", "columnB": "sedol",
                      "compare": [["PortfolioCode", "portfolio_code"], ["Quantity", "quantity"]], "keys": keys[key]})
    cases += [{"kind": "text", "file": "custodian", "fault": f} for f in ("none", "quote", "semicolon")]
    cases.append({"kind": "text", "file": "enriched", "fault": "none"})

    model = page[page.index(MODEL_START) + len(MODEL_START): page.index(MODEL_END)]
    for name, text in (("model.js", model), ("runner.js", _NODE_RUNNER), ("cases.json", json.dumps(cases))):
        (work / name).write_text(text, encoding="utf-8")
    try:
        subprocess.run(["node", str(work / "runner.js"), str(work / "model.js"), str(work / "cases.json"),
                        str(work / "out.json")], check=True)
    except FileNotFoundError:
        print("--check-model needs node on the PATH to run the page's JavaScript.")
        return 2
    miniature = json.loads((work / "out.json").read_text(encoding="utf-8"))

    def service(chunk_bytes: int) -> Service:
        return Service(Config(data_root=data, process_maps_dir=ROOT / "process_maps", limits=Limits(chunk_bytes=chunk_bytes)))

    services = {SMALL_READS: service(SMALL_READS), 8192: service(8192)}

    def guarded(fn) -> dict:
        try:
            return fn()
        except ToolFailure as failure:  # what server.py turns a failed search into
            return {"outcome": "inconclusive", "explanation": f"INCONCLUSIVE: no search was carried out. {failure.message}",
                    "error": {"type": failure.kind}}

    def search(q: dict) -> list[dict]:
        svc, name, calls, cursor = services[q["chunkBytes"]], files[q["fault"]], [], None
        for _ in range(12):
            clock = _ReadsClock(float("inf") if q["reads"] is None else q["reads"])
            if q["count"]:
                r = guarded(lambda: svc.count_matching_rows(clock, CUSTODIAN, name, q["identifier"], q["column"], cursor=cursor))
            elif q["column"] is None:
                r = guarded(lambda: svc.search_across_columns(clock, CUSTODIAN, name, q["identifier"], max_rows=q["maxRows"],
                                                              cursor=cursor))
            else:
                r = guarded(lambda: svc.find_security_in_file(clock, CUSTODIAN, name, q["identifier"], q["column"],
                                                              max_rows=q["maxRows"], cursor=cursor))
            scan, near, found = r.get("scan") or {}, r.get("near_misses") or {}, r.get("matches", [])
            own = [r["search"]["column"]] if "column" in r.get("search", {}) else []
            calls.append({
                "outcome": r["outcome"], "explanation": r["explanation"], "error": (r.get("error") or {}).get("type"),
                "complete": scan.get("complete"), "stop_reason": scan.get("stop_reason"), "started": scan.get("started_at_byte"),
                "stopped": scan.get("stopped_at_byte"), "records_after_header": scan.get("records_after_header"),
                "matches_returned": r.get("matches_returned"), "records": [m["record_number"] for m in found],
                "matched": [m.get("matched_columns", own) for m in found], "more_matches": r.get("more_matches"),
                "partial": r.get("results_are_partial"), "has_cursor": bool(r.get("next_cursor")), "count": r.get("count"),
                "count_is_exact": r.get("count_is_exact"), "seen": r.get("matches_seen_before_interruption"),
                "near": [near.get("exact_value_in_other_columns", {}),
                         near.get("same_value_with_surrounding_whitespace", {}).get("count", 0),
                         near.get("identifier_inside_a_longer_value", {}).get("count", 0),
                         near.get("inside_a_multi_line_value_in_another_column", {}).get("count", 0)],
                "warnings": r.get("warnings", []),
            })
            if not r.get("next_cursor"):
                break
            cursor = r["next_cursor"] if q.get("useCursor", True) else None
        return calls

    def compare(q: dict) -> dict:
        r = services[8192].compare_security_records(
            _ReadsClock(float("inf")), **_POSITIONS, identifier=q["identifier"],
            match_keys=[k[0] for k in q["keys"]] or None)
        comparisons, ambiguous = r.get("comparisons", []), r.get("ambiguous_keys", [])

        def numbers(side: str) -> list[int]:
            listed = [x["record_number"] for x in r.get(f"records_in_file_{side}", [])]
            paired = {c[f"record_{side}"]["record_number"] for c in comparisons}
            alone = {x["record_number"] for x in r.get(f"unmatched_records_in_file_{side}", [])}
            return listed or sorted(paired | alone | {n for k in ambiguous for n in k[f"records_in_file_{side}"]})

        return {"status": r["comparison_status"], "explanation": r["explanation"], "a": numbers("a"), "b": numbers("b"),
                "pairs": [[c["record_a"]["record_number"], c["record_b"]["record_number"], c["changed_count"], c["unchanged_count"]]
                          for c in comparisons],
                "ambiguous": len(ambiguous),
                "only_a": [x["record_number"] for x in r.get("unmatched_records_in_file_a", [])],
                "only_b": [x["record_number"] for x in r.get("unmatched_records_in_file_b", [])]}

    differences = calls = 0
    for q, got in zip(cases, miniature):
        if q["kind"] == "search":
            want = search(q)
            calls += len(want)
        elif q["kind"] == "compare":
            want = compare(q)
        else:
            folder, name = (CUSTODIAN, files[q["fault"]]) if q["file"] == "custodian" else (ENRICHED, ENR_13)
            want = (data / folder / name).read_bytes().decode("ascii")
        if want != got:
            differences += 1
            if differences <= 3:
                print(f"DIFFERENT: {json.dumps(q)}\n  server:    {json.dumps(want)[:600]}\n  miniature: {json.dumps(got)[:600]}")
    print(f"{len(cases)} cases ({calls} search calls) run through the page's miniature and the real service: "
          f"{differences} difference(s)")
    return 1 if differences else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fragment", type=Path, help="Also write the page without its <html>/<head>/<body> wrapper")
    parser.add_argument("--dump", type=Path, help="Also write the examples as a JSON file")
    parser.add_argument("--guide", type=Path, default=GUIDE, help="Page to update (default: docs/guide.html)")
    parser.add_argument("--check-model", action="store_true",
                        help="Compare the page's JavaScript miniature with the real service instead of updating the page")
    args = parser.parse_args()
    guide = args.guide.resolve()
    page = guide.read_text(encoding="utf-8") if guide.exists() else ""
    wanted = dict(EXAMPLES)
    for name in SET_MARK.findall(page):
        if name not in EXAMPLE_SETS:
            sys.exit(f"{guide} asks for example set '{name}', which is not in EXAMPLE_SETS")
        wanted.update(EXAMPLE_SETS[name])

    with tempfile.TemporaryDirectory() as tmp:
        data = Path(tmp) / "sample_data"
        make_sample_data.build(data)
        add_broken_files(data)
        for path in data.rglob("*.csv"):
            os.utime(path, (COPIED_AT, COPIED_AT))
        if args.check_model:
            sys.exit(check_model(page, data, Path(tmp)))
        examples = anyio.run(collect, data, wanted)

    blob = json.dumps(examples, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    if args.dump:
        args.dump.write_text(json.dumps(examples, ensure_ascii=False, indent=1), encoding="utf-8")
    if page:
        if START not in page or END not in page:
            sys.exit(f"{guide} has no {START} ... {END} block")
        block = f'{START}\n<script type="application/json" id="examples">{blob}</script>\n{END}'
        page = page[: page.index(START)] + block + page[page.index(END) + len(END):]
        guide.write_text(page, encoding="utf-8")
        shown = guide.relative_to(ROOT) if guide.is_relative_to(ROOT) else guide
        print(f"{len(examples)} examples written into {shown} ({len(blob) / 1024:.0f} KB)")
        if args.fragment:
            parts = re.findall(r"<!--PAGE:(?:HEAD|BODY)-->(.*?)<!--/PAGE:(?:HEAD|BODY)-->", page, flags=re.S)
            args.fragment.write_text("\n".join(part.strip("\n") for part in parts) + "\n", encoding="utf-8")
            print(f"fragment written to {args.fragment}")


if __name__ == "__main__":
    main()
