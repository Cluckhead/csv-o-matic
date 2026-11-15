#!/usr/bin/env python3
"""Refresh the example responses embedded in docs/guide.html.

The guide's examples are real output: this script generates the invented sample data, runs
each example call through the MCP server, and writes the responses into the guide between
its EXAMPLES markers. Run it after changing the server or the sample data.

    python scripts/build_guide.py                    # update docs/guide.html in place
    python scripts/build_guide.py --fragment out.html   # also write the page without its <html> wrapper
"""

from __future__ import annotations

import argparse
import json
import os
import re
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
from csv_o_matic.server import build_server  # noqa: E402

GUIDE = ROOT / "docs" / "guide.html"
START, END = "<!--EXAMPLES:START-->", "<!--EXAMPLES:END-->"
COPIED_AT = 1794622200  # 2026-11-14 02:10 UTC: every sample file "was copied" then

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


def add_broken_files(data: Path) -> None:
    """Three files that are wrong in ways real exports are wrong, for the troubleshooting section."""
    folder = data / CUSTODIAN
    good = (folder / CUST_13).read_text(encoding="utf-8")
    (folder / "custodian_positions_20261114_20261114T200000.csv").write_bytes(good.encode("utf-16"))
    lines = good.splitlines()
    lines[3] = 'PF001,"2619045,40,USD'  # a quote that is opened and never closed
    (folder / "custodian_positions_20261115_20261115T200000.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (folder / "custodian_positions_20261116_20261116T200000.csv").write_text(good.replace(",", ";"), encoding="utf-8")


async def collect(data: Path) -> dict[str, dict]:
    config = Config(data_root=data, process_maps_dir=ROOT / "process_maps", limits=Limits(chunk_bytes=8192))
    out: dict[str, dict] = {}
    async with Client(build_server(config)) as client:
        for name, (tool, arguments) in EXAMPLES.items():
            arguments = dict(arguments)
            source = arguments.pop("cursor_from", None)
            if source:
                arguments["cursor"] = out[source]["response"]["next_cursor"]
            result = await client.call_tool(tool, arguments)
            out[name] = {"tool": tool, "arguments": arguments, "response": json.loads(result.content[0].text),
                         "is_error": bool(result.is_error)}
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fragment", type=Path, help="Also write the page without its <html>/<head>/<body> wrapper")
    parser.add_argument("--dump", type=Path, help="Also write the examples as a JSON file")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        data = Path(tmp) / "sample_data"
        make_sample_data.build(data)
        add_broken_files(data)
        for path in data.rglob("*.csv"):
            os.utime(path, (COPIED_AT, COPIED_AT))
        examples = anyio.run(collect, data)

    blob = json.dumps(examples, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    if args.dump:
        args.dump.write_text(json.dumps(examples, ensure_ascii=False, indent=1), encoding="utf-8")
    if GUIDE.exists():
        page = GUIDE.read_text(encoding="utf-8")
        if START not in page or END not in page:
            sys.exit(f"{GUIDE} has no {START} ... {END} block")
        block = f'{START}\n<script type="application/json" id="examples">{blob}</script>\n{END}'
        page = page[: page.index(START)] + block + page[page.index(END) + len(END):]
        GUIDE.write_text(page, encoding="utf-8")
        print(f"{len(examples)} examples written into {GUIDE.relative_to(ROOT)} ({len(blob) / 1024:.0f} KB)")
        if args.fragment:
            parts = re.findall(r"<!--PAGE:(?:HEAD|BODY)-->(.*?)<!--/PAGE:(?:HEAD|BODY)-->", page, flags=re.S)
            args.fragment.write_text("\n".join(part.strip("\n") for part in parts) + "\n", encoding="utf-8")
            print(f"fragment written to {args.fragment}")


if __name__ == "__main__":
    main()
