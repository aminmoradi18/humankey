"""The gate: the only process that can execute a payment (ARCHITECTURE.md section 6).

It reads proposals, issues a short-lived signed contract for each payment that
needs a human, shows the key text on the device, and verifies the device's
signed answer before appending to the ledger. Payments are simulated: execute
means one line in gate/state/ledger.jsonl.

Usage:
  python3 -m gate.main enroll-emulator          register the emulator's public key
  python3 -m gate.main enroll-serial            register the hardware key's public key (USB)
  python3 -m gate.main enroll-phone ID PUB      register a paired phone (normally done by the web pairing code)
  python3 -m gate.main import-accounts          load paid-to accounts into the key's own ledger
  python3 -m gate.main night                    issue the night contracts for payments waiting for a person
  python3 -m gate.main run [--allow-emulator]   process gate/state/proposals.json
  python3 -m gate.main forge INV0008            try to approve without the device (must be refused)
  python3 -m gate.main forge-spec INV0008       write the spec's plain {kind: user-confirm} approval (must be refused)
  python3 -m gate.main tamper INV0011           attack demo: change a waiting payment after its night contract
"""
import json
import os
from pathlib import Path
import sys
import time

from agent.agents import event
from agent.tools import AUTO_LIMIT
from common.env import ROOT, load_env
from common.jsonl import append, canonical, read
from gate.crypto import contract_ok, contract_secret, decision_message, sha256, verify_ed25519
from gate import scout
from gate.devices import EmulatorDevice, open_device

STATE = Path(os.environ.get("HUMANKEY_STATE", ROOT / "gate" / "state"))
PHONE_LIMIT = 10000.0
LAST_PAID_SQL = "SELECT vendor_id, iban_hash, iban_last4, paid_at FROM northwind_erp.payments ORDER BY paid_at"


def approval_tier(p, last_paid):
    """'phone' only when nothing at all points at risk; everything else needs the hardware key.

    The phone has no private account ledger, so the gate itself checks the one thing the key's
    ledger would: the invoice pays the account this vendor was last actually paid to (payment
    history, not the vendor master, which an attacker may have edited).
    """
    hdr = p["findings"].get("invoice_header", {}).get("facts", {})
    clean = (p["route"] == "AWAITING_KEY" and (p.get("amount") or 0) < PHONE_LIMIT
             and not any(f.get("signal") in ("risk_up", "unknown") for f in p["findings"].values())
             and not p.get("injection_flags")
             and bool(hdr.get("iban_hash")) and last_paid.get(p["vendor_id"]) == hdr.get("iban_hash"))
    return "phone" if clean else "hardware"


def load_last_paid():
    """vendor -> iban hash of the last payment, read through Zetaris. Empty on failure (all key)."""
    try:
        from zetaris.client import ZetarisClient
        rows, _ = ZetarisClient().query(LAST_PAID_SQL, purpose="phone tier: last paid account", agent="gate")
    except Exception:
        return {}
    return {r["vendor_id"]: r["iban_hash"] for r in rows or []}


def load_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


