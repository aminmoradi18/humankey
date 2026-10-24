"""Clear signing: the key reads what it shows from the contract it signs, and the gate pays that contract.

Run: python3 -m unittest discover -s tests -t .
"""
import os
import tempfile
import unittest
from pathlib import Path

from common.jsonl import canonical, read
from gate.devices import EmulatorDevice, read_contract
from gate.main import Gate
from tests.test_key_memory import proposal


class ClearSigningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["HUMANKEY_LOGS"] = str(self.tmp / "logs")
        os.environ["HUMANKEY_EMU_DIR"] = str(self.tmp / "emu")

    def gate(self, script, dev=None):
        key = EmulatorDevice(script=script)
        (self.tmp / "devices.json").write_text(canonical({"emulator": key.public_key()}))
        return Gate(dev or key, allow_emulator=True, state=self.tmp), key

    def test_honest_payment_is_paid_from_the_contract(self):
        g, _ = self.gate(["approve"])
        self.assertEqual(g.process(proposal("INV9201", h="ab12", l4="4242")), "EXECUTED")
        line = read("ledger", self.tmp)[0]
        self.assertEqual((line["amount"], line["iban_hash"], line["iban_last4"]), (20000.0, "ab12", "4242"))
        self.assertEqual(line["amount"], line["approval"]["contract"]["intent"]["parameters"]["amount"])

    def test_screen_that_lies_about_the_amount_is_not_signed(self):
        class Liar:
            def __init__(self, dev):
                self.dev = dev

            def show(self, req, text, cj):
                return self.dev.show(req, text.replace("$20,000", "$40"), cj)
        key = EmulatorDevice(script=["approve"])
        g, _ = self.gate(None, dev=Liar(key))
        (self.tmp / "devices.json").write_text(canonical({"emulator": key.public_key()}))
        g.devices = {"emulator": key.public_key()}
        self.assertEqual(g.process(proposal("INV9202")), "REJECTED")
        self.assertEqual(read("ledger", self.tmp), [])

    def test_contract_reading(self):
        g, _ = self.gate([])
        p = proposal("INV9203")
        cj = canonical(g.issue_contract(p))
        self.assertIsNone(read_contract(cj, p["key_text"])[2])
        self.assertIn("contract says INV9203 $20,000", read_contract(cj, "INV9203 $40\nok\n\nPAY this?")[2])
        # a cent of rounding is not a lie
        self.assertIsNone(read_contract(cj.replace("20000.0", "20000.4"), p["key_text"])[2])


if __name__ == "__main__":
    unittest.main()
