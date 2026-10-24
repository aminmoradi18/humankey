"""Approval devices. The gate talks to all of them through `show(req, text, contract_json)`.

Clear signing: a device is given the contract itself, not a hash of it. It hashes the bytes it got,
reads the invoice, amount, payee account and invoice count from those same bytes, checks that the
laptop's screen text agrees with them, and signs the hash it computed. So the screen cannot say
$40 while the signed contract says $40,000.

- SerialDevice: the ESP32 key over USB serial, JSON lines (ARCHITECTURE.md section 5).
- EmulatorDevice: same protocol in software, with its own Ed25519 key in
  device_emu/key. It always reports id "emulator" so nothing can pass it off
  as the hardware key; the gate can refuse it (allow_emulator false).
"""
import json
import os
import queue
from pathlib import Path
import sys
import time

from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, load_pem_private_key

from common.env import ROOT
from gate.crypto import b64, decision_message, new_private_key, public_key_b64, sha256

def read_contract(contract_json, text):
    """(sha of the bytes, what they say, problem or None). Mirrors the firmware's checks."""
    c = json.loads(contract_json)
    pay, ev = c["intent"]["parameters"], c.get("evidence", {})
    seen = {"req": c.get("contractId"), "invoice": pay.get("invoice_id"), "amount": float(pay.get("amount") or 0),
            "acct": pay.get("payee") or {}, "w": ev.get("witness") or {},
            "in_master": (pay.get("payee") or {}).get("in_master", True)}
    first = text.split("\n", 1)[0]
    inv, _, money = first.partition(" $")
    try:
        shown = float(money.replace(",", ""))
    except ValueError:
        shown = None
    problem = None
    if inv != seen["invoice"] or shown is None or abs(shown - seen["amount"]) > 0.5:
        problem = f"screen says {first!r}, contract says {seen['invoice']} ${seen['amount']:,.0f}"
    elif ev.get("displaySha256") and ev["displaySha256"] != sha256(text):
        problem = "screen text is not the text in the contract"
    return sha256(contract_json), seen, problem


def emu_dir():
    return Path(os.environ.get("HUMANKEY_EMU_DIR", ROOT / "device_emu"))


