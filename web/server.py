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
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from common.env import ROOT, load_env
from agent.agents import SOFT_CHECKS
from agent.world import World
from common.jsonl import append, read
from gate.devices import EmulatorDevice, PhoneDevice, open_device
from agent.tools import APPROVAL_THRESHOLD, AUTO_LIMIT
from gate.main import STATE, Gate, approval_tier, enroll_phone, load_last_paid, phone_fatigue

PORT = int(os.environ.get("WEB_PORT", "8700"))
HOST = os.environ.get("WEB_HOST", "127.0.0.1")  # 0.0.0.0 to reach it from a real phone on the LAN
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
phone = PhoneDevice()
PAIR_CODE = f"{secrets.randbelow(10**6):06d}"  # printed in the terminal; one phone pairs with it
pairing = {"used": False, "wrong": 0}
PAIR_TRIES = 5
CORS = os.environ.get("PHONE_WEB_CORS") == "1"  # only for the app's web build (screenshots), never on a LAN
last_paid = {}
gate_lock = threading.Lock()
busy = {"invoice": None}


def tiers():
    if not last_paid:
        last_paid.update(load_last_paid())
    props = json.loads((STATE / "proposals.json").read_text()) if (STATE / "proposals.json").exists() else {}
    t = {inv: approval_tier(p, last_paid) for inv, p in props.items() if p["route"] == "AWAITING_KEY"}
    if phone_fatigue(STATE):  # many fast phone approvals: the next ones wait for the key
        t = {inv: "hardware" for inv in t}
    return t


def phone_devices():
    """What the app's Profile tab shows: who may approve what, and the trusted devices (ids only)."""
    devices = json.loads((STATE / "devices.json").read_text()) if (STATE / "devices.json").exists() else {}
    counters = json.loads((STATE / "counters.json").read_text()) if (STATE / "counters.json").exists() else {}
    ledger = read("ledger", STATE)
    by = {}
    for x in ledger:
        who = x.get("released_by", "")
        kind = "auto" if who.startswith("policy") else "phone" if who.startswith("phone") else "key"
        by[kind] = by.get(kind, 0) + 1
    out = []
    for dev in sorted(devices):
        kind = "phone" if dev.startswith("phone-") else "emulator" if dev == "emulator" else "key"
        out.append({"id": dev, "kind": kind, "counter": counters.get(dev, 0),
                    "active": (kind == "phone") or (kind == "emulator") == (DEVICE_KIND == "emulator")})
    return {"device_kind": DEVICE_KIND, "devices": out, "paid": by, "fatigue": phone_fatigue(STATE),
            "limits": {"auto": AUTO_LIMIT, "phone": APPROVAL_THRESHOLD}}


def phone_queue():
    """What the phone app lists: everything waiting for a person, and which device may approve it."""
    st, t = state(), tiers()
    out = []
    for p in st["items"]:
        if p["invoice_id"] not in t:
            continue
        hdr = p["findings"].get("invoice_header", {}).get("facts", {})
        last = last_paid.get(p["vendor_id"])
        # The vendor master can be edited by an attacker; the payment history is what was actually paid.
        same = None if not (last and hdr.get("iban_hash")) else last == hdr["iban_hash"]
        out.append({"invoice_id": p["invoice_id"], "vendor_id": p["vendor_id"], "amount": p["amount"],
                    "last_paid_match": same, "ai": p.get("ai") or {},
                    "risk": p["risk"], "tier": t[p["invoice_id"]], "key_text": p["key_text"],
                    "explanation": p.get("explanation", ""), "subject": hdr.get("subject", ""),
                    "findings": [{"check": f["check"], "signal": f["signal"], "summary": f["summary"]}
                                 for f in p["findings"].values()],
                    "done": (p.get("paid") or {}).get("released_by") or (p.get("gate_event") or {}).get("kind")})
    pend = phone.pending and {**phone.pending, "invoice_id": busy["invoice"]}
    return {"items": out, "pending": pend, "busy": busy["invoice"]}


def state():
    proposals = json.loads((STATE / "proposals.json").read_text()) if (STATE / "proposals.json").exists() else {}
    ledger = read("ledger", STATE)
    events = read("events")
    paid = {x["invoice_id"]: x for x in ledger}
    last = {}
    for e in events:
        if e.get("kind") in ("blocked", "rejected", "timeout", "refused", "executed", "forgery_attempt",
                             "contract.refused"):
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
            result = Gate(device, allow_emulator=(DEVICE_KIND == "emulator"), phone=phone,
                          last_paid=last_paid or None).process(p)
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
        if CORS and self.path.startswith("/api/phone/"):
            self.send_header("Access-Control-Allow-Origin", "*")  # the app's web build runs on another port
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/state":
            return self._send(200, state())
        if self.path == "/api/phone/queue":
            return self._send(200, phone_queue())
        if self.path == "/api/phone/devices":
            return self._send(200, phone_devices())
        self._send(404, {"error": "not found"})

    def do_OPTIONS(self):
        self.send_response(204)
        if CORS and self.path.startswith("/api/phone/"):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

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
        if self.path == "/api/phone/pair":
            pid, pub = str(body.get("id", "")), str(body.get("pub", ""))
            if pairing["used"] or pairing["wrong"] >= PAIR_TRIES:
                return self._send(403, {"error": "pairing closed: restart the server for a new code"})
            if not secrets.compare_digest(str(body.get("code", "")), PAIR_CODE):
                pairing["wrong"] += 1
                return self._send(403, {"error": "wrong pairing code"})
            if not pid.startswith("phone-") or len(pub) != 44:
                return self._send(400, {"error": "bad phone id or key"})
            enroll_phone(pid, pub)
            pairing["used"] = True
            return self._send(200, {"ok": True, "id": pid})
        if self.path == "/api/phone/release":
            inv = body.get("invoice_id", "")
            if tiers().get(inv) != "phone":
                return self._send(403, {"error": "this payment needs the hardware key"})
            if gate_lock.locked():
                return self._send(409, {"error": f"busy with {busy['invoice']}"})
            threading.Thread(target=release, args=(inv,), daemon=True).start()
            return self._send(202, {"ok": True})
        if self.path == "/api/phone/decision":
            if not phone.answer(body):
                return self._send(409, {"error": "nothing waiting for this phone"})
            return self._send(200, {"ok": True})
        if self.path == "/api/emulator" and isinstance(device, WebEmulator):
            if device.pending is None:
                return self._send(409, {"error": "nothing on the key"})
            device.answers.put({"approve": "approve", "reject": "reject"}.get(body.get("decision"), "timeout"))
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})


if __name__ == "__main__":
    if HOST not in ("127.0.0.1", "localhost") and DEVICE_KIND == "emulator":
        # The browser emulator approves anything; it must never be reachable from the network.
        raise SystemExit("WEB_HOST beyond localhost needs DEVICE=serial (the hardware key)")
    print(f"HumanKey on http://{HOST}:{PORT}  (device: {DEVICE_KIND})  phone pairing code: {PAIR_CODE}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
