"""Append-only JSONL logs under logs/ (ARCHITECTURE.md section 13)."""
import json
import os
import time
from pathlib import Path

from common.env import ROOT

LOGS = ROOT / "logs"


def logs_dir():
    return Path(os.environ.get("HUMANKEY_LOGS", LOGS))


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def append(name, record, root=None):
    d = Path(root) if root else logs_dir()
    d.mkdir(parents=True, exist_ok=True)
    record = {"ts": now_iso(), **record}
    with open(d / f"{name}.jsonl", "a") as f:
        f.write(canonical(record) + "\n")
    return record


def read(name, root=None):
    p = (Path(root) if root else logs_dir()) / f"{name}.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
