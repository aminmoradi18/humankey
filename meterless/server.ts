// HumanKey World Model service: the shared state every agent reads and writes.
//
// Engine: Meterless World Model reference (world-model/src, Apache-2.0, see
// world-model/UPSTREAM_COMMIT). This file maps our domain onto it:
//   receipt  -> observation (stable externalId = query hash + result hash, so re-ingest is a no-op)
//   finding  -> fact about the vendor entity, provenance = the agent that produced it
//   decision -> fact about the invoice entity + "billed_by" edge to the vendor
// and serves a context builder the planner and critic read before they act.
//
// Run: npx tsx meterless/server.ts      (127.0.0.1:${WORLD_PORT:-8711})
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { WorldModel, entityIdFromName, sha256Hex } from "./world-model/src/index.ts";

const here = path.dirname(fileURLToPath(import.meta.url));
const port = Number(process.env.WORLD_PORT ?? 8711);
const world = new WorldModel({
  namespace: process.env.WORLD_NAMESPACE ?? "humankey",
  storage: "local",
  directory: process.env.WORLD_DIR ?? path.join(here, "..", "data"),
});

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

const routes: Record<string, (q: URLSearchParams, b: any) => unknown> = {
  "GET /health": () => ({ ok: true, engine: "meterless-world-model-reference", events: world.events().length }),
  "POST /observe": (_q, b) => observe(b),
  "POST /decision": (_q, b) => decide(b),
  "GET /context": (q) => context(q.get("vendor") ?? ""),
  "GET /audit": () => world.audit().slice(-200),
};

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
