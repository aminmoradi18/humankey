"""Build the no-install replay page (site/) from a real sample run.

Usage: python3 scripts/build_site.py
Runs `run.py` in sample mode, then, for every invoice, sends it through the real gate with the browser
emulator answering approve and, separately, reject (each from a fresh copy of the run's state). What the
key showed, the gate's result, its events and ledger lines are recorded as they happened. site/index.html is
the web view with a small replay layer: it plays those recordings back when a visitor clicks. Nothing is
signed in the browser, and the page says so.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"

# Runs in a child process, because the gate reads its folders from the environment at import time.
CHILD = r"""
import json, sys
from gate.devices import EmulatorDevice
from gate.main import STATE, Gate, load_last_paid
screens = []
def ask(self, text):
    screens.append(text)
    return self.script.pop(0) if self.script else "timeout"
EmulatorDevice._ask = ask
p = json.loads((STATE / "proposals.json").read_text())[sys.argv[1]]
dev = EmulatorDevice(script=[sys.argv[2]] * 4)
result = Gate(dev, allow_emulator=True, last_paid=load_last_paid() or None).process(p)
print(json.dumps({"screens": screens, "result": result}))
"""


def jsonl(path):
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.exists() else []


def sample_run():
    r = subprocess.run([sys.executable, "run.py", "run", "--mode", "sample"], cwd=ROOT, capture_output=True,
                       text=True, env={**os.environ, "SAMPLE_MODE": "true"})
    out = json.loads(r.stdout[r.stdout.index("\n{") + 1:])
    if out["status"] != "success":
        sys.exit(f"sample run failed: {out}")
    return ROOT / out["run_folder"]


def outcome(folder, inv, answer):
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "run"
        shutil.copytree(folder, work, ignore=shutil.ignore_patterns("world", "hmem", "llm_cache"))
        env = {**os.environ, "SAMPLE_MODE": "true", "DEVICE": "emulator", "HUMANKEY_LOGS": str(work),
               "HUMANKEY_STATE": str(work / "gate"), "HUMANKEY_EMU_DIR": str(work / "emulator")}
        n_events = len(jsonl(work / "events.jsonl"))
        r = subprocess.run([sys.executable, "-c", CHILD, inv, answer], cwd=ROOT, env=env, capture_output=True,
                           text=True, timeout=120)
        if r.returncode:
            sys.exit(f"{inv} {answer}: {r.stderr[-400:]}")
        rec = json.loads(r.stdout.strip().splitlines()[-1])
        rec["events"] = jsonl(work / "events.jsonl")[n_events:]
        rec["paid"] = next((x for x in jsonl(work / "gate" / "ledger.jsonl") if x["invoice_id"] == inv), None)
        return rec


def state(folder):
    """What the web view's /api/state returns, read from the run (same fields as web/server.py)."""
    sys.path.insert(0, str(ROOT))
    from agent.agents import SOFT_CHECKS
    proposals = json.loads((folder / "gate" / "proposals.json").read_text())
    receipts = {r["receipt_id"]: r for r in jsonl(folder / "receipts.jsonl")}
    items = []
    for inv, p in sorted(proposals.items()):
        rids = {i for f in p["findings"].values() for i in f["receipt_ids"]}
        items.append({**p, "paid": None, "gate_event": None, "receipts": [receipts[i] for i in rids if i in receipts]})
    models = {}
    for x in jsonl(folder / "llm.jsonl"):
        if x.get("ok"):
            models[x["model"]] = models.get(x["model"], 0) + 1
    return {"items": items, "soft_checks": sorted(SOFT_CHECKS), "models": models,
            "device": {"kind": "emulator", "pending": None, "busy": None},
            "events": jsonl(folder / "events.jsonl")[-60:]}


def main():
    folder = sample_run()
    st = state(folder)
    outcomes = {}
    for it in st["items"]:
        inv = it["invoice_id"]
        answers = ["approve", "reject"] if it["route"] == "AWAITING_KEY" else ["approve"]
        outcomes[inv] = {a: outcome(folder, inv, a) for a in answers}
        print(inv, {a: o["result"] for a, o in outcomes[inv].items()})
    SITE.mkdir(exist_ok=True)
    data = {"state": st, "outcomes": outcomes, "run": folder.name}
    (SITE / "replay.json").write_text(json.dumps(data, ensure_ascii=False))
    page = (ROOT / "web" / "index.html").read_text()
    layer = (ROOT / "web" / "replay.js").read_text()
    banner = ('<div id="replayBanner">Replay of a recorded run. The agents, critic and gate ran for real when it was '
              'recorded; clicking here plays back what they did. Nothing is signed in this page. '
              '<a href="https://github.com/aminmoradi18/humankey#run-it">Run it yourself</a></div>')
    page = page.replace("<body>\n", "<body>\n" + banner + "\n", 1)
    page = page.replace("<script>\n\"use strict\";", "<script>\n" + layer + "\n</script>\n<script>\n\"use strict\";", 1)
    emu_note = "Browser emulator: signs with its own Ed25519 key and is labelled 'emulator' in every record. It is not the hardware key."
    assert emu_note in page
    page = page.replace(emu_note, "Replay: these are the screens the browser emulator showed, and the gate's answers, when the run "
                        "was recorded. The real emulator signs with its own Ed25519 key; this page signs nothing.")
    (SITE / "index.html").write_text(page)
    print(f"site/ written from {folder.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
