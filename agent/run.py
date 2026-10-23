"""Investigate invoices end to end and write proposals to gate/state/proposals.json.

Usage: python3 -m agent.run [INV0008 ...]      (no ids: every invoice in Zetaris)
"""
import json
from pathlib import Path
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from agent.agents import Critic, Guard, Investigator, Planner, Reporter, Risk, apply_memory, event, required_checks
from agent.tools import DB, Tools
from common.env import ROOT
from agent.world import World
from common.jsonl import canonical, now_iso, read
from llm.provider import LLM
from zetaris.client import ZetarisClient

STATE = Path(os.environ.get("HUMANKEY_STATE", ROOT / "gate" / "state"))


def receipts_for(findings):
    ids = {i for f in findings.values() for i in f["receipt_ids"]}
    return [r for r in read("receipts") if r["receipt_id"] in ids]


def investigate(invoice_id, z, llm, world=None, run_id="run", max_revisions=1):
    tools = Tools(z)
    planner, inv, guard, critic = Planner(llm), Investigator(llm, tools), Guard(llm), Critic(z)
    event("start", invoice_id)

    header = tools.run("invoice_header", invoice_id)
    vendor = header["facts"].get("vendor_id")
    hints = world.hints(vendor, exclude_invoice=invoice_id) if world and vendor else []
    plan = planner.plan(header, hints)
    event("plan", invoice_id, agent="planner", checks=plan["checks"], reason=plan["reason"], memory_hints=hints)

    findings = inv.run_checks(invoice_id, plan["checks"], {"invoice_header": header})
    memo_row, _ = tools.memo(invoice_id)
    memo = " ".join(str(memo_row.get(k) or "") for k in ("subject", "memo"))
    flags = guard.scan(memo)
    if flags:
        event("injection", invoice_id, agent="guard", flags=flags)

    draft = inv.write(invoice_id, findings, memo)
    event("draft", invoice_id, agent="investigator", proposal=draft["proposal"], claims=len(draft["claims"]))
    verdict, reasons, integrity = critic.review(draft, findings, receipts_for(findings), flags)
    event("critic", invoice_id, agent="critic", verdict=verdict, reasons=reasons, integrity=integrity["verdict"])

    revisions = 0
    while verdict == "revise" and revisions < max_revisions:
        revisions += 1
        extra = [c for c in required_checks(findings) if c not in findings]
        inv.run_checks(invoice_id, extra, findings)
        draft = inv.write(invoice_id, findings, memo, feedback="; ".join(reasons))
        event("revise", invoice_id, agent="investigator", added_checks=extra, proposal=draft["proposal"])
        verdict, reasons, integrity = critic.review(draft, findings, receipts_for(findings), flags)
        event("critic", invoice_id, agent="critic", verdict=verdict, reasons=reasons, integrity=integrity["verdict"])

    if world and vendor:
        w = world.observe(invoice_id, vendor, run_id, receipts_for(findings), findings)
        event("world", invoice_id, agent="investigator", **w["ingest"])
    remembered = apply_memory(world, vendor, findings) if world and vendor else []
    if remembered:
        event("memory", invoice_id, agent="risk", recalled=remembered)
    decision = Risk.decide(draft, findings, flags, integrity, reasons)
    key_text = Reporter.key_text(invoice_id, decision, findings, flags,
                                model_down=draft.get("explanation", "").startswith("investigator unavailable"))
    proposal = {
        "invoice_id": invoice_id, **decision,
        "vendor_id": header["facts"].get("vendor_id"), "amount": header["facts"].get("amount"),
        "claims": draft["claims"], "explanation": draft.get("explanation", ""),
        "findings": findings, "injection_flags": flags, "integrity": integrity,
        "critic": {"verdict": verdict, "reasons": reasons, "revisions": revisions},
        "plan": plan, "key_text": key_text, "memory": remembered,
    }
    event("decision", invoice_id, agent="risk", **decision, key_text=key_text)
    if world and vendor:
        world.decision(invoice_id, vendor, run_id, decision)
    return proposal


def coverage(z):
    """The work list and an independent count witness for it.

    The work list joins invoices to the vendor master (that is how the agents get vendor data);
    an inner join silently drops an invoice whose vendor is missing. The witness counts the raw
    table on its own and lists what the join lost (anti-join), so a gap cannot hide.
    """
    rows, rc = z.query(f"SELECT i.invoice_id, i.vendor_id FROM {DB}.invoices i "
                       f"JOIN {DB}.vendors v ON v.vendor_id = i.vendor_id ORDER BY i.invoice_id",
                       purpose="work list", agent="planner")
    count, rc2 = z.query(f"SELECT count(*) AS n FROM {DB}.invoices", purpose="count witness", agent="critic")
    lost, rc3 = z.query(f"SELECT i.invoice_id FROM {DB}.invoices i LEFT JOIN {DB}.vendors v "
                        f"ON v.vendor_id = i.vendor_id WHERE v.vendor_id IS NULL ORDER BY i.invoice_id",
                        purpose="count witness: rows the join dropped", agent="critic")
    listed = {r["invoice_id"]: r["vendor_id"] for r in rows}
    witness = {"exp": int(count[0]["n"]) if count else None, "cov": len(listed),
               "miss": [r["invoice_id"] for r in lost], "receipts": [rc["receipt_id"], rc2["receipt_id"], rc3["receipt_id"]]}
    return listed, witness


def run_group(ids, run_id):
    """Invoices of one vendor, in order, so later ones see what earlier ones put in the World Model."""
    z, llm, world = ZetarisClient(), LLM(), World()
    return [investigate(i, z, llm, world, run_id) for i in ids]


def main(ids):
    z = ZetarisClient()
    World()  # start the service once before the workers
    run_id = "run_" + now_iso().replace(":", "").replace("-", "")
    listed, witness = coverage(z)
    event("coverage", "*", agent="critic", **witness)
    ids = ids or sorted(listed)
    groups = {}
    for i in ids:
        groups.setdefault(listed.get(i, i), []).append(i)
    STATE.mkdir(parents=True, exist_ok=True)
    out = {}
    with ThreadPoolExecutor(max_workers=int(os.environ.get("RUN_WORKERS", "3"))) as pool:
        for result in pool.map(lambda g: run_group(g, run_id), groups.values()):
            for p in result:
                p["witness"] = {k: witness[k] for k in ("exp", "cov", "miss")}
                out[p["invoice_id"]] = p
    for i in sorted(out):
        p = out[i]
        print(f"{i}: {p['proposal']:8} {p['route']:12} critic={p['critic']['verdict']}"
              f"(rev {p['critic']['revisions']}) integrity={p['integrity']['verdict']}")
        print("   " + p["key_text"].replace("\n", " | "))
    path = STATE / "proposals.json"
    old = json.loads(path.read_text()) if path.exists() else {}
    old.update(out)
    path.write_text(canonical(old))
    print(f"coverage: investigated {len(out)} of {witness['exp']} invoices in Zetaris ({run_id}); "
          f"not reached: {witness['miss'] or 'none'}")


if __name__ == "__main__":
    main(sys.argv[1:])
