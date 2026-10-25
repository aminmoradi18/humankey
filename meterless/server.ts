// HumanKey World Model service: the shared state every agent reads and writes.
//
// Engine: Meterless World Model reference (world-model/src, Apache-2.0, see
// world-model/UPSTREAM_COMMIT). This file maps our domain onto it:
//   receipt  -> observation (stable externalId = query hash + result hash, so re-ingest is a no-op)
//   finding  -> fact about the vendor entity, provenance = the agent that produced it
//   decision -> fact about the invoice entity + "billed_by" edge to the vendor
// and serves a context builder the planner and critic read before they act.
//
// Memory: Meterless H-MEM reference (hmem/src, Apache-2.0). What a human says when
// answering on the key is stored as a long-term record tagged with the vendor and the
// check it explains, with provenance; the risk agent recalls it next time, and the
// human's later answers feed back (helpful / wrong) into its confidence. H-MEM's conflict
// detection runs on every new answer: two answers for the same vendor and check that contradict
// each other are both held back from recall until the person picks one, which supersedes the other.
//
// Run: npx tsx meterless/server.ts      (127.0.0.1:${WORLD_PORT:-8711})
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { WorldModel, entityIdFromName, sha256Hex } from "./world-model/src/index.ts";
import { HMEM } from "./hmem/src/index.ts";

const here = path.dirname(fileURLToPath(import.meta.url));
const port = Number(process.env.WORLD_PORT ?? 8711);
const world = new WorldModel({
  namespace: process.env.WORLD_NAMESPACE ?? "humankey",
  storage: "local",
  directory: process.env.WORLD_DIR ?? path.join(here, "..", "data"),
});

const memory = new HMEM({ persistDir: process.env.HMEM_DIR ?? path.join(here, "..", "data", "hmem"), actor: "humankey" });

type Receipt = { receipt_id: string; sql: string; result_sha256: string; status: string; ran_at: string; agent: string; purpose: string };
type Finding = { check: string; signal: string; summary: string; facts: Record<string, unknown>; receipt_ids: string[] };

const prov = (by: string, at: string, runId?: string) => ({ kind: "agent", by, at, runId });
const vendorId = (name: string) => entityIdFromName("vendor", name);

async function observe(body: { invoice_id: string; vendor: string; run_id: string; receipts: Receipt[]; findings: Finding[] }) {
  const { invoice_id, vendor, run_id, receipts, findings } = body;
  const at = receipts[0]?.ran_at ?? new Date().toISOString();
  const report = await world.ingest(
    receipts.map((r) => ({
      externalId: `rcpt:${sha256Hex(r.sql).slice(0, 16)}:${r.result_sha256.slice(0, 16)}`,
      type: "receipt",
      title: `${r.purpose} (${invoice_id})`,
      text: r.sql,
      at: r.ran_at,
      source: { kind: "pipeline", by: `zetaris:${r.agent}`, at: r.ran_at, runId: run_id, checksum: r.result_sha256 },
      entities: [{ type: "vendor", name: vendor }, { type: "invoice", name: invoice_id }],
    })),
  );
  const v = await world.upsertEntity({ type: "vendor", name: vendor, source: prov("investigator", at, run_id) });
  const inv = await world.upsertEntity({ type: "invoice", name: invoice_id, source: prov("investigator", at, run_id) });
  await world.relate({ from: inv.id, type: "billed_by", to: v.id, source: prov("investigator", at, run_id) });
  for (const f of findings) {
    if (f.check === "invoice_header") continue;
    await world.assertFact({
      about: v.id, predicate: f.check, confidence: f.signal === "unknown" ? 0.3 : 0.9,
      value: { signal: f.signal, summary: f.summary, invoice_id, receipt_ids: f.receipt_ids },
      source: prov("investigator", at, run_id),
    });
  }
  return { vendor_entity: v.id, invoice_entity: inv.id, ingest: { processed: report.processed, skippedUnchanged: report.skippedUnchanged, changed: report.changed } };
}

async function decide(body: { invoice_id: string; vendor: string; run_id: string; agent: string; decision: Record<string, unknown> }) {
  const at = new Date().toISOString();
  const inv = await world.upsertEntity({ type: "invoice", name: body.invoice_id, source: prov(body.agent, at, body.run_id) });
  await world.assertFact({ about: inv.id, predicate: "decision", value: body.decision, source: prov(body.agent, at, body.run_id) });
  const v = await world.upsertEntity({ type: "vendor", name: body.vendor, source: prov(body.agent, at, body.run_id) });
  await world.assertFact({ about: v.id, predicate: "last_decision", value: { invoice_id: body.invoice_id, ...body.decision }, source: prov(body.agent, at, body.run_id) });
  return { ok: true };
}

function context(vendor: string) {
  const id = vendorId(vendor);
  const g = world.view("graph");
  const facts = world.view("facts").about(id).filter((f) => !f.supersededBy);
  const invoices = g.incoming(id, { via: "billed_by" }).map((e) => {
    const d = world.view("facts").about(e.id).find((f) => f.predicate === "decision" && !f.supersededBy);
    return { invoice: e.name, decision: d?.value ?? null };
  });
  return {
    vendor, entity: id, known: Boolean(g.entity(id)),
    facts: facts.map((f) => ({ predicate: f.predicate, value: f.value, by: f.source.by, at: f.source.at, confidence: f.confidence })),
    invoices,
  };
}

