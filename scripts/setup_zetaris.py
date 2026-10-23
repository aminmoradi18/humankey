"""One-shot setup: (re)register our sources in Zetaris and verify row counts.

Usage: python3 scripts/setup_zetaris.py
Needs Zetaris running and scripts/erp_db.sh already run.
"""
import csv
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.env import ROOT, load_env  # noqa: E402
from zetaris.client import ZetarisClient, ZetarisError  # noqa: E402

TABLES = ["vendors", "purchase_orders", "payments", "emails", "bank_change_requests", "invoices"]


def main():
    load_env()
    z = ZetarisClient(mode="live")
    for drop in ("DROP DATASOURCE northwind_erp", "DROP SCHEMASTORE VIEW lei_entities FROM gleif",
                 "DROP LIGHTNING DATABASE GLEIF_REST"):
        try:
            z.setup(drop)
        except ZetarisError:
            pass  # first run: nothing to drop
    text = (ROOT / "scripts" / "register_zetaris_sources.sql").read_text()
    text = text.replace("${ERP_DB_PASSWORD}", os.environ.get("ERP_DB_PASSWORD", "northwind"))
    leis = [r["registry_id"] for r in csv.DictReader(open(ROOT / "data" / "out" / "vendors.csv")) if r["registry_id"]]
    text = text.replace("${LEI_LIST}", ",".join(leis))
    for stmt in text.split(";"):
        body = "\n".join(l for l in stmt.splitlines() if not l.strip().startswith("--")).strip()
        if body:
            try:
                z.setup(body)
            except ZetarisError as e:
                if "already exist" not in str(e).lower():
                    raise
                print(f"(kept existing: {body.split('(')[0][:60]})")
    bad = 0
    for t in TABLES:
        expected = sum(1 for _ in csv.DictReader(open(ROOT / "data" / "out" / f"{t}.csv")))
        rows, rc = z.query(f"SELECT count(*) AS n FROM northwind_erp.{t}", purpose="setup check", agent="setup")
        got = int(rows[0]["n"]) if rows else None
        ok = got == expected
        bad += not ok
        print(f"{'PASS' if ok else 'FAIL'} northwind_erp.{t}: {got} rows (expected {expected})")
    rows, _ = z.query("SELECT count(*) AS n FROM gleif.lei_entities", purpose="setup check", agent="setup")
    got = int(rows[0]["n"]) if rows else None
    print(f"{'PASS' if got else 'FAIL'} gleif.lei_entities: {got} registry records for {len(leis)} vendor LEIs (live GLEIF)")
    bad += not got
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
