"""Scout Intent execution contracts for payments, with hardware-signed approval records.

Shape and rules follow Meterless engines/scout-intent/docs/execution-contract.md: HMAC-signed,
5-minute TTL, immutable after signing, re-planned with `parentId` and the same `traceId`.
Where the spec carries approvals as plain JSON (`{kind: "user-confirm", confirmation: "yes"}`) and
only checks that they are present, an approval here is a record the device or phone signed with
its own Ed25519 key. The executor re-verifies that signature itself, so software on the laptop
that can write JSON (or even holds the HMAC secret) still cannot approve a payment.

One payment is a chain of three contracts:
  night    issued when the agents finish (`gate.main night`), binds the evidence the person will see
  morning  re-plan of the night contract when a person approves; this is what the key signs
  approved re-plan of the morning contract carrying the signed approval record; the executor checks it
"""
import calendar
import os
import time

from common.jsonl import canonical
from gate.crypto import contract_ok, decision_message, sha256, sign_contract, verify_ed25519

TTL_S = 300
REGISTRY_VERSION = 1
POLICY_VERSION = "humankey-policy-3"
INTENT = "invoice.pay"
CAPABILITY = "payment.release"
KINDS = {"hardware": "device-signature", "phone": "phone-signature"}


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def parse_iso(s):
    return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))


def new_id():
    # time-ordered like the spec's ULID: millisecond prefix, random tail
    return "ctr_" + format(int(time.time() * 1000), "012x") + os.urandom(4).hex()


def evidence(p):
    """What the person decides on. If any of it changes after the night contract, the chain stops."""
    hdr = p["findings"].get("invoice_header", {}).get("facts", {})
    signals = {k: f.get("signal") for k, f in sorted(p["findings"].items())}
    return {"digest": sha256(canonical({"text": p["key_text"], "amount": p["amount"], "vendor": p["vendor_id"],
                                        "iban": hdr.get("iban_hash", ""), "signals": signals})),
            "displaySha256": sha256(p["key_text"])}


def issue(p, secret, tier="hardware", parent=None, reason=None, approvals=(), stage="morning", now=None):
    now = time.time() if now is None else now
    c = {
        "contractId": new_id(), "parentId": parent, "traceId": p["invoice_id"], "stage": stage,
        "intent": {"primary": {"id": INTENT}, "confidence": 1.0, "alternates": [],
                   "parameters": {"invoice_id": p["invoice_id"], "amount": p["amount"], "currency": "USD"},
                   "references": {"vendor": {"type": "vendor", "id": p["vendor_id"]}}},
        "risk": {"level": p["risk"], "redactions": [],
                 "flags": [f["check"] for f in p["findings"].values() if f["signal"] in ("risk_up", "unknown")],
                 "requireApproval": KINDS[tier], "approvals": list(approvals)},
        "toolPlan": [{"tool": CAPABILITY, "args": {"invoice_id": p["invoice_id"]}}],
        "modelProfile": "none",
        "scope": {"user": "ap-manager", "surface": "humankey", "capabilities": [CAPABILITY]},
        "evidence": evidence(p),
        "issuedAt": iso(now), "expiresAt": iso(now + TTL_S),
        "registryVersion": REGISTRY_VERSION, "policyPackVersion": POLICY_VERSION, "signedBy": "scout",
    }
    if reason:
        c["replanReason"] = reason
    c["signature"] = sign_contract(c, secret)
    return c


def expired(c, now=None):
    return (time.time() if now is None else now) > parse_iso(c["expiresAt"])


def approval_record(parent, msg, tier):
    """The device's answer, kept whole so the executor can rebuild the signed message."""
    return {"kind": KINDS[tier], "at": iso(time.time()), "device": msg["id"], "contract": parent["contractId"],
            "contractSha256": sha256(canonical(parent)), "textSha256": msg["th"], "decision": msg["d"],
            "counter": msg["counter"], "exp": msg.get("exp", -1), "cov": msg.get("cov", -1),
            "acct": msg.get("acct", "none"), "ah": msg.get("ah", ""), "signature": msg.get("sig", "")}


def check(c, secret, devices, parent):
    """Executor side: None if this contract may release the payment, else why not."""
    if not contract_ok(c, secret):
        return "contract signature invalid"
    if expired(c):
        return "contract expired"
    if CAPABILITY not in c["scope"]["capabilities"]:
        return "scope does not allow payment.release"
    need = c["risk"]["requireApproval"]
    recs = [a for a in c["risk"]["approvals"] if a.get("kind") == need]
    if not recs:
        if c["risk"]["approvals"]:
            return "approval present but not device-signed"
        return "missing approval"
    if parent is None or c["parentId"] != parent["contractId"] or not contract_ok(parent, secret):
        return "approval is not for this contract chain"
    # the approved contract may only add the approval: same payment, evidence and scope as what was signed
    for k in ("traceId", "intent", "evidence", "scope"):
        if c[k] != parent[k]:
            return f"approved contract differs from the signed one ({k})"
    if need != parent["risk"]["requireApproval"]:
        return "approved contract differs from the signed one (requireApproval)"
    for a in recs:
        dev = a.get("device", "")
        if dev not in devices:
            return f"unknown device {dev!r}"
        if dev.startswith("phone-") != (need == KINDS["phone"]):
            return "a phone cannot approve a hardware-key payment"
        if a["contract"] != parent["contractId"] or a["contractSha256"] != sha256(canonical(parent)):
            return "approval was signed for another contract"
        if a["textSha256"] != c["evidence"]["displaySha256"] or a["decision"] != "approve":
            return "approval does not match what was shown"
        msg = decision_message(a["contract"], a["contractSha256"], a["textSha256"], "approve", a["counter"],
                               a["exp"], a["cov"], a["acct"], a["ah"])
        if not verify_ed25519(devices[dev], msg, a["signature"]):
            return "approval signature invalid"
    return None
