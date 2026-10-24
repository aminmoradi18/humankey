"""Score a run against the decisions people signed on the key or the phone.

Usage: python3 -m eval.regression [state_dir]
Every approval and every rejection a person signs is kept as a labelled case (gate/state/human_labels.jsonl,
with the device signature). This replays the current proposals against them: a case the person rejected
must not be proposed as "clear", and a case the person approved should not be blocked. The set grows
with every decision, so the agents are graded on what people actually decided, not only on our labels.
"""
import json
import sys
from pathlib import Path

from common.jsonl import read
from gate.main import STATE


def main(state):
    labels = {c["invoice_id"]: c for c in read("human_labels", state)}  # the latest decision per invoice
    props = json.loads((state / "proposals.json").read_text()) if (state / "proposals.json").exists() else {}
    bad, ok, missing = [], 0, 0
    for inv, c in sorted(labels.items()):
        p = props.get(inv)
        if not p:
            missing += 1
            continue
        if c["human"] == "reject" and p["proposal"] == "clear":
            bad.append(f"{inv}: a person rejected it, the agents now say clear")
        elif c["human"] == "approve" and p["proposal"] == "block":
            bad.append(f"{inv}: a person approved it, the agents now block it")
        else:
            ok += 1
    rejected = sum(c["human"] == "reject" for c in labels.values())
    print(f"human-signed cases: {len(labels)} ({rejected} rejected, {len(labels) - rejected} approved)")
    print(f"agreeing with the person: {ok}, disagreeing: {len(bad)}, not in this run: {missing}")
    for b in bad:
        print("  " + b)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else STATE))
