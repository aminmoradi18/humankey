"""Gate tests: nothing executes without a valid, fresh, device-signed approval.

Run: python3 -m unittest discover -s tests -t .
"""
import os
import tempfile
import unittest
from pathlib import Path

from common.jsonl import canonical, read
from gate.crypto import sha256
from gate.devices import EmulatorDevice
from gate.main import Gate

PROPOSAL = {
    "invoice_id": "INV9001", "vendor_id": "V001", "amount": 12500.0, "risk": "medium",
    "route": "AWAITING_KEY", "proposal": "escalate", "key_text": "INV9001 $12,500\nNEW bank account\nrisk medium\nPAY anyway?",
    "findings": {"bank_account": {"check": "bank_account", "signal": "risk_up"}},
}


class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["HUMANKEY_LOGS"] = str(self.tmp / "logs")

    def gate(self, script, allow=True):
        dev = EmulatorDevice(script=script)
        (self.tmp / "devices.json").write_text(canonical({"emulator": dev.public_key()}))
        return Gate(dev, allow_emulator=allow, state=self.tmp)

    def ledger(self):
        return read("ledger", self.tmp)

    def test_approve_executes_with_signed_record(self):
        self.assertEqual(self.gate(["approve"]).process(dict(PROPOSAL)), "EXECUTED")
        rec = self.ledger()[0]["approval"]
        self.assertEqual(rec["device_id"], "emulator")
        self.assertEqual(rec["text_sha256"], sha256(PROPOSAL["key_text"]))

    def test_reject_and_timeout_do_not_execute(self):
        self.assertEqual(self.gate(["reject"]).process(dict(PROPOSAL)), "REJECTED")
        self.assertEqual(self.gate([]).process(dict(PROPOSAL)), "TIMEOUT")
        self.assertEqual(self.ledger(), [])

    def test_emulator_refused_when_not_trusted(self):
        self.assertEqual(self.gate(["approve"], allow=False).process(dict(PROPOSAL)), "REFUSED (emulator_not_trusted)")
        self.assertEqual(self.ledger(), [])

    def test_forged_signature_refused(self):
        g = self.gate([])
        c = g.issue_contract(PROPOSAL)
        fake = {"d": "approve", "id": "emulator", "req": c["contractId"], "counter": 99,
                "th": sha256(PROPOSAL["key_text"]), "sig": "A" * 88}
        self.assertEqual(g.verify(c, PROPOSAL["key_text"], fake), "device signature invalid")

    def test_replayed_decision_refused(self):
        dev = EmulatorDevice(script=["approve"])
        (self.tmp / "devices.json").write_text(canonical({"emulator": dev.public_key()}))
        g = Gate(dev, allow_emulator=True, state=self.tmp)
        c = g.issue_contract(PROPOSAL)
        msg = dev.show(c["contractId"], PROPOSAL["key_text"], sha256(canonical(c)))
        self.assertIsNone(g.verify(c, PROPOSAL["key_text"], msg))
        g._save_counter("emulator", msg["counter"])
        self.assertEqual(g.verify(c, PROPOSAL["key_text"], msg), "replayed decision (counter not increasing)")

    def test_text_or_contract_tampering_refused(self):
        dev = EmulatorDevice(script=["approve", "approve"])
        (self.tmp / "devices.json").write_text(canonical({"emulator": dev.public_key()}))
        g = Gate(dev, allow_emulator=True, state=self.tmp)
        c = g.issue_contract(PROPOSAL)
        msg = dev.show(c["contractId"], "INV9001 $1\nall good\n\nPAY this?", sha256(canonical(c)))
        self.assertEqual(g.verify(c, PROPOSAL["key_text"], msg), "device showed different text")
        msg = dev.show(c["contractId"], PROPOSAL["key_text"], sha256(canonical(c)))
        c["intent"]["parameters"]["amount"] = 99999
        self.assertEqual(g.verify(c, PROPOSAL["key_text"], msg), "contract signature invalid")

    def test_blocked_never_reaches_device(self):
        p = dict(PROPOSAL, route="BLOCKED")
        self.assertEqual(self.gate(["approve"]).process(p), "BLOCKED")
        self.assertEqual(self.ledger(), [])


if __name__ == "__main__":
    unittest.main()
