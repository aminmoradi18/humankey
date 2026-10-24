"""Approval devices. The gate talks to both through `show(req, text, contract_sha)`.

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

    def show(self, req, text, contract_sha, w=None, acct=None):
        led = self._ledger()
        exp, cov = (w or {}).get("exp", -1), (w or {}).get("cov", -1)
        warn, status = [], "none"
        if exp >= 0 and cov >= 0 and cov < exp:
            warn.append(f"{exp - cov} of {exp} NOT checked")
        if exp >= 0 and led["last_expected"] and exp < led["last_expected"]:
            warn.append(f"count fell {led['last_expected']}->{exp}")
        if acct and acct.get("v"):
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
            if status in ("first", "new"):
                led["accounts"][acct["v"]] = {"h": acct["h"], "l4": acct["l4"]}
            self._save_ledger(led)
        th = sha256(text)
        ah = (acct or {}).get("h", "")
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

    def show(self, req, text, contract_sha, w=None, acct=None):
        cmd = {"t": "show", "req": req, "text": text, "c": contract_sha}
        if w:
            cmd["w"] = {"exp": w["exp"], "cov": w["cov"]}
        if acct:
            cmd["acct"] = acct
        self._send(cmd)
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

    def show(self, req, text, contract_sha, w=None, acct=None):
        self.pending = {"req": req, "text": text, "c": contract_sha,
                        "exp": (w or {}).get("exp", -1), "cov": (w or {}).get("cov", -1)}
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
