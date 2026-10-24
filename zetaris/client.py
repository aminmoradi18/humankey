"""Zetaris adapter: every SQL statement goes through `ZetarisClient.query`.

- Read statements return rows plus a Receipt (ARCHITECTURE.md section 4).
- Anything else is a write and is refused unless an approver says yes
  (the Write Guard, section 7). INVALIDATE CACHE is never sent.
- Live results are saved as snapshots keyed by the SQL text, so
  ZETARIS_MODE=snapshot (or SAMPLE_MODE=true) replays them without Zetaris.
  Snapshot receipts carry mode "snapshot"; they are never shown as live.
"""
import hashlib
import json
import os
import re
import uuid
import urllib.error
import urllib.request
from pathlib import Path

from common.env import ROOT, flag, load_env
from common.jsonl import append, canonical, now_iso

SNAPSHOTS = Path(os.environ.get("HUMANKEY_SNAPSHOTS", ROOT / "data" / "snapshots"))
READ_VERBS = ("SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "LIST", "PREVIEW", "EXPLAIN")
NEVER = re.compile(r"\bINVALIDATE\s+CACHE\b", re.I)
SAFE_ID = re.compile(r"^[A-Za-z0-9_.\-@ ]{0,80}$")


class ZetarisError(RuntimeError):
    pass


class WriteRefused(ZetarisError):
    pass


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def normalize(sql):
    return " ".join(sql.strip().rstrip(";").split())


def classify(sql):
    """'read' or 'write'. Unknown or multi-statement text counts as a write."""
    s = re.sub(r"--[^\n]*|/\*.*?\*/", " ", sql, flags=re.S).strip().rstrip(";")
    if not s or ";" in s:
        return "write"
    words = s.split(None, 2)
    verb = words[0].upper()
    second = words[1].upper() if len(words) > 1 else ""
    if (verb, second) in (("RUN", "DQ"), ("LOAD", "USL")):
        return "read"  # data-quality checks and model reads change nothing
    return "read" if verb in READ_VERBS else "write"


def bind(sql, params):
    """Inline :name parameters as quoted literals. Only short id-like values are accepted."""
    def lit(v):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return repr(v)
        v = str(v)
        if not SAFE_ID.match(v):
            raise ZetarisError(f"unsafe parameter value: {v!r}")
        return "'" + v.replace("'", "''") + "'"

    def sub(m):
        name = m.group(1)
        if name not in params:
            raise ZetarisError(f"missing parameter :{name}")
        v = params[name]
        if isinstance(v, (list, tuple)):
            return "(" + ",".join(lit(x) for x in v) + ")"
        return lit(v)

    return re.sub(r"(?<!:):([A-Za-z_]\w*)", sub, sql)


class ZetarisClient:
    def __init__(self, mode=None, approver=None, log_root=None):
        load_env()
        self.mode = mode or ("snapshot" if flag("SAMPLE_MODE") else os.environ.get("ZETARIS_MODE", "live"))
        self.approver = approver
        self.log_root = log_root
        self.login_url = os.environ.get("ZETARIS_LOGIN_URL", "http://localhost:3000").rstrip("/")
        self.rest_url = os.environ.get("ZETARIS_REST_URL", "http://localhost:8888/api/v1.0").rstrip("/")
        self.timeout = int(os.environ.get("ZETARIS_TIMEOUT", "120"))
        self._token = None
        self._org = None

    # --- transport -----------------------------------------------------
    def _post(self, url, body, headers=None):
        req = urllib.request.Request(url, json.dumps(body).encode(),
                                     {"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, json.loads(r.read().decode() or "null")
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="replace")
            try:
                return e.code, json.loads(text)
            except ValueError:
                return e.code, {"msg": text[:400]}
        except (urllib.error.URLError, OSError) as e:
            raise ZetarisError(f"cannot reach Zetaris: {getattr(e, 'reason', e)}") from e

    def _login(self):
        code, body = self._post(self.login_url + "/api/auth/login", {
            "username": os.environ.get("ZETARIS_USER", ""),
            "password": os.environ.get("ZETARIS_PASSWORD", "")})
        if code != 200 or not body or "idToken" not in body:
            raise ZetarisError(f"login failed: HTTP {code}")
        self._token, self._org = body["idToken"], str(body["orgId"])

    def _run_live(self, sql, limit):
        for attempt in range(2):
            if not self._token:
                self._login()
            code, body = self._post(self.rest_url + "/sql-editor/sqls/run",
                                    {"queryId": str(uuid.uuid4()), "sql": sql,
                                     "source": "SqlEditor", "limit": limit},
                                    {"Authorization": "Bearer " + self._token, "X-Org-ID": self._org,
                                     "X-Request-ID": str(uuid.uuid4())})
            if code == 401 and attempt == 0:
                self._token = None
                continue
            if code != 200:
                raise ZetarisError((body or {}).get("msg", f"HTTP {code}"))
            return body.get("headers") or [], body.get("data") or []
        raise ZetarisError("unauthorized")

    # --- public --------------------------------------------------------
    def query(self, sql, params=None, limit=500, purpose="", agent="unknown"):
        """Run one statement and return (rows as dicts, receipt)."""
        text = normalize(bind(sql, params or {}))
        kind = classify(text)
        if NEVER.search(text):
            return [], self._receipt(text, [], [], "error", purpose, agent,
                                     error="blocked: INVALIDATE CACHE is never sent")
        if kind == "write":
            ok = bool(self.approver and self.approver(text, purpose))
            append("write_guard", {"sql": text, "agent": agent, "approved": ok}, self.log_root)
            if not ok:
                raise WriteRefused(f"write statement needs a key approval: {text[:120]}")
        key = sha(text)
        snap = SNAPSHOTS / f"{key}.json"
        try:
            if self.mode == "snapshot":
                if not snap.exists():
                    return [], self._receipt(text, [], [], "not_run", purpose, agent,
                                             error="no snapshot for this query")
                saved = json.loads(snap.read_text())
                headers, data = saved["headers"], saved["data"]
            else:
                headers, data = self._run_live(text, limit)
                if kind == "read":
                    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
                    snap.write_text(canonical({"sql": text, "headers": headers, "data": data}))
        except ZetarisError as e:
            return [], self._receipt(text, [], [], "error", purpose, agent, error=str(e))
        rows = [dict(zip(headers, r)) for r in data]
        status = "ok" if rows or kind == "write" else "empty"
        return rows, self._receipt(text, headers, data, status, purpose, agent)

    def setup(self, sql):
        """Setup DDL run by scripts/, outside agent traffic and the write guard."""
        return self._run_live(sql.strip().rstrip(";"), 50)  # keep newlines: COMPILE USL needs them

    def _receipt(self, text, headers, data, status, purpose, agent, error=None):
        ran_at = now_iso()
        result_sha = sha(canonical({"h": headers, "d": data}))
        r = {
            # content-addressed: the same query with the same result always gets the same id, so a
            # claim's citation stays valid across runs (and recorded runs replay exactly)
            "receipt_id": "rcpt_" + sha(text + "|" + status + "|" + result_sha + "|" + (error or ""))[:12],
            "sql": text,
            "sources": sorted(set(re.findall(r"\b((?:lightning\.metastore\.humankey\.northwind|northwind_erp|gleif)\.\w+)", text, re.I))),
            "row_count": len(data),
            "ran_at": ran_at,
            "result_sha256": result_sha,
            "status": status,
            "mode": self.mode,
            # Freshness: "live" was read from the source just now (REST tables re-call their API on every
            # query, and HumanKey never caches them); "recorded" is a replay from a saved snapshot.
            "freshness": "live" if self.mode == "live" else "recorded",
            "rest_backed": bool(re.search(r"\bgleif\b|northwind\.vendors\b", text, re.I)),
            "purpose": purpose,
            "agent": agent,
        }
        if error:
            r["error"] = error
        append("receipts", r, self.log_root)
        return r
