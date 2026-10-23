"""Append-only JSONL logs under logs/ (ARCHITECTURE.md section 13)."""
import json
import time
from pathlib import Path

from common.env import ROOT

LOGS = ROOT / "logs"


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def append(name, record, root=None):
    d = Path(root) if root else LOGS
    d.mkdir(parents=True, exist_ok=True)
    record = {"ts": now_iso(), **record}
    with open(d / f"{name}.jsonl", "a") as f:
        f.write(canonical(record) + "\n")
    return record


def read(name, root=None):
    p = (Path(root) if root else LOGS) / f"{name}.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