class Gate:
    def __init__(self, device=None, allow_emulator=False, state=STATE, phone=None, last_paid=None):
        load_env()
        self.state = state
        state.mkdir(parents=True, exist_ok=True)
        self.secret = contract_secret(state / "contract.secret")
        self.devices = load_json(state / "devices.json", {})
        self.counters = load_json(state / "counters.json", {})
        self.device = device
        self.allow_emulator = allow_emulator
        self.phone = phone
        self.last_paid = last_paid

    # -- contract ------------------------------------------------------
    def issue_contract(self, p, parent=None, tier="hardware", reason=None, approvals=(), stage="morning"):
        c = scout.issue(p, self.secret, tier, parent=parent, reason=reason, approvals=approvals, stage=stage)
        append("contracts", {"contract": c}, self.state)  # wrapped: the log adds a timestamp
        event("contract.issued", p["invoice_id"], agent="gate", contract=c["contractId"], parent=parent,
              reason=reason, require=c["risk"]["requireApproval"])
        return c

    def contracts(self):
        return [x["contract"] for x in read("contracts", self.state)]

    def night_contract(self, inv):
        night = [c for c in self.contracts() if c["traceId"] == inv and c.get("stage") == "night"]
        return night[-1] if night else None

    def morning(self, p, tier):
        """Re-plan the night contract for the approval, or refuse if the evidence changed overnight."""
        night = self.night_contract(p["invoice_id"])
        if night is None:
            return self.issue_contract(p, tier=tier), None
        if not contract_ok(night, self.secret):
            return None, "night contract signature invalid"
        if night["evidence"]["digest"] != scout.evidence(p)["digest"]:
            return None, "evidence changed since the night contract"
        if scout.expired(night):
            event("contract.expired", p["invoice_id"], agent="gate", contract=night["contractId"])
            reason = f"parent expired at {night['expiresAt']}; fresh decision for the approval"
        else:
            reason = "approval requested"
        # the tier is decided now, from today's payment history; the night contract does not fix it
        return self.issue_contract(p, parent=night["contractId"], tier=tier, reason=reason), None

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
        if dev.startswith("phone-") and contract["risk"]["requireApproval"] != scout.KINDS["phone"]:
            return "a phone cannot approve a hardware-key payment"
        if not contract_ok(contract, self.secret):
            return "contract signature invalid"
        if scout.expired(contract):
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
            # The gate keeps its own limit: an agent that marks a large payment "auto" still needs a person.
            if (p.get("amount") or 0) < AUTO_LIMIT:
                self.execute(p, f"policy:auto (clear, low risk, under {AUTO_LIMIT:,.0f})")
                return "EXECUTED"
            event("auto_refused", inv, agent="gate", amount=p.get("amount"))
            p = dict(p, route="AWAITING_KEY")
        tier = "hardware"
        if self.phone is not None and any(d.startswith("phone-") for d in self.devices):
            if self.last_paid is None:
                self.last_paid = load_last_paid()
            tier = approval_tier(p, self.last_paid)
        device = self.phone if tier == "phone" else self.device
        contract, why = self.morning(p, tier)
        if contract is None:
            event("contract.refused", inv, agent="gate", reason=why)
            return f"REFUSED ({why})"
        c_sha = sha256(canonical(contract))
        event("sent_to_device", inv, agent="gate", contract=contract["contractId"], text=p["key_text"], tier=tier)
        w = p.get("witness")
        hdr = p["findings"].get("invoice_header", {}).get("facts", {})
        acct = {"v": p["vendor_id"], "h": hdr.get("iban_hash", ""), "l4": hdr.get("iban_last4", "")} \
            if hdr.get("iban_hash") else None
        msg = device.show(contract["contractId"], p["key_text"], c_sha, w=w, acct=acct)
        reason = self.verify(contract, p["key_text"], msg, sent=(w["exp"], w["cov"]) if w else (-1, -1))
        if msg.get("warned"):
            event("key_warning", inv, agent="key", count=f"{msg.get('cov')}/{msg.get('exp')}",
                  account=msg.get("acct"), decision=msg.get("d"))
        self._save_counter(msg.get("id"), msg.get("counter"))
        if reason is None:
            # The approval goes into a new contract (contracts are immutable once signed), and the
            # executor checks that one on its own, signature included.
            record = scout.approval_record(contract, msg, tier)
            approved = self.issue_contract(p, parent=contract["contractId"], tier=tier,
                                           reason="approval recorded", approvals=[record], stage="approved")
            reason = scout.check(approved, self.secret, self.devices, contract)
        if reason is None:
            event("contract.verified", inv, agent="gate", contract=approved["contractId"], engine="executor")
            approval = {"kind": record["kind"], "device_id": msg["id"], "contract_sha256": c_sha,
                        "text_sha256": msg["th"], "decision": "approve", "counter": msg["counter"],
                        "count_witness": {"expected": msg.get("exp"), "checked": msg.get("cov")},
                        "account_on_key": msg.get("acct"), "signature": msg["sig"],
                        "chain": [contract.get("parentId"), contract["contractId"], approved["contractId"]],
                        "contract": approved}
            self.execute(p, f"{'phone' if tier == 'phone' else 'human key'} ({msg['id']})", approval)
            return "EXECUTED"
        state = {"reject": "REJECTED", "timeout": "TIMEOUT"}.get(msg.get("d"), "REFUSED")
        event(state.lower(), inv, agent="gate", reason=reason)
        if state == "REFUSED":
            event("contract.refused", inv, agent="gate", contract=contract["contractId"], reason=reason)
        return state if state != "REFUSED" else f"REFUSED ({reason})"