// Open conflicts between human answers, keyed "a|b". H-MEM keeps its own list, but scan() re-adds
// every pair it sees, so this is the deduplicated view; it is rebuilt by a scan at start.
const open = new Map<string, { id: string; a: string; b: string; reason: string; confidence: number }>();
const scope = (id: string) => (memory.store.get(id)?.tags ?? []).filter((t) => t.startsWith("vendor:") || t.startsWith("check:")).sort().join(" ");

function scanConflicts() {
  for (const c of memory.conflicts.scan()) {
    const a = memory.store.get(c.memoryA), b = memory.store.get(c.memoryB);
    if (!a || !b || a.supersededBy || b.supersededBy || scope(a.id) !== scope(b.id) || !scope(a.id)) continue;
    const key = [a.id, b.id].sort().join("|");
    if (!open.has(key)) open.set(key, { id: c.id, a: a.id, b: b.id, reason: c.reason, confidence: c.confidence });
  }
}
const inConflict = (id: string) => [...open.values()].some((c) => c.a === id || c.b === id);
const view = (id: string) => {
  const m = memory.store.get(id)!;
  return { id, content: m.content, refs: m.provenance?.refs ?? [], source: m.source };
};
const conflictsOf = (id?: string) =>
  [...open.values()].filter((c) => !id || c.a === id || c.b === id)
    .map((c) => ({ ...c, memories: [view(c.a), view(c.b)], scope: scope(c.a) }));

function remember(b: { vendor: string; check: string; content: string; by: string; invoice_id: string }) {
  const rec = memory.add({
    content: b.content, type: "factual", layer: "long_term", confidence: 0.9,
    tags: [`vendor:${b.vendor}`, `check:${b.check}`], source: `human:${b.by}`,
    provenance: { origin: "user_correction", learnedAt: Date.now(), label: "answer given on the key", refs: [b.invoice_id] },
  });
  scanConflicts();
  return { id: rec.id, confidence: rec.confidence, conflicts: conflictsOf(rec.id) };
}

// The person chose which answer holds: the other is superseded (kept, never deleted, out of recall).
function resolve(b: { keep: string; drop: string; by: string }) {
  const key = [b.keep, b.drop].sort().join("|");
  if (!open.has(key)) throw new Error(`no open conflict between ${b.keep} and ${b.drop}`);
  memory.store.update(b.drop, { supersededBy: b.keep });
  open.delete(key);
  for (const [k, c] of open) if (c.a === b.drop || c.b === b.drop) open.delete(k);
  return { ok: true, kept: view(b.keep), superseded: b.drop, by: b.by };
}

function recall(vendor: string, check: string, q: string) {
  // Scope by tags (vendor + check), rank by H-MEM's score; no relevance cut-off, because the
  // scope is exact and the caller applies its own confidence floor.
  const res = memory.query(`${check} ${q}`, { topN: 50, threshold: 0 });
  const hits = res.memories
    .filter((m) => m.memory.tags.includes(`vendor:${vendor}`) && m.memory.tags.includes(`check:${check}`) && !m.memory.supersededBy)
    // Contradicting answers settle nothing until the person resolves them.
    .filter((m) => !inConflict(m.memory.id))
    .map((m) => ({ id: m.memory.id, content: m.memory.content, confidence: m.memory.confidence, relevance: m.relevance,
                   source: m.memory.source, refs: m.memory.provenance?.refs ?? [] }));
  return { hits, trace: res.trace };
}

const routes: Record<string, (q: URLSearchParams, b: any) => unknown> = {
  "POST /memory/add": (_q, b) => remember(b),
  "GET /memory/recall": (q) => recall(q.get("vendor") ?? "", q.get("check") ?? "", q.get("q") ?? ""),
  "POST /memory/feedback": (_q, b) => { memory.feedback(b.id, b.kind); return { ok: true }; },
  "GET /memory/conflicts": () => conflictsOf(),
  "POST /memory/resolve": (_q, b) => resolve(b),
  "GET /health": () => ({ ok: true, engine: "meterless-world-model-reference", events: world.events().length }),
  "POST /observe": (_q, b) => observe(b),
  "POST /decision": (_q, b) => decide(b),
  "GET /context": (q) => context(q.get("vendor") ?? ""),
  "GET /audit": () => world.audit().slice(-200),
};

scanConflicts();

http.createServer(async (req, res) => {
  const url = new URL(req.url ?? "/", "http://x");
  const fn = routes[`${req.method} ${url.pathname}`];
  let raw = "";
  for await (const chunk of req) raw += chunk;
  try {
    if (!fn) throw Object.assign(new Error("not found"), { code: 404 });
    const out = await fn(url.searchParams, raw ? JSON.parse(raw) : {});
    res.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify(out));
  } catch (e: any) {
    res.writeHead(e.code === 404 ? 404 : 400, { "content-type": "application/json" }).end(JSON.stringify({ error: String(e.message ?? e) }));
  }
}).listen(port, "127.0.0.1", () => console.log(`world model on http://127.0.0.1:${port}`));
