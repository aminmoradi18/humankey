"""One injected generator for every agent (Meterless engine checklist).

Backends, all OpenAI-compatible chat completions:
- meterless: Meterless Runtime served by Nightshift (primary provider, Power rule).
  Bearer token + `meterless-app` + `meterless-scope: user` headers, 180 s timeout.
- openrouter: NVIDIA Nemotron models for development (free variants first).
- ollama: open-weights Nemotron served locally (no key, no internet).
- custom: any other OpenAI-compatible endpoint.
- replay: answers from data/llm_cache only (SAMPLE_MODE, no credentials).

Every call appends to logs/llm.jsonl with the provider and the concrete model
that answered, so the run shows which model handled each request.
Live answers are cached by prompt hash; replayed answers are marked replay.
"""
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from common.env import ROOT, flag, load_env
from common.jsonl import append, canonical

CACHE = Path(os.environ.get("HUMANKEY_LLM_CACHE", ROOT / "data" / "llm_cache"))
APP_ID = "humankey"

# Free Nemotron routes that answered on 2026-10-22; tried in order after LLM_MODEL.
OPENROUTER_FALLBACK = [
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3.5-super-vl:free",
    "nvidia/nemotron-3.5-lightning:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
]


class LLMError(RuntimeError):
    pass


def normalize_meterless_url(url):
    """Accept a pasted URL in any of the documented shapes and return <origin>/v1."""
    url = url.strip().rstrip("/")
    if "://" not in url:
        url = "http://" + url
    url = re.sub(r"/(v1/chat/completions|v1/models|healthz|v1)$", "", url)
    url = re.sub(r"/connect(#.*)?$", "", url)
    return url + "/v1"


