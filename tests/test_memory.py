"""H-MEM in the product: a person's answer settles the same soft finding next time,
never a hard one, and a "wrong" grade takes the memory out of use.

Starts its own World Model/H-MEM service on a spare port with temporary storage
(needs Node; run `cd meterless && npm install` once).
"""
import os
import signal
import subprocess
import tempfile
import time
import unittest

TMP = tempfile.mkdtemp()
os.environ["WORLD_PORT"] = "8719"
os.environ["HUMANKEY_LOGS"] = os.path.join(TMP, "logs")

from agent import world as world_mod  # noqa: E402
from agent.agents import Risk, apply_memory  # noqa: E402
from common.env import ROOT  # noqa: E402


def finding(check, signal, summary="x"):
    return {"check": check, "signal": signal, "summary": summary, "facts": {}, "receipt_ids": ["r1"]}


class MemoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = dict(os.environ, WORLD_PORT="8719", WORLD_DIR=TMP, HMEM_DIR=os.path.join(TMP, "hmem"))
        cls.proc = subprocess.Popen(["npx", "tsx", "server.ts"], cwd=ROOT / "meterless", env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        world_mod.BASE = "http://127.0.0.1:8719"
        for _ in range(60):
            if world_mod.World(autostart=False)._up():
                break
            time.sleep(0.5)
        cls.w = world_mod.World(autostart=False)

    @classmethod
    def tearDownClass(cls):
        os.killpg(cls.proc.pid, signal.SIGTERM)  # npx and its node child

    def test_human_answer_settles_soft_finding_next_time(self):
        self.w.remember("V007", "purchase_order", "Kennametal tooling is bought on a blanket PO, no PO per invoice",
                        "ap-manager", "INV0007")
        f = {"invoice_header": dict(finding("invoice_header", "neutral"), facts={"amount": 4000.0}),
             "purchase_order": finding("purchase_order", "risk_up", "no purchase order referenced"),
             "bank_account": finding("bank_account", "risk_down")}
        used = apply_memory(self.w, "V007", f)
        self.assertEqual(len(used), 1)
        d = Risk.decide({"proposal": "escalate"}, f, [], {"verdict": "pass"}, [])
        self.assertEqual(d["proposal"], "escalate", "the model's caution still wins")
        d = Risk.decide({"proposal": "clear"}, f, [], {"verdict": "pass"}, [])
        self.assertEqual(d["route"], "QUEUED_AUTO")

    def test_memory_never_settles_hard_findings_or_other_vendors(self):
        self.w.remember("V005", "bank_account", "the new account is fine", "ap-manager", "INV0012")
        f = {"bank_account": finding("bank_account", "risk_up", "pays a NEW account")}
        self.assertEqual(apply_memory(self.w, "V005", f), [])
        f = {"purchase_order": finding("purchase_order", "risk_up", "no purchase order referenced")}
        self.assertEqual(apply_memory(self.w, "V999", f), [])

    def test_wrong_feedback_retires_the_memory(self):
        rec = self.w.remember("V004", "amount_history", "Sealed Air bills a yearly service contract in October",
                              "ap-manager", "INV0004")
        f = {"amount_history": finding("amount_history", "risk_up", "amount is 6x the average")}
        self.assertEqual(len(apply_memory(self.w, "V004", f)), 1)
        self.w.feedback(rec["id"], "wrong")
        f = {"amount_history": finding("amount_history", "risk_up", "amount is 6x the average")}
        self.assertEqual(apply_memory(self.w, "V004", f), [])


if __name__ == "__main__":
    unittest.main()
