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
        f = {"invoice_header": dict(finding("invoice_header", "neutral"), facts={"amount": 3000.0}),
             "purchase_order": finding("purchase_order", "risk_up", "no purchase order referenced"),
             "bank_account": finding("bank_account", "risk_down")}
        used = apply_memory(self.w, "V007", f)
        self.assertEqual(len(used), 1)
        d = Risk.decide({"proposal": "escalate"}, f, [], {"verdict": "pass"}, [])
        self.assertEqual(d["proposal"], "escalate", "the model's caution still wins")
        d = Risk.decide({"proposal": "clear"}, f, [], {"verdict": "pass"}, [])
        self.assertEqual(d["route"], "QUEUED_AUTO")

    def test_contradicting_answers_settle_nothing_until_the_person_picks(self):
        f = lambda: {"purchase_order": finding("purchase_order", "risk_up", "no purchase order referenced")}
        first = self.w.remember("V010", "purchase_order", "V010 tooling always runs on a blanket PO", "ap-manager", "INV0007")
        self.assertEqual(first["conflicts"], [])
        self.assertEqual(len(apply_memory(self.w, "V010", f())), 1)
        second = self.w.remember("V010", "purchase_order", "V010 tooling never runs on a blanket PO", "ap-manager", "INV0011")
        self.assertEqual(len(second["conflicts"]), 1, "H-MEM flags the contradiction")
        self.assertEqual(apply_memory(self.w, "V010", f()), [], "neither answer settles the finding")
        self.w.resolve(second["id"], first["id"], "ap-manager")
        used = apply_memory(self.w, "V010", f())
        self.assertEqual([u["id"] for u in used], [second["id"]], "the kept answer is used, the other is superseded")
        self.assertEqual(self.w.conflicts(), [])

    def test_dream_rule_is_stored_only_when_the_key_signs_it(self):
        from gate.devices import EmulatorDevice
        from gate.main import Gate, dream_rules
        os.environ["HUMANKEY_EMU_DIR"] = os.path.join(TMP, "emu")
        self.w.remember("V020", "amount_history", "V020 repairs spike every March for the plant shutdown",
                        "ap-manager", "INV0101")
        self.w.remember("V020", "amount_history", "V020 repairs are higher in March, shutdown maintenance",
                        "ap-manager", "INV0102")
        state = __import__("pathlib").Path(TMP) / "gate"
        dev = EmulatorDevice(script=["reject"])
        gate = Gate(dev, allow_emulator=True, state=state)
        gate.devices["emulator"] = dev.public_key()
        rejected = dream_rules(gate, self.w)
        self.assertEqual([r[1] for r in rejected], ["NOT STORED"], "the person said no on the key")
        dev.script = ["approve"]
        stored = dream_rules(gate, self.w)
        self.assertEqual([r[1] for r in stored], ["STORED"])
        hits = self.w.recall("V020", "amount_history")
        rule = [h for h in hits if h["id"] == stored[0][2]]
        self.assertEqual(len(rule), 1, "the signed rule is in recall's scope")
        self.assertTrue(rule[0]["source"].startswith("human:emulator"))
        self.assertEqual(self.w.dream(), [], "the same answers are not offered again, and a rule is not a source")

    def test_dream_rule_needs_a_trusted_device(self):
        from gate.devices import EmulatorDevice
        from gate.main import Gate, dream_rules
        os.environ["HUMANKEY_EMU_DIR"] = os.path.join(TMP, "emu2")
        self.w.remember("V021", "split_pattern", "V021 always bills tooling in two parts", "ap-manager", "INV0201")
        self.w.remember("V021", "split_pattern", "V021 splits tooling invoices in two parts", "ap-manager", "INV0202")
        dev = EmulatorDevice(script=["approve"] * 4)
        gate = Gate(dev, allow_emulator=False, state=__import__("pathlib").Path(TMP) / "gate2")
        gate.devices["emulator"] = dev.public_key()
        out = dream_rules(gate, self.w)
        self.assertTrue(out and all(r[1] == "NOT STORED" for r in out))
        self.assertIn("emulator_not_trusted", out[0][2])

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
