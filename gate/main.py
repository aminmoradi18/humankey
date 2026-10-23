"""The gate: the only process that can execute a payment (ARCHITECTURE.md section 6).

It reads proposals, issues a short-lived signed contract for each payment that
needs a human, shows the key text on the device, and verifies the device's
signed answer before appending to the ledger. Payments are simulated: execute
means one line in gate/state/ledger.jsonl.

Usage:
  python3 -m gate.main enroll-emulator          register the emulator's public key
  python3 -m gate.main enroll-serial            register the hardware key's public key (USB)
  python3 -m gate.main import-accounts          load paid-to accounts into the key's own ledger
  python3 -m gate.main run [--allow-emulator]   process gate/state/proposals.json
  python3 -m gate.main forge INV0008            try to approve without the device (must be refused)
"""
import json
import os
from pathlib import Path
import sys
import time
import uuid

from agent.agents import event
from common.env import ROOT, load_env
from common.jsonl import append, canonical, read
from gate.crypto import contract_ok, contract_secret, decision_message, sha256, sign_contract, verify_ed25519
from gate.devices import EmulatorDevice, open_device

STATE = Path(os.environ.get("HUMANKEY_STATE", ROOT / "gate" / "state"))
TTL_S = 300
POLICY_VERSION = "humankey-policy-1"


def load_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


class Gate:
    def __init__(self, device=None, allow_emulator=False, state=STATE):
        load_env()
        self.state = state
        state.mkdir(parents=True, exist_ok=True)
        self.secret = contract_secret(state / "contract.secret")
        self.devices = load_json(state / "devices.json", {})
        self.counters = load_json(state / "counters.json", {})
        self.device = device
        self.allow_emulator = allow_emulator

    # -- contract ------------------------------------------------------
    def issue_contract(self, p, parent=None):
        now = time.time()
        c = {
            "contractId": "ctr_" + uuid.uuid4().hex[:12], "parentId": parent, "traceId": p["invoice_id"],
            "intent": {"primary": "payment.release", "confidence": 1.0,
                       "parameters": {"invoice_id": p["invoice_id"], "vendor_id": p["vendor_id"],
                                      "amount": p["amount"]}},
            "risk": {"level": p["risk"], "flags": [f["check"] for f in p["findings"].values()
                                                    if f["signal"] in ("risk_up", "unknown")],
                     "approvals": ["hardware-confirm"]},
            "scope": {"user": "ap-manager", "surface": "humankey", "capabilities": ["payment.release"]},
            "issuedAt": int(now), "expiresAt": int(now + TTL_S), "policyPackVersion": POLICY_VERSION,
            "signedBy": "gate",
        }
        c["signature"] = sign_contract(c, self.secret)
        return c

    # -- verification --------------------------------------------------
    def verify(self, contract, text, msg, sent=None):
        """Return None if the approval is valid, else the refusal reason."""
        if msg.get("d") != "approve":
            return f"device answered {msg.get('d')}"
        dev = msg.get("id")
        if dev not in self.devices:
            return f"unknown device {dev!r}"
        if dev == "emulator" and not self.allow_emulator:
            return "emulator_not_trusted"
        if not contract_ok(contract, self.secret):
            return "contract signature invalid"
        if time.time() > contract["expiresAt"]:
            return "contract expired"
        if msg.get("th") != sha256(text):
            return "device showed different text"
        c_sha = sha256(canonical(contract))
        if not verify_ed25519(self.devices[dev], decision_message(
                msg["req"], c_sha, msg["th"], "approve", msg["counter"], msg.get("exp", -1), msg.get("cov", -1),
                msg.get("acct", "none"), msg.get("ah", "")), msg.get("sig", "")):
            return "device signature invalid"
        if sent is not None and (msg.get("exp", -1), msg.get("cov", -1)) != sent:
            return "device saw a different count than the gate sent"
        if msg["req"] != contract["contractId"]:
            return "decision is for another contract"
        if int(msg["counter"]) <= int(self.counters.get(dev, 0)):
            return "replayed decision (counter not increasing)"
        return None

    def _save_counter(self, dev, counter):
        if dev and counter is not None:
            self.counters[dev] = max(int(counter), int(self.counters.get(dev, 0)))
            (self.state / "counters.json").write_text(canonical(self.counters))

    def execute(self, p, how, approval=None):
        line = {"invoice_id": p["invoice_id"], "vendor_id": p["vendor_id"], "amount": p["amount"],
                "released_by": how, "approval": approval}
        append("ledger", line, self.state)
        event("executed", p["invoice_id"], agent="gate", released_by=how)

    # -- main flow -----------------------------------------------------
    def process(self, p):
        inv = p["invoice_id"]
        if any(x["invoice_id"] == inv for x in read("ledger", self.state)):
            return "ALREADY_EXECUTED"
        if p["route"] == "BLOCKED":
            event("blocked", inv, agent="gate")
            return "BLOCKED"
        if p["route"] == "QUEUED_AUTO":
            self.execute(p, "policy:auto (clear, low risk, under threshold)")
            return "EXECUTED"
        contract = self.issue_contract(p)
        c_sha = sha256(canonical(contract))
        event("sent_to_device", inv, agent="gate", contract=contract["contractId"], text=p["key_text"])
        w = p.get("witness")
        hdr = p["findings"].get("invoice_header", {}).get("facts", {})
        acct = {"v": p["vendor_id"], "h": hdr.get("iban_hash", ""), "l4": hdr.get("iban_last4", "")} \
            if hdr.get("iban_hash") else None
        msg = self.device.show(contract["contractId"], p["key_text"], c_sha, w=w, acct=acct)
        reason = self.verify(contract, p["key_text"], msg, sent=(w["exp"], w["cov"]) if w else (-1, -1))
        if msg.get("warned"):
            event("key_warning", inv, agent="key", count=f"{msg.get('cov')}/{msg.get('exp')}",
                  account=msg.get("acct"), decision=msg.get("d"))
        self._save_counter(msg.get("id"), msg.get("counter"))
        if reason is None:
            approval = {"kind": "hardware-confirm", "device_id": msg["id"], "contract_sha256": c_sha,
                        "text_sha256": msg["th"], "decision": "approve", "counter": msg["counter"],
                        "count_witness": {"expected": msg.get("exp"), "checked": msg.get("cov")},
                        "account_on_key": msg.get("acct"),
                        "signature": msg["sig"], "contract": contract}
            self.execute(p, f"human key ({msg['id']})", approval)
            return "EXECUTED"
        state = {"reject": "REJECTED", "timeout": "TIMEOUT"}.get(msg.get("d"), "REFUSED")
        event(state.lower(), inv, agent="gate", reason=reason)
        return state if state != "REFUSED" else f"REFUSED ({reason})"


