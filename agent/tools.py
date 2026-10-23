"""Investigation tools: fixed, read-only SQL checks over Zetaris.

Each tool returns a Finding:
  {check, signal: risk_up | risk_down | neutral | unknown, summary, facts, receipt_ids}
The numbers come from SQL, not from the model. The model chooses which tools to
run and explains the findings; it never invents the facts.
A tool whose query failed returns signal "unknown" (a check that could not run
is never a pass).
"""
from difflib import SequenceMatcher

DB = "northwind_erp"
APPROVAL_THRESHOLD = 10000.0

CATALOG = {
    "invoice_header": "Invoice amount, PO reference, payee account (last 4) and the e-mail it came with.",
    "vendor_registry": "Is the vendor on file with a company registry id, and how long has it been a vendor?",
    "bank_account": "Does the payee account match the vendor master? Any bank-change request, and was it verified?",
    "sender_domain": "Does the sender e-mail domain match the vendor's domain (look-alike check)?",
    "purchase_order": "Is there a purchase order for this vendor and amount?",
    "amount_history": "How does the amount compare with 12 months of payments to this vendor?",
    "split_pattern": "Several invoices from one vendor just under the approval threshold in a short window?",
}


def _f(check, signal, summary, facts, receipts):
    return {"check": check, "signal": signal, "summary": summary, "facts": facts,
            "receipt_ids": [r["receipt_id"] for r in receipts]}


