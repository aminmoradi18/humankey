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
NS = "lightning.metastore.humankey"
USL = NS + ".northwind"

# Each USL table is bound to a query over the registered sources. vendors is where the two
# sources meet: the ERP vendor master LEFT JOIN the live GLEIF registry.
USL_ACTIVATE = {
    "vendors": "SELECT COALESCE(v.vendor_id, '') AS vendor_id, v.name, v.legal_name, v.country, v.registry_id AS lei, "
               "v.email_domain, v.iban_last4, v.iban_hash, v.bank_since, v.contact_name, g.legal_name AS gleif_legal_name, "
               "g.entity_status AS gleif_entity_status, g.registration_status AS gleif_registration_status "
               "FROM northwind_erp.vendors v LEFT JOIN gleif.lei_entities g ON g.lei = v.registry_id",
    "invoices": "SELECT COALESCE(invoice_id, '') AS invoice_id, vendor_id, po_id, CAST(amount AS DECIMAL(12,2)) AS amount, "
                "currency, issued_at, iban_hash, iban_last4, memo, email_id FROM northwind_erp.invoices",
    "payments": "SELECT COALESCE(payment_id, '') AS payment_id, vendor_id, invoice_id, CAST(amount AS DECIMAL(12,2)) AS amount, "
                "paid_at, iban_hash, iban_last4 FROM northwind_erp.payments",
    "purchase_orders": "SELECT COALESCE(po_id, '') AS po_id, vendor_id, CAST(amount AS DECIMAL(12,2)) AS amount, currency, "
                       "created_at, status FROM northwind_erp.purchase_orders",
    "bank_change_requests": "SELECT COALESCE(request_id, '') AS request_id, vendor_id, new_iban_hash, new_iban_last4, "
                            "requested_at, channel, verified_by FROM northwind_erp.bank_change_requests",
    "emails": "SELECT COALESCE(email_id, '') AS email_id, from_addr, from_domain, to_addr, received_at, subject, body "
              "FROM northwind_erp.emails",
}

# Data-quality rules (on top of the PK/FK constraints declared in the model, e.g. invoices.vendor_id).
USL_DQ = [
    ("vendor_in_gleif", "vendors", "gleif_legal_name IS NOT NULL"),
    ("vendor_lei_current", "vendors", "gleif_registration_status = 'ISSUED'"),
    ("invoice_amount_positive", "invoices", "amount > 0"),
    ("invoice_has_payee", "invoices", "iban_hash IS NOT NULL AND length(iban_hash) > 0"),
    ("bank_change_verified", "bank_change_requests", "verified_by IS NOT NULL AND length(verified_by) > 0"),
]


def setup_usl(z):
    try:
        z.setup(f"REMOVE USL northwind NAMESPACE {NS}")
    except ZetarisError:
        pass  # first run
    z.setup(f"CREATE NAMESPACE IF NOT EXISTS {NS}")
    ddl = "\n".join(l for l in (ROOT / "scripts" / "usl_northwind.sql").read_text().splitlines()
                    if not l.strip().startswith("--"))
    z.setup(ddl)
    for table, query in USL_ACTIVATE.items():
        z.setup(f"ACTIVATE USL TABLE {USL}.{table} AS {query}")
    for rule, table, expr in USL_DQ:
        z.setup(f"REGISTER DQ {rule} TABLE {USL}.{table} AS {expr}")


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
    setup_usl(z)
    for t in TABLES:
        rows, _ = z.query(f"SELECT count(*) AS n FROM {USL}.{t}", purpose="setup check", agent="setup")
        print(f"{'PASS' if rows else 'FAIL'} USL {t}: {rows[0]['n'] if rows else '?'} rows")
        bad += not rows
    for rule, table, _ in USL_DQ + [("vendor_id", "invoices", "FK")]:
        rows, _ = z.query(f"RUN DQ {rule} TABLE {USL}.{table}", purpose="setup check", agent="setup")
        r = rows[0] if rows else {}
        print(f"DQ   {table}.{rule}: {r.get('valid_record')} valid, {r.get('invalid_record')} invalid of {r.get('total_record')}")
    rows, _ = z.query("SELECT count(*) AS n FROM gleif.lei_entities", purpose="setup check", agent="setup")
    got = int(rows[0]["n"]) if rows else None
    print(f"{'PASS' if got else 'FAIL'} gleif.lei_entities: {got} registry records for {len(leis)} vendor LEIs (live GLEIF)")
    bad += not got
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
