"""Benchmark: many generated companies, three ways of paying their invoices.

Usage: python3 -m eval.bench [--seeds 101-110] [--keep]
Needs the live stack (Zetaris + model provider). Each seed is a different company: the ordinary
vendors swap roles, so every planted fraud lands on a different vendor with different amounts.
Writes eval/BENCHMARK.md and eval/benchmark.json. The demo data (seed 7) is reloaded at the end.

The three ways, on the same invoices:
  A. AP rule, no AI:      pay automatically when the PO matches and the amount is under the
                          approval limit; everything else goes to a person.
  B. Agent alone:         the investigator's first answer decides (clear = paid), no critic,
                          no key, no count witness. Invoices the agents never saw stay unseen.
  C. HumanKey:            the full system: critic, risk rules, the key (with its count witness and
                          account memory) for anything risky or over the limit.
"""
import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from common.env import ROOT

THRESHOLD = 10000.0
WORK = ROOT / "logs" / "bench"


def parse_seeds(text):
    out = []
    for part in text.split(","):
        a, _, b = part.partition("-")
        out += list(range(int(a), int(b) + 1)) if b else [int(a)]
    return out


def load(path):
    return {r[next(iter(r))]: r for r in csv.DictReader(open(path))}


def baseline_ap_rule(out):
    """A: the classic rule, computed from the company's own records (all invoices, no join)."""
    invoices, pos = load(out / "invoices.csv"), load(out / "purchase_orders.csv")
    res = {}
    for inv, r in invoices.items():
        po = pos.get(r["po_id"]) if r["po_id"] else None
        amt = float(r["amount"])
        ok_po = po and po["vendor_id"] == r["vendor_id"] and abs(float(po["amount"]) - amt) <= 0.05 * float(po["amount"])
        res[inv] = "paid" if ok_po and amt < THRESHOLD else "human"
    return res


def key_would_warn(out, inv_row):
    """The key's account memory: last account this vendor was actually paid to (payment history)."""
    paid = [r for r in csv.DictReader(open(out / "payments.csv")) if r["vendor_id"] == inv_row["vendor_id"]]
    if not paid:
        return False
    last = sorted(paid, key=lambda r: r["paid_at"])[-1]
    return last["iban_hash"] != inv_row["iban_hash"]


def score(seed, out, props, witness):
    labels = load(out / "labels.csv")
    invoices = load(out / "invoices.csv")
    a = baseline_ap_rule(out)
    rows = []
    for inv, lab in labels.items():
        fraud = lab["is_fraud"] == "1"
        p = props.get(inv)
        b = "unseen" if not p else {"clear": "paid", "block": "blocked"}.get(p.get("first_draft"), "human")
        if not p:
            c = "flagged by count witness" if inv in witness.get("miss", []) else "unseen"
        else:
            c = {"QUEUED_AUTO": "paid", "BLOCKED": "blocked"}.get(p["route"], "human")
            if c == "human" and key_would_warn(out, invoices[inv]):
                c = "human + key warning"
        rows.append({"seed": seed, "invoice": inv, "case": lab["note"], "fraud": fraud, "A": a[inv], "B": b, "C": c})
    return rows


def summarize(rows):
    def m(cfg):
        fr = [r for r in rows if r["fraud"]]
        cl = [r for r in rows if not r["fraud"]]
        return {
            "frauds": len(fr),
            "frauds paid with no human": sum(r[cfg] == "paid" for r in fr),
            "frauds never seen or flagged": sum(r[cfg] == "unseen" for r in fr),
            "clean invoices": len(cl),
            "clean invoices blocked": sum(r[cfg] == "blocked" for r in cl),
            "clean invoices paid automatically": sum(r[cfg] == "paid" for r in cl),
            "invoices sent to a person": sum(r[cfg].startswith("human") for r in rows),
        }
    return {cfg: m(cfg) for cfg in "ABC"}


