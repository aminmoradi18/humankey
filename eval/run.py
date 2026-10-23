"""Score proposals against data/labels.csv (labels are never shown to the agents).

Usage: python3 -m eval.run            writes eval/RESULTS.md
A fraud is "stopped" when its route is BLOCKED or AWAITING_KEY (money cannot move
without the human key). A clean invoice is a "false alarm" when it is BLOCKED.
"""
import csv
import json
from collections import Counter

from common.env import ROOT
from common.jsonl import read

LABELS = ROOT / "data" / "labels.csv"
PROPOSALS = ROOT / "gate" / "state" / "proposals.json"


def main():
    labels = {r["invoice_id"]: r for r in csv.DictReader(open(LABELS))}
    props = json.loads(PROPOSALS.read_text())
    rows, c = [], Counter()
    for inv, lab in sorted(labels.items()):
        p = props.get(inv)
        fraud = lab["is_fraud"] == "1"
        if not p:
            rows.append((inv, lab["note"], lab["expected_proposal"], "-", "-", "NOT INVESTIGATED"))
            c["missing"] += 1
            continue
        stopped = p["route"] in ("BLOCKED", "AWAITING_KEY")
        if fraud:
            c["fraud"] += 1
            c["fraud_stopped"] += stopped
            c["fraud_blocked"] += p["route"] == "BLOCKED"
            verdict = "stopped" if stopped else "MISSED"
        else:
            c["clean"] += 1
            c["false_block"] += p["route"] == "BLOCKED"
            c["clean_auto"] += p["route"] == "QUEUED_AUTO"
            verdict = "false alarm" if p["route"] == "BLOCKED" else "ok"
        c["exact"] += p["proposal"] == lab["expected_proposal"]
        rows.append((inv, lab["note"], lab["expected_proposal"], p["proposal"], p["route"], verdict))
    llm = read("llm")
    models = Counter(x["model"] for x in llm if x.get("ok"))
    revises = sum(1 for e in read("events") if e.get("kind") == "revise")
    out = ["# Evaluation", "",
           f"- Frauds stopped before payment: **{c['fraud_stopped']} / {c['fraud']}** "
           f"({c['fraud_blocked']} blocked outright, the rest held for the human key)",
           f"- Clean invoices wrongly blocked: **{c['false_block']} / {c['clean']}**; "
           f"paid automatically: {c['clean_auto']} (the others were over the approval threshold or escalated)",
           f"- Proposal equals the label: {c['exact']} / {len(labels) - c['missing']}",
           f"- Critic send-backs: {revises}",
           f"- Model calls: {sum(models.values())} ok, by model: " + ", ".join(f"`{m}` x{n}" for m, n in models.items()),
           "", "| invoice | planted case | expected | proposal | route | result |", "|---|---|---|---|---|---|"]
    out += [f"| {' | '.join(map(str, r))} |" for r in rows]
    (ROOT / "eval" / "RESULTS.md").write_text("\n".join(out) + "\n")
    print("\n".join(out))


if __name__ == "__main__":
    main()
