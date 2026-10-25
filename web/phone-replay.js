// Replay layer for the phone app's web build on the static page (site/phone/, built by
// scripts/build_site.py). It answers the app's /api/phone/* calls from ../replay.json: the queue and
// profile from a real sample run, and what the real gate did when a paired phone approved or rejected.
// The app still checks the contract and signs with its own key in the browser; the gate's answer shown
// afterwards is the recorded one.
(() => {
  "use strict";
  const ready = fetch("../replay.json").then((r) => r.json()).then((d) => d.phone);
  let P = null, cur = null;
  const reply = (body, status = 200) => new Response(JSON.stringify(body), {status, headers: {"Content-Type": "application/json"}});
  const real = window.fetch.bind(window);
  window.fetch = async (url, opts = {}) => {
    const path = String(url), i = path.indexOf("/api/phone/");
    if (i < 0) return real(url, opts);
    if (!P) P = await ready;
    const route = path.slice(i), body = opts.body ? JSON.parse(opts.body) : {};
    const q = P.queue;
    if (route === "/api/phone/queue") return reply(q);
    if (route === "/api/phone/devices") return reply(P.devices);
    if (route === "/api/phone/pair") {
      if (!/^\d{6}$/.test(String(body.code || ""))) return reply({error: "wrong pairing code"}, 403);
      return reply({ok: true, id: body.id});
    }
    if (route === "/api/phone/release") {
      const it = q.items.find((x) => x.invoice_id === body.invoice_id), o = P.outcomes[body.invoice_id];
      if (!it || it.tier !== "phone" || !o) return reply({error: "this payment needs the hardware key"}, 403);
      if (it.done) return reply({error: "already decided in this replay; reload to start again"}, 409);
      cur = body.invoice_id;
      q.pending = {...o.approve.pending, invoice_id: cur}; q.busy = cur;
      return reply({ok: true}, 202);
    }
    if (route === "/api/phone/decision") {
      const o = cur && P.outcomes[cur][body.d];
      if (!o) return reply({error: "nothing waiting for this phone"}, 409);
      const it = q.items.find((x) => x.invoice_id === cur);
      it.done = (o.paid && o.paid.released_by) || ([...o.events].reverse().find((e) => e.kind === "rejected") || {}).kind || o.result.toLowerCase();
      P.devices.paid.phone = (P.devices.paid.phone || 0) + (o.paid ? 1 : 0);
      q.pending = null; q.busy = null; cur = null;
      return reply({ok: true});
    }
    return reply({error: "not in the replay"}, 404);
  };
})();
