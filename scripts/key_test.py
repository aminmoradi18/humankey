"""Hardware key check: send one test payment to the board and verify its signed answer.

Usage: python3 scripts/key_test.py [approve-hint]
Uses a scratch gate state (copy of the enrolled devices), so the real ledger is untouched.
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gate.devices import SerialDevice  # noqa: E402
from gate.main import STATE, Gate  # noqa: E402

PROPOSAL = {
    "invoice_id": "TEST-KEY", "vendor_id": "V000", "amount": 1.0, "risk": "medium", "route": "AWAITING_KEY",
    "proposal": "escalate", "key_text": "TEST $1\nhardware key check\nBT1 yes  BT2 no\nPAY test?",
    "findings": {},
}


def main():
    tmp = Path(tempfile.mkdtemp())
    for f in ("devices.json", "counters.json"):
        if (STATE / f).exists():
            shutil.copy(STATE / f, tmp / f)
    dev = SerialDevice()
    pong = dev.ping()
    print("key:", pong)
    print("Look at the screen and answer within 60 s ...")
    result = Gate(dev, allow_emulator=False, state=tmp).process(dict(PROPOSAL))
    print("gate result:", result)
    if (tmp / "counters.json").exists():
        shutil.copy(tmp / "counters.json", STATE / "counters.json")  # keep replay protection in step


if __name__ == "__main__":
    main()
