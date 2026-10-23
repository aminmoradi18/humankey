"""Critic, risk and reporter rules (no network, no model)."""
import unittest

from agent.agents import Critic, Reporter, Risk


class FakeZetaris:
    def __init__(self, same=True):
        self.same = same

    def query(self, sql, purpose="", agent=""):
        return [], {"result_sha256": "h" if self.same else "changed", "status": "ok"}


def finding(check, signal, rid):
    return {"check": check, "signal": signal, "summary": check, "facts": {}, "receipt_ids": [rid]}


RECEIPTS = [{"receipt_id": f"r{i}", "sql": "SELECT 1", "status": "ok", "result_sha256": "h"} for i in range(4)]
CLEAN = {"invoice_header": dict(finding("invoice_header", "neutral", "r0"), facts={"amount": 1200.0}),
         "vendor_registry": finding("vendor_registry", "risk_down", "r1"),
         "bank_account": finding("bank_account", "risk_down", "r2"),
         "sender_domain": finding("sender_domain", "risk_down", "r3"),
         "purchase_order": finding("purchase_order", "risk_down", "r3")}


class CriticTest(unittest.TestCase):
    def test_accepts_supported_clear(self):
        draft = {"proposal": "clear", "claims": [{"text": "ok", "supports": "risk_down", "receipt_ids": ["r2"]}]}
        verdict, reasons, integ = Critic(FakeZetaris()).review(draft, CLEAN, RECEIPTS, [])
        self.assertEqual((verdict, reasons, integ["verdict"]), ("accept", [], "pass"))

    def test_sends_back_missing_required_check(self):
        f = {k: v for k, v in CLEAN.items() if k != "bank_account"}
        verdict, reasons, _ = Critic(FakeZetaris()).review({"proposal": "clear", "claims": []}, f, RECEIPTS, [])
        self.assertEqual(verdict, "revise")
        self.assertIn("bank_account", reasons[0])

    def test_sends_back_claim_without_receipt_and_clear_over_injection(self):
        draft = {"proposal": "clear", "claims": [{"text": "trust me", "supports": "risk_down", "receipt_ids": []}]}
        verdict, reasons, _ = Critic(FakeZetaris()).review(draft, CLEAN, RECEIPTS, [{"by": "rules"}])
        self.assertEqual(verdict, "revise")
        self.assertTrue(any("no receipt" in r for r in reasons))
        self.assertTrue(any("instruct the AI" in r for r in reasons))

    def test_result_drift_degrades_integrity(self):
        draft = {"proposal": "escalate", "claims": []}
        _, reasons, integ = Critic(FakeZetaris(same=False)).review(draft, CLEAN, RECEIPTS, [])
        self.assertEqual(integ["verdict"], "degraded")
        self.assertTrue(any("changed on re-run" in r for r in reasons))


class CoverageTest(unittest.TestCase):
    def test_large_amount_requires_history_and_split_checks(self):
        f = dict(CLEAN, invoice_header=dict(CLEAN["invoice_header"], facts={"amount": 9700.0}))
        verdict, reasons, _ = Critic(FakeZetaris()).review({"proposal": "clear", "claims": []}, f, RECEIPTS, [])
        self.assertEqual(verdict, "revise")
        self.assertIn("split_pattern", reasons[0])

    def test_block_without_evidence_is_sent_back_and_becomes_escalate(self):
        verdict, reasons, _ = Critic(FakeZetaris()).review({"proposal": "block", "claims": []}, CLEAN, RECEIPTS, [])
        self.assertTrue(any("no check raised risk" in r for r in reasons))
        d = Risk.decide({"proposal": "block"}, CLEAN, [], {"verdict": "pass"}, [])
        self.assertEqual((d["proposal"], d["route"]), ("escalate", "AWAITING_KEY"))


class RiskTest(unittest.TestCase):
    PASS = {"verdict": "pass"}

    def test_clean_small_payment_is_auto(self):
        d = Risk.decide({"proposal": "clear"}, CLEAN, [], self.PASS, [])
        self.assertEqual((d["proposal"], d["route"]), ("clear", "QUEUED_AUTO"))

    def test_large_clean_payment_needs_key(self):
        f = dict(CLEAN, invoice_header=dict(CLEAN["invoice_header"], facts={"amount": 12000.0}))
        self.assertEqual(Risk.decide({"proposal": "clear"}, f, [], self.PASS, [])["route"], "AWAITING_KEY")

    def test_model_cannot_lower_risk(self):
        f = dict(CLEAN, bank_account=finding("bank_account", "risk_up", "r2"),
                 sender_domain=finding("sender_domain", "risk_up", "r3"))
        self.assertEqual(Risk.decide({"proposal": "clear"}, f, [], self.PASS, [])["route"], "BLOCKED")

    def test_unknown_check_escalates(self):
        f = dict(CLEAN, bank_account=finding("bank_account", "unknown", "r2"))
        self.assertEqual(Risk.decide({"proposal": "clear"}, f, [], self.PASS, [])["route"], "AWAITING_KEY")

    def test_injection_blocks(self):
        self.assertEqual(Risk.decide({"proposal": "clear"}, CLEAN, [{"by": "rules"}], self.PASS, [])["proposal"], "block")


class ReporterTest(unittest.TestCase):
    def test_key_text_fits_the_screen(self):
        f = dict(CLEAN, bank_account=finding("bank_account", "risk_up", "r2"))
        text = Reporter.key_text("INV0008", {"proposal": "block", "risk": "high"}, f, [{"by": "rules"}])
        lines = text.split("\n")
        self.assertEqual(len(lines), 4)
        self.assertTrue(all(len(x) <= 21 for x in lines))
        self.assertTrue(lines[-1].endswith("?"))


if __name__ == "__main__":
    unittest.main()
