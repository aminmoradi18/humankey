"""HumanKey web view: the queue, the evidence behind each decision, and the key.

Usage: python3 -m web.server            then open http://127.0.0.1:8700
       DEVICE=serial python3 -m web.server   to use the hardware key instead of the emulator

Read-only views come from gate/state and logs/. "Release" sends one payment
through the gate; when it needs a human, the device (or the clearly labelled
browser emulator) must answer. The browser never signs anything itself: the
emulator's private key stays in this process, like the board's stays on the board.
"""
import json
import os
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from common.env import ROOT, load_env
from agent.agents import SOFT_CHECKS
from agent.world import World
from common.jsonl import append, read
from gate.devices import EmulatorDevice, open_device
from gate.main import STATE, Gate

PORT = int(os.environ.get("WEB_PORT", "8700"))
HERE = Path(__file__).resolve().parent


class WebEmulator(EmulatorDevice):
    """The emulator, answered from the browser instead of the terminal."""

    def __init__(self):
        super().__init__(script=None)
        self.pending = None
        self.answers = queue.Queue()

    def _ask(self, text):
        self.pending = text
        try:
            return self.answers.get(timeout=self.timeout)
        except queue.Empty:
            return "timeout"
        finally:
            self.pending = None


load_env()
DEVICE_KIND = os.environ.get("DEVICE", "emulator")
device = WebEmulator() if DEVICE_KIND == "emulator" else open_device("serial")
gate_lock = threading.Lock()
busy = {"invoice": None}


def state():
    proposals = json.loads((STATE / "proposals.json").read_text()) if (STATE / "proposals.json").exists() else {}
    ledger = read("ledger", STATE)
    events = read("events")
    paid = {x["invoice_id"]: x for x in ledger}
    last = {}
    for e in events:
        if e.get("kind") in ("blocked", "rejected", "timeout", "refused", "executed", "forgery_attempt"):
            last[e["invoice_id"]] = e
    receipts = {r["receipt_id"]: r for r in read("receipts")}
    llm = read("llm")
    items = []
    for inv, p in sorted(proposals.items()):
        rids = {i for f in p["findings"].values() for i in f["receipt_ids"]}
        items.append({**p, "paid": paid.get(inv), "gate_event": last.get(inv),
                      "receipts": [receipts[i] for i in rids if i in receipts]})
    models = {}
    for x in llm:
        if x.get("ok"):
            models[x["model"]] = models.get(x["model"], 0) + 1
    return {"items": items, "soft_checks": sorted(SOFT_CHECKS), "device": {"kind": DEVICE_KIND, "pending": getattr(device, "pending", None),
                                       "busy": busy["invoice"]},
            "models": models, "events": events[-60:]}


def release(invoice_id):
    p = json.loads((STATE / "proposals.json").read_text())[invoice_id]
    with gate_lock:
        busy["invoice"] = invoice_id
        try:
            result = Gate(device, allow_emulator=(DEVICE_KIND == "emulator")).process(p)
        finally:
            busy["invoice"] = None
    # The human's answer grades any memory that settled a finding (H-MEM feedback).
    kind = {"EXECUTED": "helpful", "REJECTED": "wrong"}.get(result)
    if kind and p.get("memory"):
        w = World()
        for m in p["memory"]:
            w.feedback(m["id"], kind)
            append("events", {"kind": "memory_feedback", "invoice_id": invoice_id, "agent": "human",
                              "memory": m["id"], "feedback": kind})
    return result


def remember(invoice_id, check, content):
    p = json.loads((STATE / "proposals.json").read_text())[invoice_id]
    rec = World().remember(p["vendor_id"], check, content, "ap-manager", invoice_id)
    append("events", {"kind": "remembered", "invoice_id": invoice_id, "agent": "human", "check": check,
                      "memory": rec["id"], "content": content})
    return rec


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/state":
            return self._send(200, state())
        self._send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/api/release":
            inv = body.get("invoice_id", "")
            if gate_lock.locked():
                return self._send(409, {"error": f"the key is busy with {busy['invoice']}"})
            threading.Thread(target=release, args=(inv,), daemon=True).start()
            return self._send(202, {"ok": True})
        if self.path == "/api/remember":
            check, content = body.get("check", ""), str(body.get("content", "")).strip()[:400]
            if check not in SOFT_CHECKS or not content:
                return self._send(400, {"error": "pick a check this answer explains and write the reason"})
            return self._send(200, remember(body.get("invoice_id", ""), check, content))
        if self.path == "/api/emulator" and isinstance(device, WebEmulator):
            if device.pending is None:
                return self._send(409, {"error": "nothing on the key"})
            device.answers.put({"approve": "approve", "reject": "reject"}.get(body.get("decision"), "timeout"))
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})


if __name__ == "__main__":
    print(f"HumanKey on http://127.0.0.1:{PORT}  (device: {DEVICE_KIND})")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