def main(argv):
    cmd = argv[0] if argv else "run"
    if cmd == "enroll-emulator":
        devices = load_json(STATE / "devices.json", {})
        devices["emulator"] = EmulatorDevice().public_key()
        STATE.mkdir(parents=True, exist_ok=True)
        (STATE / "devices.json").write_text(canonical(devices))
        print("emulator enrolled")
    elif cmd == "enroll-serial":
        # Ask the board for its public key once, over USB, and trust it from now on.
        from gate.devices import SerialDevice
        dev = SerialDevice()
        pong = dev.ping()
        if not pong or not pong.get("pub"):
            sys.exit("no answer from the key on " + dev.port)
        devices = load_json(STATE / "devices.json", {})
        devices[pong["id"]] = pong["pub"]
        STATE.mkdir(parents=True, exist_ok=True)
        (STATE / "devices.json").write_text(canonical(devices))
        print(f"enrolled {pong['id']} (counter {pong['counter']})")
    elif cmd == "import-accounts":
        # One-time: give the key the account each vendor was actually PAID to (payment history),
        # not the current vendor master; the person confirms the import on the key.
        from zetaris.client import ZetarisClient
        rows, _ = ZetarisClient().query(
            "SELECT vendor_id, iban_hash, iban_last4, paid_at FROM northwind_erp.payments ORDER BY paid_at",
            purpose="account import from payment history", agent="gate")
        last = {}
        for r in rows:
            last[r["vendor_id"]] = {"v": r["vendor_id"], "h": r["iban_hash"], "l4": r["iban_last4"]}
        print(open_device().import_accounts(list(last.values())))
    elif cmd == "run":
        gate = Gate(open_device(), allow_emulator="--allow-emulator" in argv)
        proposals = load_json(STATE / "proposals.json", {})
        only = [a for a in argv[1:] if not a.startswith("--")]
        for inv, p in sorted(proposals.items()):
            if only and inv not in only:
                continue
            print(f"{inv}: {p['route']:12} -> {gate.process(p)}")
    elif cmd == "forge":
        # An attacker on the laptop writes an approval without the device's private key.
        gate = Gate(allow_emulator=True)
        p = load_json(STATE / "proposals.json", {})[argv[1]]
        contract = gate.issue_contract(p)
        fake = {"t": "decision", "req": contract["contractId"], "d": "approve", "counter": 10**6,
                "th": sha256(p["key_text"]), "id": "emulator", "sig": "A" * 88}
        reason = gate.verify(contract, p["key_text"], fake)
        event("forgery_attempt", p["invoice_id"], agent="gate", refused=reason)
        print(f"forged approval for {argv[1]}: {'REFUSED: ' + reason if reason else 'ACCEPTED (bug!)'}")


if __name__ == "__main__":
    main(sys.argv[1:])