class EmulatorDevice:
    device_id = "emulator"

    def __init__(self, script=None, timeout=60):
        """script: list of 'approve'/'reject'/'timeout' answers for tests; None asks on the terminal."""
        EMU_DIR = emu_dir()
        EMU_DIR.mkdir(parents=True, exist_ok=True)
        self.dir = EMU_DIR
        key_path, self.counter_path = self.dir / "key", self.dir / "counter"
        if not key_path.exists():
            key_path.write_bytes(new_private_key().private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
            key_path.chmod(0o600)
        self._key = load_pem_private_key(key_path.read_bytes(), None)
        self.script = list(script) if script is not None else None
        env_script = os.environ.get("DEVICE_SCRIPT")
        if self.script is None and env_script:
            self.script = env_script.split(",")
        self.timeout = timeout

    def public_key(self):
        return public_key_b64(self._key)

    def _next_counter(self):
        n = int(self.counter_path.read_text()) + 1 if self.counter_path.exists() else 1
        self.counter_path.write_text(str(n))
        return n

    def _ask(self, text):
        if self.script is not None:
            return self.script.pop(0) if self.script else "timeout"
        bar = "+" + "-" * 23 + "+"
        print("\n  HumanKey EMULATOR (not the hardware key)\n  " + bar, file=sys.stderr)
        for line in text.split("\n"):
            print(f"  | {line:<21} |", file=sys.stderr)
        print("  " + bar + "\n  [a] approve   [r] reject", file=sys.stderr)
        try:
            ans = input("  > ").strip().lower()
        except EOFError:
            return "timeout"
        return {"a": "approve", "r": "reject"}.get(ans[:1], "timeout")

    # The emulator keeps the same private memory as the board: last independent count and the
    # last account a human approved per vendor (device_emu/ledger.json).
    def _ledger(self):
        p = self.dir / "ledger.json"
        return json.loads(p.read_text()) if p.exists() else {"last_expected": 0, "accounts": {}}

    def _save_ledger(self, led):
        (self.dir / "ledger.json").write_text(json.dumps(led, sort_keys=True))

    def import_accounts(self, items):
        led = self._ledger()
        d = self._ask(f"ACCOUNT IMPORT\ntrust {len(items)} vendor\naccounts from\nthe gate?")
        if d == "approve":
            led["accounts"] = {it["v"]: {"h": it["h"], "l4": it["l4"]} for it in items}
            self._save_ledger(led)
        return {"t": "acct_import", "d": d, "n": len(led["accounts"])}

    def show(self, req, text, contract_json):
        contract_sha, seen, problem = read_contract(contract_json, text)
        if problem or seen["req"] != req:
            # Nothing to approve: the person only sees the mismatch, any answer is a rejection.
            self._ask(f"!! MISMATCH !!\n{problem or 'contract is for another request'}\nnot signing")
            counter = self._next_counter()
            th = sha256(text)
            return {"t": "decision", "req": req, "d": "reject", "counter": counter, "th": th, "id": self.device_id,
                    "exp": -1, "cov": -1, "acct": "none", "ah": "", "warned": 1, "mismatch": problem,
                    "sig": b64(self._key.sign(decision_message(req, contract_sha, th, "reject", counter)))}
        led = self._ledger()
        w, acct = seen["w"], {"v": seen["acct"].get("vendor"), "h": seen["acct"].get("iban_hash", ""),
                              "l4": seen["acct"].get("iban_last4", "")}
        exp, cov = w.get("exp", -1), w.get("cov", -1)
        warn, status = [], "none"
        if exp >= 0 and cov >= 0 and cov < exp:
            warn.append(f"{exp - cov} of {exp} not in list")
            back = set(w.get("recovered") or [])
            warn += [f"{m} {'re-checked' if m in back else 'NOT checked'}" for m in (w.get("miss") or [])[:2]]
        if exp >= 0 and led["last_expected"] and exp < led["last_expected"]:
            warn.append(f"count fell {led['last_expected']}->{exp}")
        if not seen["in_master"]:
            warn.append("vendor NOT in master")
            warn.append("account not saved")
        if acct.get("v") and acct.get("h"):
            known = led["accounts"].get(acct["v"])
            status = "first" if not known else ("known" if known["h"] == acct["h"] else "new")
            if status == "new":
                warn += [f"NEW ACCOUNT ..{acct['l4']}", f"last OK'd ..{known['l4']}"]
        d = "approve"
        if warn:
            d = self._ask("!! KEY WARNING\n" + "\n".join(warn) + "\nBT1=I SEE  BT2=NO")
        if d == "approve":
            d = self._ask(text)
        counter = self._next_counter()
        if d == "approve":
            if exp >= 0:
                led["last_expected"] = exp
            if status in ("first", "new") and seen["in_master"]:
                led["accounts"][acct["v"]] = {"h": acct["h"], "l4": acct["l4"]}
            self._save_ledger(led)
        th = sha256(text)
        ah = acct.get("h", "") if status != "none" else ""
        msg = {"t": "decision", "req": req, "d": d, "counter": counter, "th": th, "id": self.device_id,
               "exp": exp, "cov": cov, "acct": status, "ah": ah, "warned": len(warn)}
        if d != "timeout":
            msg["sig"] = b64(self._key.sign(decision_message(req, contract_sha, th, d, counter, exp, cov, status, ah)))
        return msg


class SerialDevice:
    def __init__(self, port=None, baud=115200, timeout=60):
        import serial  # pyserial, only needed with the hardware key
        self.port = port or os.environ.get("DEVICE_PORT", "/dev/ttyACM0")
        self.ser = serial.Serial(self.port, baud, timeout=1)
        self.timeout = timeout
        self.device_id = None
        time.sleep(2.5)  # opening the port resets the board; let it boot
        self.ser.reset_input_buffer()

    def _send(self, obj):
        self.ser.write((json.dumps(obj, separators=(",", ":")) + "\n").encode())

    def _read(self, want, deadline):
        while time.time() < deadline:
            line = self.ser.readline().decode(errors="replace").strip()
            if line.startswith("{"):
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("t") == want:
                    return msg
        return None

    def ping(self):
        self._send({"t": "ping"})
        msg = self._read("pong", time.time() + 5)
        if msg:
            self.device_id = msg.get("id")
        return msg

    def import_accounts(self, items):
        self._send({"t": "acct_import", "items": items})
        return self._read("acct_import", time.time() + self.timeout + 10)

    def show(self, req, text, contract_json):
        if not contract_json.isascii():
            raise ValueError("contract must be ASCII so the board hashes the same bytes")
        # The board ignores input while it shows the last result (about 5 s), and a whole contract is
        # longer than its receive buffer, so wait until it answers a ping before sending.
        ready = time.time() + 15
        while time.time() < ready and not self.ping():
            pass
        self._send({"t": "show", "req": req, "text": text, "cj": contract_json})
        # a warning screen may come first: allow two answers' worth of time
        msg = self._read("decision", time.time() + 2 * self.timeout + 10)
        return msg or {"t": "decision", "req": req, "d": "timeout", "counter": None, "th": None,
                       "id": self.device_id}


class PhoneDevice:
    """A paired phone (mobile/ app). It holds its own Ed25519 key; this side only relays.

    show() publishes the request; the phone fetches it, shows it, signs on the phone and posts
    the decision back through answer(). The gate verifies like any device, and refuses a phone
    signature on anything but a phone-tier contract.
    """

    def __init__(self, timeout=120):
        self.timeout = timeout
        self.pending = None
        self.answers = queue.Queue()

    def show(self, req, text, contract_json):
        # The app hashes cj itself and checks it against what it shows; c is only a convenience.
        w = json.loads(contract_json).get("evidence", {}).get("witness") or {}
        self.pending = {"req": req, "text": text, "cj": contract_json, "c": sha256(contract_json),
                        "exp": w.get("exp", -1), "cov": w.get("cov", -1)}
        try:
            msg = self.answers.get(timeout=self.timeout)
        except queue.Empty:
            msg = {"d": "timeout", "id": None}
        finally:
            self.pending = None
        return {"t": "decision", "req": req, "acct": "none", "ah": "", "warned": 0, **msg}

    def answer(self, msg):
        if self.pending is None or msg.get("req") != self.pending["req"]:
            return False
        self.answers.put({k: msg.get(k) for k in ("d", "id", "counter", "th", "sig", "exp", "cov")})
        return True


def open_device(kind=None, **kw):
    kind = kind or os.environ.get("DEVICE", "emulator")
    return SerialDevice(**kw) if kind == "serial" else EmulatorDevice(**kw)
