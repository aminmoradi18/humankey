"""Scout execution contracts: night -> morning -> approved, and only a device signature approves.

Run: python3 -m unittest discover -s tests -t .
"""
import os
import tempfile
import time
import unittest
from pathlib import Path

from common.jsonl import append, canonical, read
from gate import scout
from gate.devices import EmulatorDevice
from gate.main import Gate
from tests.test_gate import PROPOSAL


class ScoutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["HUMANKEY_LOGS"] = str(self.tmp / "logs")

    def gate(self, script=()):
        dev = EmulatorDevice(script=list(script))
        (self.tmp / "devices.json").write_text(canonical({"emulator": dev.public_key()}))
        return Gate(dev, allow_emulator=True, state=self.tmp)

    def approved_chain(self):
        g = self.gate(["approve"])
        self.assertEqual(g.process(dict(PROPOSAL)), "EXECUTED")
        return g, read("ledger", self.tmp)[0]["approval"]

    def test_device_approval_rides_in_a_child_contract(self):
        g, rec = self.approved_chain()
        approved = rec["contract"]
        morning = next(c for c in g.contracts() if c["contractId"] == approved["parentId"])
        self.assertEqual((approved["stage"], morning["stage"]), ("approved", "morning"))
        self.assertEqual(approved["traceId"], morning["traceId"])
        self.assertEqual(approved["risk"]["approvals"][0]["kind"], "device-signature")
        self.assertIsNone(scout.check(approved, g.secret, g.devices, morning))

    def test_spec_plain_approval_is_refused(self):
        g = self.gate()
        parent = g.issue_contract(PROPOSAL)
        plain = {"kind": "user-confirm", "at": scout.iso(time.time()), "confirmation": "yes"}
        forged = g.issue_contract(PROPOSAL, parent=parent["contractId"], approvals=[plain], stage="approved")
        self.assertEqual(scout.check(forged, g.secret, g.devices, parent), "approval present but not device-signed")

    def test_holding_the_hmac_secret_is_not_enough(self):
        g, rec = self.approved_chain()
        morning = next(c for c in g.contracts() if c["contractId"] == rec["contract"]["parentId"])
        fake = dict(rec["contract"]["risk"]["approvals"][0], signature="A" * 88)
        forged = g.issue_contract(PROPOSAL, parent=morning["contractId"], approvals=[fake], stage="approved")
        self.assertEqual(scout.check(forged, g.secret, g.devices, morning), "approval signature invalid")

    def test_real_approval_cannot_be_moved_to_other_contents(self):
        g, rec = self.approved_chain()
        morning = next(c for c in g.contracts() if c["contractId"] == rec["contract"]["parentId"])
        bigger = dict(PROPOSAL, amount=99000.0)
        moved = g.issue_contract(bigger, parent=morning["contractId"], approvals=rec["contract"]["risk"]["approvals"],
                                 stage="approved")
        self.assertEqual(scout.check(moved, g.secret, g.devices, morning),
                         "approved contract differs from the signed one (intent)")

    def test_approval_for_another_contract_is_refused(self):
        g, rec = self.approved_chain()
        other = g.issue_contract(PROPOSAL)
        moved = g.issue_contract(PROPOSAL, parent=other["contractId"], approvals=rec["contract"]["risk"]["approvals"],
                                 stage="approved")
        self.assertEqual(scout.check(moved, g.secret, g.devices, other), "approval was signed for another contract")

    def test_edited_after_signing_is_refused(self):
        g, rec = self.approved_chain()
        morning = next(c for c in g.contracts() if c["contractId"] == rec["contract"]["parentId"])
        edited = dict(rec["contract"], intent=dict(rec["contract"]["intent"], parameters={"amount": 1}))
        self.assertEqual(scout.check(edited, g.secret, g.devices, morning), "contract signature invalid")

    def test_expired_night_contract_is_replanned(self):
        g = self.gate(["approve"])
        night = scout.issue(PROPOSAL, g.secret, stage="night", now=time.time() - 3600)
        append("contracts", {"contract": night}, self.tmp)
        self.assertEqual(g.process(dict(PROPOSAL)), "EXECUTED")
        chain = read("ledger", self.tmp)[0]["approval"]["chain"]
        self.assertEqual(chain[0], night["contractId"])
        morning = next(c for c in g.contracts() if c["contractId"] == chain[1])
        self.assertTrue(morning["replanReason"].startswith("parent expired at"))

    def test_evidence_changed_overnight_is_refused(self):
        g = self.gate(["approve"])
        append("contracts", {"contract": scout.issue(PROPOSAL, g.secret, stage="night")}, self.tmp)
        changed = dict(PROPOSAL, key_text=PROPOSAL["key_text"].replace("12,500", "21,500"))
        self.assertEqual(g.process(changed), "REFUSED (evidence changed since the night contract)")
        self.assertEqual(read("ledger", self.tmp), [])


if __name__ == "__main__":
    unittest.main()


class HumanLabelTest(unittest.TestCase):
    def test_signed_decisions_become_labelled_cases(self):
        tmp = Path(tempfile.mkdtemp())
        os.environ["HUMANKEY_LOGS"] = str(tmp / "logs")
        dev = EmulatorDevice(script=["reject", "approve"])
        (tmp / "devices.json").write_text(canonical({"emulator": dev.public_key()}))
        g = Gate(dev, allow_emulator=True, state=tmp)
        clear = dict(PROPOSAL, invoice_id="INV9301", proposal="clear",
                     key_text=PROPOSAL["key_text"].replace("INV9001", "INV9301"))
        self.assertEqual(g.process(clear), "REJECTED")
        self.assertEqual(g.process(dict(PROPOSAL)), "EXECUTED")
        labels = read("human_labels", tmp)
        self.assertEqual([(c["invoice_id"], c["human"]) for c in labels], [("INV9301", "reject"), ("INV9001", "approve")])
        self.assertTrue(any(e["kind"] == "contract.override" for e in read("events", tmp / "logs")))
