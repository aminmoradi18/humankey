"""Copy sample inputs and outputs out of a finished run, for the README's examples.

Usage: python3 scripts/make_examples.py LOG_DIR INVOICE_ID [INVOICE_ID ...]
Writes examples/<invoice>/input.json (the invoice and its vendor, as the agents' data had them) and
output.json (every event the run logged for that invoice: plan, drafts, critiques, decision, contract,
and the Zetaris receipts its events cite, when they cite any).
Nothing is written by hand: it only copies what the run logged.
"""
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def rows(name):
    with open(ROOT / "data" / "out" / f"{name}.csv", newline="") as f:
        return list(csv.DictReader(f))


def jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    logs = Path(sys.argv[1])
    events = jsonl(logs / "events.jsonl")
    receipts = {r["receipt_id"]: r for r in jsonl(logs / "receipts.jsonl")}
    llm = jsonl(logs / "llm.jsonl")
    invoices = {r["invoice_id"]: r for r in rows("invoices")}
    vendors = {r["vendor_id"]: r for r in rows("vendors")}
    for inv in sys.argv[2:]:
        invoice = invoices[inv]
        mine = [e for e in events if e.get("invoice_id") == inv]
        if not any(e["kind"] == "decision" for e in mine):
            sys.exit(f"{inv}: no decision in {logs}")
        cited = sorted({r for e in mine for r in e.get("receipts", []) if r in receipts})
        out = ROOT / "examples" / inv
        out.mkdir(parents=True, exist_ok=True)
        (out / "input.json").write_text(json.dumps(
            {"invoice": invoice, "vendor": vendors.get(invoice["vendor_id"])}, indent=2) + "\n")
        (out / "output.json").write_text(json.dumps(
            {"run": str(logs.relative_to(ROOT)) if logs.is_relative_to(ROOT) else str(logs),
             "models_in_run": sorted({(c["provider"], c["model"]) for c in llm if c.get("ok")}),
             "events": mine, **({"receipts": [receipts[r] for r in cited]} if cited else {})}, indent=2) + "\n")
        print(inv, next(e for e in mine if e["kind"] == "decision")["route"])


if __name__ == "__main__":
    main()