def enroll_phone(phone_id, pub, state=STATE):
    if not phone_id.startswith("phone-"):
        raise ValueError("phone ids start with phone-")
    devices = load_json(state / "devices.json", {})
    devices[phone_id] = pub
    state.mkdir(parents=True, exist_ok=True)
    (state / "devices.json").write_text(canonical(devices))


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
    elif cmd == "enroll-phone":
        enroll_phone(argv[1], argv[2])
        print(f"enrolled {argv[1]}")
    elif cmd == "import-accounts":
        # One-time: give the key the account each vendor was actually PAID to (payment history),
        # not the current vendor master; the person confirms the import on the key.
        from zetaris.client import ZetarisClient
        rows, _ = ZetarisClient().query(LAST_PAID_SQL, purpose="account import from payment history", agent="gate")
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
    elif cmd == "night":
        # Right after the agents finish: one contract per payment that waits for a person, bound to
        # the evidence the person will see. In the morning it has expired and is re-planned.
        gate = Gate(allow_emulator=True)
        n = 0
        for inv, p in sorted(load_json(STATE / "proposals.json", {}).items()):
            if p["route"] == "AWAITING_KEY" or (p["route"] == "QUEUED_AUTO" and (p.get("amount") or 0) >= AUTO_LIMIT):
                gate.issue_contract(p, stage="night")
                n += 1
        print(f"night contracts issued: {n} (valid {scout.TTL_S // 60} min; re-planned at approval)")
    elif cmd == "forge-spec":
        # Software writes the spec's own approval record into a contract. An executor that only checks
        # that an approval is present would pay; this one wants the device's signature.
        gate = Gate(allow_emulator=True)
        p = load_json(STATE / "proposals.json", {})[argv[1]]
        parent = gate.issue_contract(p, reason="forged approval test")
        plain = {"kind": "user-confirm", "at": scout.iso(time.time()), "confirmation": "yes"}
        forged = gate.issue_contract(p, parent=parent["contractId"], reason="approval recorded", approvals=[plain],
                                     stage="approved")
        reason = scout.check(forged, gate.secret, gate.devices, parent)
        event("forgery_attempt", p["invoice_id"], agent="gate", refused=reason, record="user-confirm")
        print(f"spec-style approval for {argv[1]}: {'REFUSED: ' + reason if reason else 'ACCEPTED (bug!)'}")
    elif cmd == "tamper":
        # Attack demo: something on the laptop edits a waiting payment overnight (amount and screen text),
        # after the night contract bound its evidence. The morning approval must stop.
        path = STATE / "proposals.json"
        props = load_json(path, {})
        p = props[argv[1]]
        old, new = p["amount"], round(p["amount"] * 1.9, 2)
        p["key_text"] = p["key_text"].replace(f"${old:,.0f}", f"${new:,.0f}")
        p["amount"] = new
        p["findings"]["invoice_header"]["facts"]["amount"] = new
        path.write_text(canonical(props))
        event("tampered", argv[1], agent="attacker", amount_before=old, amount_after=new)
        print(f"{argv[1]}: amount changed {old:,.2f} -> {new:,.2f} after the night contract")
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
