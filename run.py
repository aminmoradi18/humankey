"""HumanKey's programmatic entry point: invoice ids in, the agents' decisions and their trace out (JSON).

Usage:
  python3 run.py                         HTTP API on $PORT (default 8000): POST /run, GET /health
  python3 run.py run input_examples/example_1.json
  python3 run.py run INV0006 INV0003 --mode sample

Request: {"run_id": "demo-001", "input": {"invoice_ids": ["INV0006"]}, "options": {"mode": "sample"}}
  mode  sample   recorded Zetaris results and model answers, all agent and gate logic run for real
        offline  recorded Zetaris results, every model call made for real (provider from .env)
        live     Zetaris and the model provider from .env
  The default is sample when SAMPLE_MODE=true, otherwise live. No invoice_ids: every invoice.

A run only investigates and routes. It never releases a payment: that needs the key, through the web view.
Each run gets its own folder (logs/runs/<run_id>/) and a trace, logs/trace-<run_id>.jsonl, which is
rebuilt from the run's own logs: agent events, model calls (with the model that answered) and Zetaris
receipts.
"""
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from common.env import flag, load_env  # noqa: E402

MODES = ("sample", "offline", "live")
INVOICE = re.compile(r"^INV\d{4}$")
RUN_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
LOCK = threading.Lock()  # one run at a time: runs share the CPU, the model and, in live mode, the gate state


class RunError(Exception):
    def __init__(self, kind, message, recoverable=True):
        super().__init__(message)
        self.kind, self.recoverable = kind, recoverable


def info(run_id, msg):
    print(f"[INFO] run_id={run_id} {msg}", flush=True)


