"""The key's own memory: count witness and account ledger (emulator, same rules as the board)."""
import os
import tempfile
import unittest
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
os.environ["HUMANKEY_EMU_DIR"] = str(TMP / "emu")
os.environ["HUMANKEY_LOGS"] = str(TMP / "logs")

from common.jsonl import canonical, read  # noqa: E402
from gate.devices import EmulatorDevice  # noqa: E402
from gate.main import Gate  # noqa: E402


def proposal(inv, h="aaaa", l4="1111", exp=16, cov=16):
    return {"invoice_id": inv, "vendor_id": "V013", "amount": 20000.0, "risk": "medium", "route": "AWAITING_KEY",
            "proposal": "escalate", "key_text": f"{inv} $20,000\nchecks passed\nrisk medium\nPAY this?",
            "witness": {"exp": exp, "cov": cov, "miss": []},
            "findings": {"invoice_header": {"check": "invoice_header", "signal": "neutral",
                                            "facts": {"iban_hash": h, "iban_last4": l4}}}}


class KeyMemoryTest(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())

    def run_gate(self, p, answers):
        dev = EmulatorDevice(script=answers)
        self.asked = []
        ask = dev._ask
        dev._ask = lambda text: (self.asked.append(text), ask(text))[1]
        (self.state / "devices.json").write_text(canonical({"emulator": dev.public_key()}))
        return Gate(dev, allow_emulator=True, state=self.state).process(p), dev

    def test_gap_needs_acknowledgement_and_is_signed(self):
        result, _ = self.run_gate(proposal("INV9101", cov=15), ["approve", "approve"])
        self.assertEqual(result, "EXECUTED")
        self.assertIn("1 of 16 NOT checked", self.asked[0])
        rec = read("ledger", self.state)[0]["approval"]
        self.assertEqual(rec["count_witness"], {"expected": 16, "checked": 15})

    def test_rejecting_the_warning_rejects_the_payment(self):
        result, _ = self.run_gate(proposal("INV9102", cov=15), ["reject"])
        self.assertEqual(result, "REJECTED")
        self.assertEqual(len(self.asked), 1)

    def test_new_account_is_caught_by_the_key_even_if_the_database_agrees(self):
        dev = EmulatorDevice(script=["approve"])
        dev.import_accounts([{"v": "V013", "h": "old0", "l4": "3735"}])
        result, _ = self.run_gate(proposal("INV9103", h="new0", l4="5298"), ["approve", "approve"])
        self.assertEqual(result, "EXECUTED")
        self.assertIn("NEW ACCOUNT ..5298", self.asked[0])
        self.assertIn("last OK'd ..3735", self.asked[0])
        # approved once by a person: now it is the known account
        result, _ = self.run_gate(proposal("INV9104", h="new0", l4="5298"), ["approve"])
        self.assertEqual(len(self.asked), 1)

    def test_count_fell_since_last_signature(self):
        self.run_gate(proposal("INV9105", exp=17, cov=17), ["approve"])
        result, _ = self.run_gate(proposal("INV9106", exp=14, cov=14), ["approve", "approve"])
        self.assertIn("count fell 17->14", self.asked[0])

    def test_device_claiming_another_count_is_refused(self):
        dev = EmulatorDevice(script=["approve"])
        (self.state / "devices.json").write_text(canonical({"emulator": dev.public_key()}))
        g = Gate(dev, allow_emulator=True, state=self.state)
        p = proposal("INV9107")
        c = g.issue_contract(p)
        from gate.crypto import sha256
        msg = dev.show(c["contractId"], p["key_text"], canonical(c))
        self.assertEqual(g.verify(c, p["key_text"], msg, sent=(17, 16)), "device saw a different count than the gate sent")


if __name__ == "__main__":
    unittest.main()