def _unknown(check, receipts):
    bad = [r for r in receipts if r["status"] in ("error", "not_run")]
    why = bad[0].get("error", bad[0]["status"]) if bad else "no data"
    return _f(check, "unknown", f"check could not run: {why}", {}, receipts)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class Tools:
    def __init__(self, zetaris, agent="investigator"):
        self.z = zetaris
        self.agent = agent

    def q(self, sql, params, purpose):
        return self.z.query(sql, params, purpose=purpose, agent=self.agent)

    def run(self, check, invoice_id):
        fn = getattr(self, "t_" + check, None)
        if fn is None:
            return {"check": check, "signal": "unknown", "summary": "no such tool", "facts": {}, "receipt_ids": []}
        return fn(invoice_id)

    # ------------------------------------------------------------------
    def header(self, invoice_id):
        rows, rc = self.q(f"SELECT i.*, e.from_domain, e.subject FROM {DB}.invoices i "
                          f"LEFT JOIN {DB}.emails e ON e.email_id = i.email_id WHERE i.invoice_id = :id",
                          {"id": invoice_id}, "invoice header")
        return (rows[0] if rows else None), rc

    def t_invoice_header(self, invoice_id):
        inv, rc = self.header(invoice_id)
        if not inv:
            return _unknown("invoice_header", [rc])
        amt = _num(inv["amount"])
        facts = {"vendor_id": inv["vendor_id"], "amount": amt, "po_id": inv.get("po_id") or None,
                 "iban_last4": inv["iban_last4"], "from_domain": inv.get("from_domain"),
                 "subject": inv.get("subject"), "over_threshold": amt is not None and amt >= APPROVAL_THRESHOLD}
        return _f("invoice_header", "neutral",
                  f"{invoice_id}: {amt:,.2f} USD to account ...{inv['iban_last4']}", facts, [rc])

    def t_vendor_registry(self, invoice_id):
        rows, rc = self.q(f"SELECT v.vendor_id, v.name, v.registry_id, v.bank_since, v.country, "
                          f"(SELECT count(*) FROM {DB}.payments p WHERE p.vendor_id = v.vendor_id) AS paid_count "
                          f"FROM {DB}.vendors v JOIN {DB}.invoices i ON i.vendor_id = v.vendor_id "
                          f"WHERE i.invoice_id = :id", {"id": invoice_id}, "vendor registry")
        if not rows:
            return _unknown("vendor_registry", [rc])
        v = rows[0]
        paid = int(_num(v["paid_count"]) or 0)
        facts = {"vendor": v["name"], "registry_id": v["registry_id"] or None, "payments_on_file": paid,
                 "bank_since": v["bank_since"]}
        if not v["registry_id"]:
            return _f("vendor_registry", "risk_up",
                      f"{v['name']} has no company registry id and {paid} past payments", facts, [rc])
        return _f("vendor_registry", "risk_down",
                  f"{v['name']} is registered ({v['registry_id']}) with {paid} past payments", facts, [rc])

    def t_bank_account(self, invoice_id):
        rows, rc = self.q(f"SELECT i.iban_hash AS inv_hash, i.iban_last4 AS inv_last4, v.iban_hash AS master_hash, "
                          f"v.iban_last4 AS master_last4, v.bank_since FROM {DB}.invoices i "
                          f"JOIN {DB}.vendors v ON v.vendor_id = i.vendor_id WHERE i.invoice_id = :id",
                          {"id": invoice_id}, "payee account vs vendor master")
        if not rows:
            return _unknown("bank_account", [rc])
        r = rows[0]
        changes, rc2 = self.q(f"SELECT b.request_id, b.new_iban_hash, b.new_iban_last4, b.requested_at, b.channel, "
                              f"b.verified_by FROM {DB}.bank_change_requests b JOIN {DB}.invoices i "
                              f"ON i.vendor_id = b.vendor_id WHERE i.invoice_id = :id ORDER BY b.requested_at",
                              {"id": invoice_id}, "bank change requests")
        if rc2["status"] in ("error", "not_run"):
            return _unknown("bank_account", [rc, rc2])
        same = r["inv_hash"] == r["master_hash"]
        req = next((c for c in changes if c["new_iban_hash"] == r["inv_hash"]), None)
        facts = {"invoice_account": "..." + r["inv_last4"], "master_account": "..." + r["master_last4"],
                 "matches_master": same, "change_request": req}
        if not same and req and not req.get("verified_by"):
            return _f("bank_account", "risk_up",
                      f"pays a NEW account ...{r['inv_last4']} (master ...{r['master_last4']}); change requested by "
                      f"{req['channel']} on {req['requested_at'][:10]}, never verified", facts, [rc, rc2])
        if not same:
            return _f("bank_account", "risk_up",
                      f"pays account ...{r['inv_last4']}, not the master ...{r['master_last4']}", facts, [rc, rc2])
        verified = next((c for c in changes if c["new_iban_hash"] == r["master_hash"] and c.get("verified_by")), None)
        if verified:
            facts["change_request"] = verified
            return _f("bank_account", "risk_down",
                      f"account ...{r['inv_last4']} changed on {verified['requested_at'][:10]} and was verified "
                      f"({verified['verified_by']})", facts, [rc, rc2])
        return _f("bank_account", "risk_down", f"pays the account on file ...{r['inv_last4']}", facts, [rc, rc2])

    def t_sender_domain(self, invoice_id):
        rows, rc = self.q(f"SELECT e.from_domain, v.email_domain FROM {DB}.invoices i "
                          f"JOIN {DB}.vendors v ON v.vendor_id = i.vendor_id "
                          f"LEFT JOIN {DB}.emails e ON e.email_id = i.email_id WHERE i.invoice_id = :id",
                          {"id": invoice_id}, "sender domain vs vendor domain")
        if not rows or not rows[0]["from_domain"]:
            return _unknown("sender_domain", [rc])
        got, want = rows[0]["from_domain"], rows[0]["email_domain"]
        sim = round(SequenceMatcher(None, got, want).ratio(), 3)
        facts = {"from_domain": got, "vendor_domain": want, "similarity": sim}
        if got == want:
            return _f("sender_domain", "risk_down", f"sent from the vendor's own domain {got}", facts, [rc])
        if sim >= 0.8:
            return _f("sender_domain", "risk_up",
                      f"sent from look-alike domain {got} (vendor uses {want}, similarity {sim})", facts, [rc])
        return _f("sender_domain", "risk_up", f"sent from {got}, vendor uses {want}", facts, [rc])

    def t_purchase_order(self, invoice_id):
        rows, rc = self.q(f"SELECT i.po_id, i.amount AS inv_amount, p.amount AS po_amount, p.vendor_id AS po_vendor, "
                          f"i.vendor_id FROM {DB}.invoices i LEFT JOIN {DB}.purchase_orders p ON p.po_id = i.po_id "
                          f"WHERE i.invoice_id = :id", {"id": invoice_id}, "purchase order match")
        if not rows:
            return _unknown("purchase_order", [rc])
        r = rows[0]
        if not r["po_id"]:
            return _f("purchase_order", "risk_up", "no purchase order referenced", {"po_id": None}, [rc])
        if r["po_amount"] is None:
            return _f("purchase_order", "risk_up", f"PO {r['po_id']} not found", {"po_id": r["po_id"]}, [rc])
        ia, pa = _num(r["inv_amount"]), _num(r["po_amount"])
        facts = {"po_id": r["po_id"], "po_amount": pa, "invoice_amount": ia, "same_vendor": r["po_vendor"] == r["vendor_id"]}
        if r["po_vendor"] != r["vendor_id"] or abs(ia - pa) > 0.05 * pa:
            return _f("purchase_order", "risk_up", f"PO {r['po_id']} does not match (PO {pa:,.2f})", facts, [rc])
        return _f("purchase_order", "risk_down", f"matches PO {r['po_id']} ({pa:,.2f})", facts, [rc])

    def t_amount_history(self, invoice_id):
        rows, rc = self.q(f"SELECT count(p.amount) AS n, avg(p.amount) AS avg_amt, max(p.amount) AS max_amt, "
                          f"max(i.amount) AS inv_amount FROM {DB}.invoices i "
                          f"LEFT JOIN {DB}.payments p ON p.vendor_id = i.vendor_id WHERE i.invoice_id = :id",
                          {"id": invoice_id}, "amount vs 12-month history")
        if not rows:
            return _unknown("amount_history", [rc])
        r = rows[0]
        n, avg, mx, amt = int(_num(r["n"]) or 0), _num(r["avg_amt"]), _num(r["max_amt"]), _num(r["inv_amount"])
        if n == 0:
            return _f("amount_history", "risk_up", "no payment history for this vendor",
                      {"payments": 0, "amount": amt}, [rc])
        ratio = round(amt / avg, 2)
        facts = {"payments": n, "average": round(avg, 2), "max": mx, "amount": amt, "ratio_to_average": ratio}
        if ratio >= 3:
            return _f("amount_history", "risk_up", f"{amt:,.2f} is {ratio}x the 12-month average {avg:,.2f}", facts, [rc])
        return _f("amount_history", "risk_down", f"{amt:,.2f} is in the usual range (avg {avg:,.2f})", facts, [rc])

    def t_split_pattern(self, invoice_id):
        rows, rc = self.q(f"SELECT o.invoice_id, o.amount, o.issued_at FROM {DB}.invoices i "
                          f"JOIN {DB}.invoices o ON o.vendor_id = i.vendor_id "
                          f"WHERE i.invoice_id = :id AND abs(datediff(o.issued_at, i.issued_at)) <= 14 "
                          f"ORDER BY o.issued_at", {"id": invoice_id}, "split invoices near threshold")
        if not rows:
            return _unknown("split_pattern", [rc])
        amounts = [_num(r["amount"]) for r in rows]
        near = [r["invoice_id"] for r, a in zip(rows, amounts) if 0.9 * APPROVAL_THRESHOLD <= a < APPROVAL_THRESHOLD]
        total = round(sum(amounts), 2)
        facts = {"invoices_in_14_days": [r["invoice_id"] for r in rows], "total": total, "just_under_threshold": near}
        if invoice_id in near and len(near) >= 2 and total >= APPROVAL_THRESHOLD:
            return _f("split_pattern", "risk_up",
                      f"{len(near)} invoices just under {APPROVAL_THRESHOLD:,.0f} within 14 days, total {total:,.2f}",
                      facts, [rc])
        return _f("split_pattern", "risk_down", f"no split pattern ({len(rows)} invoice(s) in 14 days)", facts, [rc])

    def memo(self, invoice_id):
        """Untrusted free text from the invoice and its e-mail (for the guard, never for facts)."""
        rows, rc = self.q(f"SELECT i.memo, e.subject, e.body FROM {DB}.invoices i "
                          f"LEFT JOIN {DB}.emails e ON e.email_id = i.email_id WHERE i.invoice_id = :id",
                          {"id": invoice_id}, "untrusted memo text")
        return (rows[0] if rows else {}), rc
