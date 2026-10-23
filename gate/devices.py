"""Approval devices. The gate talks to both through `show(req, text, contract_sha)`.

- SerialDevice: the ESP32 key over USB serial, JSON lines (ARCHITECTURE.md section 5).
- EmulatorDevice: same protocol in software, with its own Ed25519 key in
  device_emu/key. It always reports id "emulator" so nothing can pass it off
  as the hardware key; the gate can refuse it (allow_emulator false).
"""
import json
import os
import sys
import time

from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, load_pem_private_key

from common.env import ROOT
from gate.crypto import b64, decision_message, new_private_key, public_key_b64, sha256

EMU_DIR = ROOT / "device_emu"


class EmulatorDevice:
    device_id = "emulator"

    def __init__(self, script=None, timeout=60):
        """script: list of 'approve'/'reject'/'timeout' answers for tests; None asks on the terminal."""
        EMU_DIR.mkdir(exist_ok=True)
        key_path, self.counter_path = EMU_DIR / "key", EMU_DIR / "counter"
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

    def show(self, req, text, contract_sha):
        d = self._ask(text)
        counter = self._next_counter()
        th = sha256(text)
        msg = {"t": "decision", "req": req, "d": d, "counter": counter, "th": th, "id": self.device_id}
        if d != "timeout":
            msg["sig"] = b64(self._key.sign(decision_message(req, contract_sha, th, d, counter)))
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

    def show(self, req, text, contract_sha):
        self._send({"t": "show", "req": req, "text": text, "c": contract_sha})
        msg = self._read("decision", time.time() + self.timeout + 5)
        return msg or {"t": "decision", "req": req, "d": "timeout", "counter": None, "th": None,
                       "id": self.device_id}


def open_device(kind=None, **kw):
    kind = kind or os.environ.get("DEVICE", "emulator")
    return SerialDevice(**kw) if kind == "serial" else EmulatorDevice(**kw)
