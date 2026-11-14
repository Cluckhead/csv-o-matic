#!/usr/bin/env python3
"""Create small sample data that matches the example process maps in process_maps/.

    python scripts/make_sample_data.py            # writes ./sample_data
    python scripts/make_sample_data.py /some/dir

The data contains things worth investigating: a vendor redelivery, a retried run, a backfill,
securities dropped by filter rules, and a position that fails its join because a SEDOL lost
its leading zero.
"""

from __future__ import annotations

import csv
import random
import sys
from pathlib import Path

VENDOR_COLUMNS = ["VendorId", "ISIN", "SEDOL", "SecurityName", "AssetType", "ExchangeCode", "IsPrimary",
                  "Status", "ClosePrice", "Ccy", "MaturityDate", "Coupon"]
# VendorId, ISIN, SEDOL, name, asset type, exchange, primary, status, price, currency, maturity, coupon
STORY = [
    ("V0001", "GB0002634946", "0263494", "BAE Systems PLC", "EQ", "XLON", "Y", "ACTIVE", "1312.50000", "GBX", "", ""),
    ("V0002", "US0846707026", "2073390", "Berkshire Hathaway, Inc.", "EQ", "XNYS", "Y", "ACTIVE", "452.11000", "USD", "", ""),
    ("V0003", "GB00B03MLX29", "B03MLX2", "Shell PLC", "EQ", "XLON", "Y", "ACTIVE", "2710.00000", "GBX", "", ""),
    ("V0003", "GB00B03MLX29", "B03MLX2", "Shell PLC", "EQ", "XAMS", "N", "ACTIVE", "31.95500", "EUR", "", ""),
    ("V0004", "GB0007980591", "0798059", "BP PLC", "EQ", "XLON", "Y", "DELISTED", "0.00000", "GBX", "", ""),
    ("V0005", "XS1234567890", "BF4KX01", "Acme Finance 4.5% 2031\n(guaranteed notes)", "BD", "XLON", "Y", "ACTIVE", "98.76543", "GBP", "2031-06-30", "4.5"),
    ("V0006", "XS0987654321", "B1YW440", "Old Bond 3% 2025", "BD", "XLON", "Y", "ACTIVE", "100.00000", "GBP", "2025-01-15", "3.0"),
    ("V0007", "GB00BYZ0WQ12", "BYZ0WQ1", "Gold Warrant Series A", "WT", "XLON", "Y", "ACTIVE", "0.42000", "GBP", "", ""),
    ("V0008", "", "0000001", "Unidentified Holding", "EQ", "XLON", "Y", "ACTIVE", "1.00000", "GBP", "", ""),
]
LATE_ADDITION = ("V0009", "GB00BLATE001", "0045614", "Late Listing PLC", "EQ", "XLON", "Y", "ACTIVE", "77.70000", "GBX", "", "")


def filler(count: int, seed: int) -> list[tuple]:
    rng = random.Random(seed)
    rows = []
    for i in range(count):
        sedol = f"{rng.randrange(10_000_000):07d}"
        rows.append((f"F{i:05d}", f"GB00F{i:07d}", sedol, f"Filler Company {i}", "EQ", "XLON", "Y", "ACTIVE",
                     f"{rng.uniform(1, 5000):.5f}", "GBX", "", ""))
    return rows