class LLM:
    def __init__(self, provider=None, log_root=None):
        self.calls = []  # (agent, model that answered, prompt hash) of every answer, in order
        load_env()
        self.provider = provider or ("replay" if flag("SAMPLE_MODE") else os.environ.get("LLM_PROVIDER", "meterless"))
        self.log_root = log_root
        if self.provider == "meterless":
            self.base = normalize_meterless_url(os.environ.get("METERLESS_BASE_URL", "http://127.0.0.1:8088/v1"))
            self.key = os.environ.get("METERLESS_RUNTIME_TOKEN", "")
            self.models = [os.environ.get("METERLESS_MODEL", "meterless/auto")]
            self.timeout = 180
        elif self.provider == "openrouter":
            self.base = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
            self.key = os.environ.get("OPENROUTER_API_KEY", "")
            first = os.environ.get("LLM_MODEL", OPENROUTER_FALLBACK[0])
            self.models = [first] + [m for m in OPENROUTER_FALLBACK if m != first]
            self.timeout = 120
        elif self.provider in ("ollama", "custom"):
            # ollama: local open-weights Nemotron on this machine's NVIDIA GPU.
            # custom: any other OpenAI-compatible endpoint (CUSTOM_BASE_URL, CUSTOM_API_KEY).
            local = self.provider == "ollama"
            self.base = os.environ.get("OLLAMA_BASE_URL" if local else "CUSTOM_BASE_URL",
                                       "http://127.0.0.1:11434/v1" if local else "").rstrip("/")
            self.key = "ollama" if local else os.environ.get("CUSTOM_API_KEY", "")
            names = os.environ.get("OLLAMA_MODEL" if local else "CUSTOM_MODEL", "")
            self.models = [m.strip() for m in names.split(",") if m.strip()]
            self.timeout = 600 if local else 120
        elif self.provider == "replay":
            self.base, self.key, self.models, self.timeout = "", "", ["replay"], 0
        else:
            raise LLMError(f"unknown LLM_PROVIDER {self.provider!r}")

    # ------------------------------------------------------------------
    def _headers(self):
        h = {"Content-Type": "application/json", "Authorization": f"Bearer {self.key}"}
        if self.provider == "meterless":
            h.update({"meterless-app": APP_ID, "meterless-scope": "user"})
        else:
            h.update({"HTTP-Referer": "https://github.com/humankey", "X-Title": "HumanKey"})
        return h

    def status(self):
        """'online' | 'unauthorized' | 'offline', decided by GET /models (contract v4)."""
        if self.provider == "replay":
            return "online"
        req = urllib.request.Request(self.base + "/models", headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return "online" if r.status == 200 else "offline"
        except urllib.error.HTTPError as e:
            return "unauthorized" if e.code in (401, 403) else "offline"
        except (urllib.error.URLError, OSError):
            return "offline"

    def _post(self, model, messages, max_tokens, temperature):
        body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
        req = urllib.request.Request(self.base + "/chat/completions", json.dumps(body).encode(), self._headers())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                d = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise LLMError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:200]}") from e
        except (urllib.error.URLError, OSError) as e:
            raise LLMError(f"transport: {getattr(e, 'reason', e)}") from e
        if "error" in d:
            raise LLMError(str(d["error"].get("message", d["error"]))[:200])
        msg = d["choices"][0]["message"]
        text = msg.get("content") or ""
        if not text.strip():
            raise LLMError("empty answer")
        # Nightshift may route to another model than the one asked for; its receipt says which one answered.
        receipt = d.get("meterless") or d.get("receipt") or {}
        used = receipt.get("routed_model") or d.get("routed_model") or d.get("model", model)
        return text, used, d.get("provider"), d.get("usage", {}), receipt

    def chat(self, messages, agent, purpose="", max_tokens=1200, temperature=0.2, cache=True):
        """Return the answer text. Tries models in order; raises LLMError if all fail."""
        key = hashlib.sha256(canonical({"m": messages, "t": max_tokens}).encode()).hexdigest()
        path = CACHE / f"{key}.json"
        if self.provider == "replay" or (cache and flag("LLM_USE_CACHE", "true") and path.exists()):
            if not path.exists():
                raise LLMError("no cached answer for this prompt (replay mode)")
            saved = json.loads(path.read_text())
            append("llm", {"agent": agent, "purpose": purpose, "provider": "replay",
                           "model": saved["model"], "prompt_sha": key, "ms": 0, "ok": True}, self.log_root)
            self.calls.append((agent, saved["model"], key))
            return saved["text"]
        errors = []
        for model in self.models:
            t0 = time.time()
            try:
                try:
                    text, used, upstream, usage, receipt = self._post(model, messages, max_tokens, temperature)
                except LLMError as e:
                    # Reasoning models (hosted Nemotron) can spend the whole budget thinking and return no
                    # answer, and a long link can drop once. Retry once, with room to think if it was empty.
                    if not (str(e) == "empty answer" or str(e).startswith("transport")):
                        raise
                    more = max(max_tokens * 3, 3000) if str(e) == "empty answer" else max_tokens
                    text, used, upstream, usage, receipt = self._post(model, messages, more, temperature)
            except LLMError as e:
                errors.append(f"{model}: {e}")
                append("llm", {"agent": agent, "purpose": purpose, "provider": self.provider, "model": model,
                               "prompt_sha": key, "ms": int((time.time() - t0) * 1000), "ok": False,
                               "error": str(e)[:200]}, self.log_root)
                # A typed refusal from Meterless goes back to the caller; only transport errors fall back.
                if self.provider == "meterless" and not str(e).startswith("transport"):
                    break
                continue
            append("llm", {"agent": agent, "purpose": purpose, "provider": self.provider, "model": used,
                           "upstream": upstream, "prompt_sha": key, "ms": int((time.time() - t0) * 1000),
                           "ok": True, "usage": usage, "requested": model,
                           **({"receipt": receipt} if receipt else {})}, self.log_root)
            CACHE.mkdir(parents=True, exist_ok=True)
            path.write_text(canonical({"model": used, "text": text}))
            self.calls.append((agent, used, key))
            return text
        raise LLMError("all models failed: " + " | ".join(errors))

    def chat_json(self, messages, agent, purpose="", **kw):
        """chat() and parse the first JSON object in the answer."""
        text = self.chat(messages, agent, purpose, **kw)
        return parse_json(text)


def parse_json(text):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    raw = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    try:
        return json.loads(raw)
    except ValueError as e:
        raise LLMError(f"answer is not JSON: {text[:160]!r}") from e
