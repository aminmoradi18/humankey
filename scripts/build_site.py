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
# mode "key": the emulator answers. "phone": a paired phone answers, signing in this process the way the app
# does (gate/crypto.py decision_message). "queue": what the phone app's list and profile endpoints return.
CHILD = r"""
import json, sys
from gate.crypto import b64, decision_message, new_private_key, public_key_b64, sha256
from gate.devices import EmulatorDevice, PhoneDevice
from gate.main import STATE, Gate, enroll_phone, load_last_paid
inv, answer, mode = sys.argv[1:4]
if mode != "key":
    phone_key = new_private_key()
    enroll_phone("phone-replay", public_key_b64(phone_key))
if mode == "queue":
    import web.server as w
    print(json.dumps({"queue": w.phone_queue(), "devices": w.phone_devices()}))
    sys.exit()
screens, pending = [], {}
def ask(self, text):
    screens.append(text)
    return self.script.pop(0) if self.script else "timeout"
EmulatorDevice._ask = ask
class Phone(PhoneDevice):
    def show(self, req, text, cj):
        w = json.loads(cj).get("evidence", {}).get("witness") or {}
        pending.update(req=req, text=text, cj=cj, c=sha256(cj), exp=w.get("exp", -1), cov=w.get("cov", -1))
        th = sha256(text)
        msg = decision_message(req, pending["c"], th, answer, 1, pending["exp"], pending["cov"])
        return {"t": "decision", "req": req, "acct": "none", "ah": "", "warned": 0, "d": answer, "id": "phone-replay",
                "counter": 1, "th": th, "sig": b64(phone_key.sign(msg)), "exp": pending["exp"], "cov": pending["cov"]}
p = json.loads((STATE / "proposals.json").read_text())[inv]
gate = Gate(EmulatorDevice(script=[answer] * 4), allow_emulator=True, phone=Phone() if mode == "phone" else None,
            last_paid=load_last_paid() or None)
result = gate.process(p)
print(json.dumps({"screens": screens, "pending": pending or None, "result": result}))
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


def outcome(folder, inv, answer, mode="key"):
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "run"
        shutil.copytree(folder, work, ignore=shutil.ignore_patterns("world", "hmem", "llm_cache"))
        env = {**os.environ, "SAMPLE_MODE": "true", "DEVICE": "emulator", "HUMANKEY_LOGS": str(work),
               "HUMANKEY_STATE": str(work / "gate"), "HUMANKEY_EMU_DIR": str(work / "emulator")}
        n_events = len(jsonl(work / "events.jsonl"))
        r = subprocess.run([sys.executable, "-c", CHILD, inv, answer, mode], cwd=ROOT, env=env, capture_output=True,
                           text=True, timeout=120)
        if r.returncode:
            sys.exit(f"{inv} {answer}: {r.stderr[-400:]}")
        rec = json.loads(r.stdout.strip().splitlines()[-1])
        if mode == "queue":
            return rec
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


def phone_app():
    """The phone app's web build (mobile/, Expo) under site/phone/, with its replay layer, and a page framing it."""
    out = SITE / "phone"
    shutil.rmtree(out, ignore_errors=True)
    subprocess.run(["npx", "expo", "export", "--platform", "web", "--output-dir", str(out)], cwd=ROOT / "mobile",
                   check=True, capture_output=True, timeout=600)
    shutil.copy(ROOT / "web" / "phone-replay.js", out / "phone-replay.js")
    # In the replay the gate's answer is the recorded one, so the app's success line says so.
    said, honest = "Approved. The gate checked the signature and paid.", \
        "Approved. In the recorded run the gate checked the phone signature and paid."
    for js in (out / "_expo").rglob("*.js"):
        text = js.read_text()
        if said in text:
            js.write_text(text.replace(said, honest))
    index = (out / "index.html").read_text()
    (out / "index.html").write_text(index.replace("<head>", '<head><script src="phone-replay.js"></script>', 1))
    shutil.copy(ROOT / "web" / "phone.html", SITE / "phone.html")


def main():
    folder = sample_run()
    st = state(folder)
    outcomes = {}
    for it in st["items"]:
        inv = it["invoice_id"]
        answers = ["approve", "reject"] if it["route"] == "AWAITING_KEY" else ["approve"]
        outcomes[inv] = {a: outcome(folder, inv, a) for a in answers}
        print(inv, {a: o["result"] for a, o in outcomes[inv].items()})
    phone = outcome(folder, "", "", "queue")
    phone["outcomes"] = {}
    for it in phone["queue"]["items"]:
        if it["tier"] == "phone":
            inv = it["invoice_id"]
            phone["outcomes"][inv] = {a: outcome(folder, inv, a, "phone") for a in ("approve", "reject")}
            print(inv, "phone", {a: o["result"] for a, o in phone["outcomes"][inv].items()})
    SITE.mkdir(exist_ok=True)
    data = {"state": st, "outcomes": outcomes, "phone": phone, "run": folder.name}
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
    page = page.replace("Nothing is signed in this page. ", "Nothing is signed in this page. "
                        '<a href="phone.html">Open the phone app</a> · ', 1)
    (SITE / "index.html").write_text(page)
    phone_app()
    print(f"site/ written from {folder.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
