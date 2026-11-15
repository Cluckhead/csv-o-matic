"""Nightly fund holdings feed. Runs from the scheduler after the security master is published."""
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

SHARE = Path("/mnt/etl")
IN_DIR = SHARE / "holdings" / "in"
GOLDEN_DIR = SHARE / "security_master" / "04_golden"
OUT_FUNDS = SHARE / "holdings" / "out" / "funds"
OUT_INTERNAL = SHARE / "holdings" / "out" / "internal"


def latest(folder: Path, prefix: str) -> Path:
    # file names sort by time, so the last one is the newest
    return sorted(folder.glob(f"{prefix}*.csv"))[-1]


def main(business_date: str) -> None:          # business_date like 20261113
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")

    # fund admin sends fundadmin_holdings_<date>_<HHMMSS>.csv, sometimes more than once
    src = latest(IN_DIR, f"fundadmin_holdings_{business_date}_")
    holdings = pd.read_csv(src, dtype=str, encoding="latin-1", keep_default_na=False)

    golden = pd.read_csv(latest(GOLDEN_DIR, f"security_master_{business_date}_"), dtype=str, keep_default_na=False)

    holdings = holdings[~holdings["TradeStatus"].isin(["CANCELLED", "PENDING"])]
    holdings["Qty"] = pd.to_numeric(holdings["Qty"], errors="coerce")
    holdings = holdings.dropna(subset=["Qty"])
    holdings = holdings[holdings["Qty"] != 0]
    holdings = holdings.drop_duplicates(subset=["FundCode", "Isin"], keep="last")

    merged = holdings.merge(golden[["isin", "name", "close_price", "currency"]], left_on="Isin", right_on="isin", how="inner")
    merged["market_value"] = (merged["Qty"] * merged["close_price"].astype(float)).round(2)

    out = merged.rename(columns={"FundCode": "fund", "Qty": "quantity", "close_price": "price"})[
        ["fund", "isin", "name", "quantity", "price", "market_value", "currency"]
    ]

    internal = out[out["fund"].str.startswith("X")]
    external = out[~out["fund"].str.startswith("X")]

    external.to_csv(OUT_FUNDS / f"holdings_funds_{business_date}_{run_ts}.csv", index=False)
    internal.to_csv(OUT_INTERNAL / f"holdings_internal_{business_date}_{run_ts}.csv", index=False, sep="\t")


if __name__ == "__main__":
    main(sys.argv[1])