def write(path: Path, header: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


def security_master(root: Path, bdate: str, run: str, vendor: list[tuple], vendor_run: str, backfill: bool = False) -> list[dict]:
    """Apply the rules written in process_maps/security_master.yaml and write each stage."""
    base = root / "security_master"
    write(base / "01_vendor_raw" / f"vendor_ref_{bdate}_{vendor_run}.csv", VENDOR_COLUMNS, [list(r) for r in vendor])
    asset = {"EQ": "EQUITY", "BD": "BOND"}
    normalised = [
        {"isin": r[1].strip().upper(), "sedol": r[2], "name": r[3], "asset_class": asset.get(r[4], "OTHER"),
         "exchange_code": r[5], "is_primary": r[6], "close_price": f"{float(r[8]):.4f}", "currency": r[9],
         "maturity_date": r[10], "coupon": r[11]}
        for r in vendor if r[1].strip() and r[7] != "DELISTED"
    ]
    columns = list(normalised[0])
    suffix = "_BF" if backfill else ""
    write(base / "02_normalised" / f"sm_normalised_{bdate}_{run}{suffix}.csv", columns, [list(r.values()) for r in normalised])
    iso = f"{bdate[:4]}-{bdate[4:6]}-{bdate[6:]}"
    equity = [dict(r, sector="Industrials" if i % 2 else "Energy") for i, r in enumerate(normalised) if r["asset_class"] == "EQUITY"]
    bonds = [r for r in normalised if r["asset_class"] == "BOND" and r["maturity_date"] >= iso]
    write(base / "03a_equity_enriched" / f"sm_equity_{bdate}_{run}.csv", columns + ["sector"], [list(r.values()) for r in equity])
    write(base / "03b_fixed_income_enriched" / f"sm_fi_{bdate}_{run}.csv", columns, [list(r.values()) for r in bonds])
    golden_columns = ["isin", "sedol", "name", "asset_class", "close_price", "currency", "sector"]
    golden = [r for r in equity + bonds if r["is_primary"] == "Y"]
    write(base / "04_golden" / f"security_master_{bdate}_{run}.csv", golden_columns,
          [[r.get(c, "") for c in golden_columns] for r in golden])
    return golden


def positions(root: Path, bdate: str, run: str, golden: list[dict]) -> None:
    """Apply the rules written in process_maps/eod_positions.yaml and write each stage."""
    base = root / "positions"
    custodian = [
        ["PF001", "0263494", "1500", "GBP"],
        ["PF002", "0263494", "250", "GBP"],
        ["PF001", "2073390", "40", "USD"],
        ["PF003", "263494", "900", "GBP"],   # leading zero lost upstream: will not join
        ["PF001", "B03MLX2", "0", "GBP"],    # zero quantity: dropped
        ["INT01", "B03MLX2", "10000", "GBP"],  # internal portfolio: excluded from client report
        ["PF002", "BF4KX01", "200000", "GBP"],
        ["PF001", "0045614", "75", "GBP"],   # only in the security master after its rerun
    ]
    write(base / "01_custodian_raw" / f"custodian_positions_{bdate}_{run}.csv",
          ["PortfolioCode", "SecurityId", "Quantity", "SettlementCcy"], custodian)
    by_sedol = {g["sedol"]: g for g in golden}
    enriched = []
    for portfolio, sedol, quantity, _ in custodian:
        sec = by_sedol.get(sedol)
        if sec is None or float(quantity) == 0:
            continue
        value = f"{float(quantity) * float(sec['close_price']):.2f}"
        enriched.append([portfolio, sec["isin"], sedol, sec["name"], quantity, sec["close_price"], value, sec["currency"]])
    columns = ["portfolio_code", "isin", "sedol", "name", "quantity", "price", "market_value", "currency"]
    write(base / "02_enriched" / f"positions_enriched_{bdate}_{run}.csv", columns, enriched)
    write(base / "03a_client_report" / f"client_holdings_{bdate}_{run}.csv", ["Portfolio", "ISIN", "Name", "Quantity", "MarketValue"],
          [[r[0], r[1], r[3], r[4], r[6]] for r in enriched if not r[0].startswith("INT")])
    write(base / "03b_risk_feed" / f"risk_positions_{bdate}_{run}.csv", columns, enriched)


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "sample_data")
    bulk = filler(2000, seed=7)
    # 12 Nov: a normal day.
    golden = security_master(root, "20261112", "20261112T183000", STORY + bulk, "20261112T180200")
    positions(root, "20261112", "20261112T200000", golden)
    # 13 Nov: first run, positions built from it, then a vendor redelivery adds a security and
    # corrects a price, and the security master is rerun. Positions are NOT rerun.
    first = security_master(root, "20261113", "20261113T183000", STORY + bulk, "20261113T180200")
    positions(root, "20261113", "20261113T200000", first)
    corrected = [r if r[0] != "V0002" else r[:8] + ("455.00000",) + r[9:] for r in STORY]
    security_master(root, "20261113", "20261113T211500", corrected + [LATE_ADDITION] + bulk, "20261113T204700")
    # A backfill of 2 Nov, run on 13 Nov.
    security_master(root, "20261102", "20261113T220000", STORY + bulk, "20261102T180200", backfill=True)
    print(f"Sample data written to {root}")


if __name__ == "__main__":
    main()