def run_seed(seed, i, env_base, reuse=False):
    out = WORK / f"seed{seed}"
    done = out / "gate" / "proposals.json"
    if reuse and done.exists():
        props = json.loads(done.read_text())
        witness = next(iter(props.values()), {}).get("witness", {})
        return score(seed, out, props, witness), None
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    subprocess.run([sys.executable, "data/gen.py", "--seed", str(seed), "--out", str(out)], cwd=ROOT, check=True,
                   stdout=subprocess.DEVNULL)
    subprocess.run(["./scripts/erp_db.sh"], cwd=ROOT, check=True, env={**env_base, "DATA_OUT": str(out)},
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    port = str(8730 + i)
    # a world server left over from an interrupted run would still point at the deleted folder
    subprocess.run(["fuser", "-k", f"{port}/tcp"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    env = {**env_base, "HUMANKEY_STATE": str(out / "gate"), "HUMANKEY_LOGS": str(out / "logs"),
           "HUMANKEY_SNAPSHOTS": str(out / "snapshots"), "HUMANKEY_LLM_CACHE": str(out / "llm_cache"),
           "WORLD_PORT": port, "WORLD_DIR": str(out / "world"), "HMEM_DIR": str(out / "hmem"),
           "LLM_USE_CACHE": "false"}
    t0 = time.time()
    subprocess.run([sys.executable, "-m", "agent.run"], cwd=ROOT, env=env, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["fuser", "-k", f"{port}/tcp"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    llm_log = out / "logs" / "llm.jsonl"
    calls = [json.loads(x) for x in llm_log.read_text().splitlines() if x.strip()] if llm_log.exists() else []
    if not any(c.get("ok") for c in calls):
        last = calls[-1].get("error", "")[:200] if calls else "no calls logged"
        sys.exit(f"seed {seed}: no model call succeeded, results would be meaningless ({last})")
    props = json.loads((out / "gate" / "proposals.json").read_text())
    witness = next(iter(props.values()), {}).get("witness", {})
    return score(seed, out, props, witness), round(time.time() - t0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", default="101-110")
    ap.add_argument("--keep", action="store_true", help="keep per-seed folders in logs/bench")
    ap.add_argument("--reuse", action="store_true", help="score seeds already run with --keep instead of rerunning")
    ap.add_argument("--name", default="", help="write eval/BENCHMARK_<name>.md and logs/bench_<name>/ instead")
    args = ap.parse_args()
    global WORK
    if args.name:
        WORK = ROOT / "logs" / f"bench_{args.name}"
    report = ROOT / "eval" / (f"BENCHMARK_{args.name}.md" if args.name else "BENCHMARK.md")
    summary = ROOT / "eval" / (f"benchmark_{args.name}.json" if args.name else "benchmark.json")
    seeds = parse_seeds(args.seeds)
    env_base = dict(os.environ)
    all_rows, times = [], {}
    try:
        for i, seed in enumerate(seeds):
            rows, secs = run_seed(seed, i, env_base, args.reuse)
            all_rows += rows
            if secs is None:
                secs = json.loads(summary.read_text())["seconds"].get(str(seed), 0)
            times[seed] = secs
            s = summarize(rows)
            print(f"seed {seed}: {secs}s  frauds paid w/o human A={s['A']['frauds paid with no human']} "
                  f"B={s['B']['frauds paid with no human']} C={s['C']['frauds paid with no human']}", flush=True)
    finally:
        subprocess.run(["./scripts/erp_db.sh"], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    total = summarize(all_rows)
    llm = []
    for seed in seeds:
        p = WORK / f"seed{seed}" / "logs" / "llm.jsonl"
        if p.exists():
            llm += [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
    models = sorted({x["model"] for x in llm if x.get("ok")})
    summary.write_text(json.dumps({"seeds": seeds, "summary": total, "rows": all_rows,
                                                              "seconds": times, "models": models}, indent=1))
    keys = list(total["A"].keys())
    md = ["# Benchmark", "",
          f"{len(seeds)} generated companies (seeds {args.seeds}), {len(all_rows)} invoices, "
          f"{total['A']['frauds']} planted frauds. Models: {', '.join(models)}. "
          f"Average {round(sum(times.values()) / len(times))} s per company "
          f"(provider: {os.environ.get('LLM_PROVIDER', 'openrouter')}).", "",
          "A = AP rule without AI (PO matches and amount under the limit: pay). "
          "B = agent alone (its first answer decides). C = HumanKey.", "",
          "| | A. AP rule | B. agent alone | C. HumanKey |", "|---|---|---|---|"]
    md += [f"| {k} | {total['A'][k]} | {total['B'][k]} | {total['C'][k]} |" for k in keys]
    md += ["", "Per planted case (C):", "", "| case | count | outcomes |", "|---|---|---|"]
    by = {}
    for r in all_rows:
        by.setdefault(r["case"], []).append(r["C"])
    for case, outs in sorted(by.items()):
        cnt = {o: outs.count(o) for o in sorted(set(outs))}
        md.append(f"| {case} | {len(outs)} | " + ", ".join(f"{o}: {n}" for o, n in cnt.items()) + " |")
    report.write_text("\n".join(md) + "\n")
    print("\n".join(md))
    if not args.keep:
        shutil.rmtree(WORK, ignore_errors=True)


if __name__ == "__main__":
    main()
