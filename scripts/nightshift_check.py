"""Check the Meterless Runtime (Nightshift) connection with one tiny request.

Usage: LLM_PROVIDER=meterless python3 scripts/nightshift_check.py
Reads METERLESS_BASE_URL, METERLESS_RUNTIME_TOKEN and METERLESS_MODEL from .env.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.env import load_env  # noqa: E402
from common.jsonl import read  # noqa: E402
from llm.provider import LLM  # noqa: E402

load_env()
llm = LLM(provider="meterless")
print("runtime:", llm.base, "->", llm.status())
answer = llm.chat([{"role": "user", "content": "Reply with the single word: ready"}], agent="setup",
                  purpose="nightshift check", max_tokens=2000, cache=False)
print("answer:", answer.strip()[:80])
last = read("llm")[-1]
print("answered by:", last.get("model"), "| receipt:", json.dumps(last.get("receipt", {}))[:200])
