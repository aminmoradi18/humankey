"""Re-score saved benchmark runs with the current approval lanes (no model calls).

Usage: python3 -m eval.tiers 101-105 [108-109 ...]
Reads logs/bench/seed*/ from `python3 -m eval.bench --keep`. Each saved proposal is routed the way
the gate routes it today: auto under AUTO_LIMIT, phone for clean invoices up to the approval threshold
that pay the vendor's last paid account, the hardware key for the rest.
"""
import collections
import csv
import json
import sys

from agent.tools import AUTO_LIMIT
from common.env import ROOT
from gate.main import approval_tier

LANES = ("auto", "phone", "key", "blocked")


def lane(p, last_paid):
    if p["route"] == "BLOCKED":
        return "blocked"
    if p["route"] == "QUEUED_AUTO" and (p.get("amount") or 0) < AUTO_LIMIT:
        return "auto"
    return "phone" if approval_tier(dict(p, route="AWAITING_KEY"), last_paid) == "phone" else "key"


def seeds(args):
    for a in args:
        lo, _, hi = a.partition("-")
        yield from range(int(lo), int(hi or lo) + 1)


def main():
    count = collections.Counter()
    for seed in seeds(sys.argv[1:]):
        out = ROOT / "logs" / "bench" / f"seed{seed}"
        labels = {r["invoice_id"]: r["is_fraud"] == "1" for r in csv.DictReader(open(out / "labels.csv"))}
        last_paid = {}
        for r in sorted(csv.DictReader(open(out / "payments.csv")), key=lambda r: r["paid_at"]):
            last_paid[r["vendor_id"]] = r["iban_hash"]
        for p in json.loads((out / "gate" / "proposals.json").read_text()).values():
            count[(lane(p, last_paid), labels[p["invoice_id"]])] += 1
    print(f"| lane | clean | fraud |\n|---|---|---|")
    for name in LANES:
        print(f"| {name} | {count[(name, False)]} | {count[(name, True)]} |")


if __name__ == "__main__":
    main()