def jsonl(path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass  # a half-written last line
    return out


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def environment(mode, folder):
    env = dict(os.environ)
    env.update(HUMANKEY_LOGS=str(folder), WORLD_DIR=str(folder / "world"), HMEM_DIR=str(folder / "hmem"),
               HUMANKEY_EMU_DIR=str(folder / "emulator"), WORLD_PORT=str(free_port()))
    env.pop("SAMPLE_MODE", None)
    if mode == "sample":
        env.update(SAMPLE_MODE="true", DEVICE="emulator")
    elif mode == "offline":
        env.update(ZETARIS_MODE="snapshot", LLM_USE_CACHE="false", HUMANKEY_LLM_CACHE=str(folder / "llm_cache"))
        env.setdefault("DEVICE", "emulator")
    if mode != "live":
        env["HUMANKEY_STATE"] = str(folder / "gate")  # live keeps the real gate state, for the web view and key
    return env


def step(args, env, run_id, timeout):
    info(run_id, "$ " + " ".join(args))
    try:
        r = subprocess.run([sys.executable, "-m", *args], cwd=ROOT, env=env, capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RunError("Timeout", f"{' '.join(args)} did not finish in {timeout} s")
    for line in r.stdout.splitlines():
        info(run_id, "  " + line)
    if r.returncode != 0:
        tail = (r.stderr or r.stdout).strip().splitlines()[-3:]
        raise RunError("StepFailed", f"{' '.join(args)} exited {r.returncode}: {' | '.join(tail)}")


def trace(folder):
    """One time-ordered trace from the run's own logs, in the guide's field names."""
    rows = []
    for e in jsonl(folder / "events.jsonl"):
        detail = {k: v for k, v in e.items() if k not in ("ts", "agent", "kind", "invoice_id")}
        rows.append({"timestamp": e.get("ts"), "agent_name": e.get("agent", "run"), "action": e["kind"],
                     "invoice_id": e.get("invoice_id"), "status": "success", "detail": detail})
    for c in jsonl(folder / "llm.jsonl"):
        rows.append({"timestamp": c.get("ts"), "agent_name": c.get("agent"), "action": "model_call",
                     "output_summary": c.get("purpose"), "provider": c.get("provider"), "model": c.get("model"),
                     "status": "success" if c.get("ok") else "error", "ms": c.get("ms"),
                     **({"error": c["error"]} if c.get("error") else {})})
    for r in jsonl(folder / "receipts.jsonl"):
        rows.append({"timestamp": r.get("ts"), "agent_name": r.get("agent"), "action": "zetaris_query",
                     "output_summary": f"{r.get('row_count')} rows from {', '.join(r.get('sources', []))}",
                     "receipt_id": r.get("receipt_id"), "mode": r.get("mode"), "sql": r.get("sql"),
                     "status": r.get("status", "ok")})
    return sorted(rows, key=lambda x: x["timestamp"] or "")


def summary(p, events, receipts):
    cited = {i for f in p.get("findings", {}).values() for i in f.get("receipt_ids", [])}
    return {"invoice_id": p["invoice_id"], "route": p["route"], "risk": p["risk"], "proposal": p["proposal"],
            "first_draft": p.get("first_draft"), "key_text": p["key_text"], "explanation": p.get("explanation"),
            "critic": p["critic"], "integrity_verdict": p["integrity"]["verdict"],
            "injection_flags": p.get("injection_flags"), "models": p.get("ai"),
            "events": [e for e in events if e.get("invoice_id") == p["invoice_id"]],
            "receipts": [{k: r.get(k) for k in ("receipt_id", "sql", "sources", "row_count", "mode", "freshness")}
                         for r in receipts if r["receipt_id"] in cited]}


def run(request):
    t0 = time.time()
    load_env()
    run_id = request.get("run_id") or "run-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    if not RUN_ID.match(str(run_id)):
        raise RunError("BadInput", "run_id: letters, digits, . _ - only", recoverable=False)
    mode = (request.get("options") or {}).get("mode") or ("sample" if flag("SAMPLE_MODE") else "live")
    if mode not in MODES:
        raise RunError("BadInput", f"options.mode must be one of {', '.join(MODES)}", recoverable=False)
    ids = list((request.get("input") or {}).get("invoice_ids") or [])
    bad = [i for i in ids if not isinstance(i, str) or not INVOICE.match(i)]
    if bad:
        raise RunError("BadInput", f"invoice ids look like INV0006: {bad}", recoverable=False)
    if not (ROOT / "meterless" / "node_modules").exists():
        raise RunError("NotInstalled", "run: cd meterless && npm install", recoverable=False)
    folder = ROOT / "logs" / "runs" / run_id
    if folder.exists():
        raise RunError("BadInput", f"run_id {run_id} already used (logs/runs/{run_id})", recoverable=False)
    folder.mkdir(parents=True)
    env = environment(mode, folder)
    info(run_id, f"Starting run: mode={mode}, invoices={ids or 'all'}")
    world = subprocess.Popen(["npx", "tsx", "server.ts"], cwd=ROOT / "meterless", env=env,
                             stdout=open(folder / "world-server.log", "w"), stderr=subprocess.STDOUT,
                             start_new_session=True)
    try:
        timeout = int(os.environ.get("RUN_STEP_TIMEOUT", "900"))
        step(["gate.main", "enroll-emulator"], env, run_id, 60)
        if mode != "live":
            step(["gate.main", "import-accounts"], {**env, "DEVICE_SCRIPT": "approve"}, run_id, 120)
        # Recorded model answers belong to the whole recorded run: an invoice reads what earlier invoices
        # of its vendor put in the World Model, so sample mode replays every invoice and reports the asked ones.
        step(["agent.run", *([] if mode == "sample" else ids)], env, run_id, timeout)
        step(["gate.main", "night"], env, run_id, 120)
    finally:
        os.killpg(world.pid, signal.SIGTERM)
    state = Path(env.get("HUMANKEY_STATE", ROOT / "gate" / "state"))
    proposals = json.loads((state / "proposals.json").read_text())
    unknown = [i for i in ids if i not in proposals]
    if unknown:
        raise RunError("UnknownInvoice", f"not in this company's data: {unknown}", recoverable=False)
    events, receipts = jsonl(folder / "events.jsonl"), jsonl(folder / "receipts.jsonl")
    rows = trace(folder)
    trace_file = ROOT / "logs" / f"trace-{run_id}.jsonl"
    trace_file.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    calls = jsonl(folder / "llm.jsonl")
    models = {}
    for c in calls:
        k = f"{c.get('provider')}:{c.get('model')}:{'ok' if c.get('ok') else 'failed'}"
        models[k] = models.get(k, 0) + 1
    out = [summary(proposals[i], events, receipts) for i in (ids or sorted(proposals))]
    elapsed = round(time.time() - t0, 1)
    info(run_id, f"Completed in {elapsed} s: " + ", ".join(f"{p['invoice_id']} {p['route']}" for p in out))
    return {"run_id": run_id, "status": "success", "mode": mode,
            "output": {"invoices": out},
            "agents": sorted({e["agent"] for e in events if e.get("agent")}),
            "model_calls": models, "zetaris_queries": len(receipts),
            "trace_id": run_id, "log_file": str(trace_file.relative_to(ROOT)),
            "run_folder": str(folder.relative_to(ROOT)), "execution_time_seconds": elapsed}


def safe_run(request, t0=None):
    t0 = t0 or time.time()
    try:
        with LOCK:
            return 200, run(request)
    except RunError as e:
        print(f"[ERROR] {e.kind}: {e}", flush=True)
        code = 400 if not e.recoverable else 502
        return code, {"run_id": request.get("run_id"), "status": "error",
                      "error": {"type": e.kind, "message": str(e), "recoverable": e.recoverable},
                      "execution_time_seconds": round(time.time() - t0, 1)}
    except Exception as e:  # a controlled answer, never a hang or a bare traceback
        print(f"[ERROR] {type(e).__name__}: {e}", flush=True)
        return 500, {"run_id": request.get("run_id"), "status": "error",
                     "error": {"type": type(e).__name__, "message": str(e)[:300], "recoverable": True},
                     "execution_time_seconds": round(time.time() - t0, 1)}


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, body):
        data = json.dumps(body, ensure_ascii=False, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self.reply(200, {"ok": True, "modes": MODES})
        self.reply(404, {"status": "error", "error": {"type": "NotFound", "message": "POST /run"}})

    def do_POST(self):
        if self.path != "/run":
            return self.reply(404, {"status": "error", "error": {"type": "NotFound", "message": "POST /run"}})
        t0 = time.time()
        try:
            request = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if not isinstance(request, dict):
                raise ValueError("a JSON object")
        except ValueError as e:
            return self.reply(400, {"status": "error", "error": {"type": "BadInput", "message": f"body: {e}",
                                                                 "recoverable": False}})
        self.reply(*safe_run(request, t0))


def main(argv):
    if argv[:1] == ["run"]:
        rest = argv[1:]
        mode = None
        if "--mode" in rest:
            i = rest.index("--mode")
            mode, rest = rest[i + 1], rest[:i] + rest[i + 2:]
        if len(rest) == 1 and rest[0].endswith(".json"):
            request = json.loads(Path(rest[0]).read_text())
        else:
            request = {"input": {"invoice_ids": rest}}
        if mode:
            request.setdefault("options", {})["mode"] = mode
        code, body = safe_run(request)
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return 0 if code == 200 else 1
    if argv:
        sys.exit(__doc__)
    host, port = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8000"))
    print(f"[INFO] HumanKey API on http://{host}:{port}  (POST /run, GET /health)", flush=True)
    HTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
