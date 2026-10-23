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
    try:
        z.setup("DROP DATASOURCE northwind_erp")
    except ZetarisError:
        pass  # first run: nothing to drop
    text = (ROOT / "scripts" / "register_zetaris_sources.sql").read_text()
    text = text.replace("${ERP_DB_PASSWORD}", os.environ.get("ERP_DB_PASSWORD", "northwind"))
    for stmt in text.split(";"):
        body = "\n".join(l for l in stmt.splitlines() if not l.strip().startswith("--")).strip()
        if body:
            z.setup(body)
    bad = 0
    for t in TABLES:
        expected = sum(1 for _ in csv.DictReader(open(ROOT / "data" / "out" / f"{t}.csv")))
        rows, rc = z.query(f"SELECT count(*) AS n FROM northwind_erp.{t}", purpose="setup check", agent="setup")
        got = int(rows[0]["n"]) if rows else None
        ok = got == expected
        bad += not ok
        print(f"{'PASS' if ok else 'FAIL'} northwind_erp.{t}: {got} rows (expected {expected})")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
