"""Load humankey/.env into os.environ (stdlib only; existing env wins)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env(path=None):
    p = Path(path) if path else ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.split(" #", 1)[0].strip()
        os.environ.setdefault(k.strip(), v)


def flag(name, default="false"):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")
