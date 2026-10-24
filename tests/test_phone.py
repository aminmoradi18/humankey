"""Phone tier: the phone may approve only payments where nothing points at risk.

Run: python3 -m unittest discover -s tests -t .
"""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from common.jsonl import canonical, read
from gate.crypto import b64, decision_message, new_private_key, public_key_b64, sha256
from gate.devices import EmulatorDevice, PhoneDevice
from gate.main import Gate, approval_tier


def finding(check, signal, **facts):
    return {"check": check, "signal": signal, "summary": check, "facts": facts, "receipt_ids": []}


def proposal(amount=4200.0, iban="aa11", signals=(), flags=()):
    f = {"invoice_header": finding("invoice_header", "neutral", amount=amount, iban_hash=iban, iban_last4="1111"),
         "bank_account": finding("bank_account", "risk_down"),
         "purchase_order": finding("purchase_order", "risk_down")}
    for check in signals:
        f[check] = finding(check, "risk_up")
    return {"invoice_id": "INV7001", "vendor_id": "V001", "amount": amount, "risk": "medium",
            "route": "AWAITING_KEY", "proposal": "escalate", "key_text": "INV7001 $4,200\nPAY?",
            "findings": f, "injection_flags": list(flags)}


LAST_PAID = {"V001": "aa11"}


class Phone:
    """Stands in for the app: signs on its own key, like mobile/ does with tweetnacl."""

    def __init__(self, d="approve", counter=1):
        self.key, self.d, self.counter = new_private_key(), d, counter
        self.id = "phone-test"

    def show(self, req, text, contract_sha, w=None, acct=None):
        th = sha256(text)
        sig = b64(self.key.sign(decision_message(req, contract_sha, th, self.d, self.counter)))
        return {"t": "decision", "req": req, "d": self.d, "counter": self.counter, "th": th,
                "id": self.id, "sig": sig, "exp": -1, "cov": -1, "acct": "none", "ah": ""}


class TierTest(unittest.TestCase):
    def test_only_fully_clean_small_payments_go_to_the_phone(self):
        self.assertEqual(approval_tier(proposal(), LAST_PAID), "phone")
        self.assertEqual(approval_tier(proposal(amount=10000.0), LAST_PAID), "hardware")
        self.assertEqual(approval_tier(proposal(signals=["split_pattern"]), LAST_PAID), "hardware")
        self.assertEqual(approval_tier(proposal(flags=[{"by": "rules"}]), LAST_PAID), "hardware")
        # vendor master edited to the attacker's account: matches the master, not the payment history
        self.assertEqual(approval_tier(proposal(iban="evil"), LAST_PAID), "hardware")
        self.assertEqual(approval_tier(proposal(), {}), "hardware")
        self.assertEqual(approval_tier(dict(proposal(), route="QUEUED_AUTO"), LAST_PAID), "hardware")


class PhoneGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["HUMANKEY_LOGS"] = str(self.tmp / "logs")

    def gate(self, phone, key_script=()):
        key = EmulatorDevice(script=list(key_script))
        devices = {"emulator": key.public_key(), phone.id: public_key_b64(phone.key)}
        (self.tmp / "devices.json").write_text(canonical(devices))
        return Gate(key, allow_emulator=True, state=self.tmp, phone=phone, last_paid=dict(LAST_PAID))

    def test_phone_approves_clean_small_payment(self):
        self.assertEqual(self.gate(Phone()).process(proposal()), "EXECUTED")
        rec = read("ledger", self.tmp)[0]
        self.assertEqual((rec["released_by"], rec["approval"]["kind"]), ("phone (phone-test)", "phone-signature"))

    def test_risky_payment_goes_to_the_key_not_the_phone(self):
        g = self.gate(Phone(), key_script=["reject"])
        self.assertEqual(g.process(proposal(signals=["amount_history"])), "REJECTED")

    def test_phone_signature_on_hardware_contract_refused(self):
        ph = Phone()
        g = self.gate(ph)
        p = proposal(amount=25000.0)
        c = g.issue_contract(p, tier="hardware")
        msg = ph.show(c["contractId"], p["key_text"], sha256(canonical(c)))
        self.assertEqual(g.verify(c, p["key_text"], msg), "a phone cannot approve a hardware-key payment")

    def test_gate_refuses_auto_pay_at_or_over_the_auto_limit(self):
        g = self.gate(Phone())
        p = dict(proposal(amount=7000.0), route="QUEUED_AUTO", proposal="clear", invoice_id="INV7002")
        self.assertEqual(g.process(p), "EXECUTED")
        self.assertEqual(read("ledger", self.tmp)[0]["released_by"], "phone (phone-test)")
        small = dict(proposal(amount=1500.0), route="QUEUED_AUTO", proposal="clear", invoice_id="INV7003")
        self.assertEqual(g.process(small), "EXECUTED")
        self.assertTrue(read("ledger", self.tmp)[1]["released_by"].startswith("policy:auto"))

    def test_forged_and_replayed_phone_decisions_refused(self):
        ph = Phone()
        g = self.gate(ph)
        p = proposal()
        c = g.issue_contract(p, tier="phone")
        msg = dict(ph.show(c["contractId"], p["key_text"], sha256(canonical(c))), sig="A" * 88)
        self.assertEqual(g.verify(c, p["key_text"], msg), "device signature invalid")
        g.counters[ph.id] = 5
        msg = ph.show(c["contractId"], p["key_text"], sha256(canonical(c)))
        self.assertEqual(g.verify(c, p["key_text"], msg), "replayed decision (counter not increasing)")

    def test_phone_relay_passes_the_apps_answer_through(self):
        dev, ph = PhoneDevice(timeout=5), Phone()
        out = {}
        t = threading.Thread(target=lambda: out.update(dev.show("ctr_1", "text", "c" * 64)))
        t.start()
        while dev.pending is None:
            time.sleep(0.01)
        self.assertFalse(dev.answer({"req": "ctr_other", "d": "approve"}))
        self.assertTrue(dev.answer({"req": "ctr_1", "d": "reject", "id": ph.id, "counter": 2}))
        t.join()
        self.assertEqual((out["d"], out["id"], out["req"]), ("reject", "phone-test", "ctr_1"))


if __name__ == "__main__":
    unittest.main()


class PairingTest(unittest.TestCase):
    def test_pairing_locks_after_wrong_codes(self):
        import json
        import urllib.error
        import urllib.request
        from http.server import ThreadingHTTPServer
        from web import server
        srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        saved = dict(server.pairing)

        def pair(code):
            req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/api/phone/pair", method="POST",
                                         data=json.dumps({"code": code, "id": "phone-x", "pub": "A" * 44}).encode())
            try:
                return urllib.request.urlopen(req).status
            except urllib.error.HTTPError as e:
                return e.code
        try:
            server.pairing.update(used=False, wrong=0)
            wrong = "000000" if server.PAIR_CODE != "000000" else "111111"
            self.assertEqual([pair(wrong) for _ in range(server.PAIR_TRIES)], [403] * server.PAIR_TRIES)
            self.assertEqual(pair(server.PAIR_CODE), 403)  # even the right code: pairing is closed now
            self.assertFalse(server.pairing["used"])
        finally:
            server.pairing.update(saved)
            srv.shutdown()
            srv.server_close()
