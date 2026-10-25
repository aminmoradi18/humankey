"""Client for the Meterless World Model service (meterless/server.ts).

Starts the service if it is not running. Agents write findings and decisions to
it and read a vendor's context from it before planning.
"""
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from common.env import ROOT

PORT = int(os.environ.get("WORLD_PORT", "8711"))
BASE = f"http://127.0.0.1:{PORT}"


class WorldError(RuntimeError):
    pass


class World:
    def __init__(self, autostart=True):
        if autostart and not self._up():
            self._start()

    def _req(self, method, path, body=None, timeout=30):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(BASE + path, data, {"Content-Type": "application/json"}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise WorldError(e.read().decode(errors="replace")[:200]) from e
        except (urllib.error.URLError, OSError) as e:
            raise WorldError(str(getattr(e, "reason", e))) from e

    def _up(self):
        try:
            return self._req("GET", "/health", timeout=2).get("ok") is True
        except WorldError:
            return False

    def _start(self):
        log = open(ROOT / "logs" / "world-server.log", "a")
        subprocess.Popen(["npx", "tsx", "server.ts"], cwd=ROOT / "meterless", stdout=log, stderr=log,
                         start_new_session=True)
        for _ in range(60):
            if self._up():
                return
            time.sleep(0.5)
        raise WorldError("world model service did not start (see logs/world-server.log)")

    def observe(self, invoice_id, vendor, run_id, receipts, findings):
        return self._req("POST", "/observe", {"invoice_id": invoice_id, "vendor": vendor, "run_id": run_id,
                                              "receipts": receipts, "findings": list(findings.values())})

    def decision(self, invoice_id, vendor, run_id, decision, agent="risk"):
        return self._req("POST", "/decision", {"invoice_id": invoice_id, "vendor": vendor, "run_id": run_id,
                                               "agent": agent, "decision": decision})

    def context(self, vendor):
        return self._req("GET", "/context?vendor=" + urllib.request.quote(vendor))

    def hints(self, vendor, exclude_invoice=None):
        """Short lines for the planner: what other agents already established about this vendor."""
        ctx = self.context(vendor)
        found, decided = [], []
        for f in ctx["facts"]:
            v = f["value"] if isinstance(f["value"], dict) else {}
            if v.get("invoice_id") == exclude_invoice:
                continue
            if f["predicate"] == "last_decision":
                decided.append(f"earlier invoice {v.get('invoice_id')} was {v.get('proposal')} ({v.get('route')})")
            elif v.get("signal") == "risk_up":
                found.append(f"{f['predicate']}: {v.get('summary')} (seen on {v.get('invoice_id')}, by {f['by']})")
        # The service returns facts in no fixed order; a fixed order keeps the planner's prompt the same.
        return sorted(found) + sorted(decided)

    # -- H-MEM: what humans told us --------------------------------------
    def remember(self, vendor, check, content, by, invoice_id):
        return self._req("POST", "/memory/add", {"vendor": vendor, "check": check, "content": content,
                                                 "by": by, "invoice_id": invoice_id})

    def recall(self, vendor, check, query=""):
        q = urllib.parse.urlencode({"vendor": vendor, "check": check, "q": query})
        return self._req("GET", "/memory/recall?" + q)["hits"]

    def feedback(self, memory_id, kind):
        return self._req("POST", "/memory/feedback", {"id": memory_id, "kind": kind})

    def conflicts(self):
        """Human answers that contradict each other (H-MEM conflict detection); none of them is recalled."""
        return self._req("GET", "/memory/conflicts")

    def resolve(self, keep, drop, by):
        return self._req("POST", "/memory/resolve", {"keep": keep, "drop": drop, "by": by})

    def dream(self):
        """H-MEM dreaming: rules proposed from several human answers. None is stored until approved."""
        return self._req("POST", "/memory/dream", {})

    def dream_approve(self, proposal, approval):
        return self._req("POST", "/memory/dream/approve", {"id": proposal["id"], "vendor": proposal["vendor"],
                                                           "check": proposal["check"], "approval": approval})

    def dream_reject(self, proposal_id):
        return self._req("POST", "/memory/dream/reject", {"id": proposal_id})

