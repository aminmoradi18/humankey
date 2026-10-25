// Replay layer for the static page (site/, built by scripts/build_site.py). It answers the web view's
// API calls from replay.json: the state of a real sample run, and what the real gate and the emulator key
// did for each invoice when approved or rejected. Nothing is signed or computed here.
(() => {
  "use strict";
  const GATE_KINDS = ["blocked", "rejected", "timeout", "refused", "executed", "forgery_attempt", "contract.refused"];
  const css = document.createElement("style");
  css.textContent = "#replayBanner{background:#1d2433;color:#e8ecf4;font:13px/1.45 system-ui,sans-serif;padding:9px 16px;text-align:center}" +
    "#replayBanner a{color:#9fb3ff;font-weight:600}";
  document.head.appendChild(css);
  const ready = fetch("replay.json").then((r) => r.json());
  let S = null, cur = null;
  const reply = (body, status = 200) => new Response(JSON.stringify(body), {status, headers: {"Content-Type": "application/json"}});
  const item = (inv) => S.state.items.find((x) => x.invoice_id === inv);
  function apply(inv, o) {
    const x = item(inv);
    x.paid = o.paid;
    x.gate_event = [...o.events].reverse().find((e) => GATE_KINDS.includes(e.kind)) || null;
    S.state.events.push(...o.events);
    S.state.device.pending = null; S.state.device.busy = null; cur = null;
  }
  const real = window.fetch.bind(window);
  window.fetch = async (url, opts = {}) => {
    const path = String(url);
    if (!path.startsWith("/api/")) return real(url, opts);
    if (!S) S = await ready;
    const body = opts.body ? JSON.parse(opts.body) : {};
    if (path === "/api/state") return reply(S.state);
    if (path === "/api/release") {
      const inv = body.invoice_id, x = item(inv), o = S.outcomes[inv];
      if (!x || !o) return reply({error: "unknown invoice"}, 404);
      if (x.paid || x.gate_event) return reply({error: "Already decided in this replay. Reload the page to start again."}, 409);
      if (o.approve.screens.length) {
        cur = {inv, step: 0};
        S.state.device.busy = inv; S.state.device.pending = o.approve.screens[0];
      } else apply(inv, o.approve);
      return reply({ok: true});
    }
    if (path === "/api/emulator") {
      if (!cur) return reply({error: "nothing on the key"}, 409);
      const o = S.outcomes[cur.inv][body.decision];
      if (!o) return reply({error: "no recording for that answer"}, 404);
      cur.step += 1;
      if (cur.step < o.screens.length) S.state.device.pending = o.screens[cur.step];
      else apply(cur.inv, o);
      return reply({ok: true});
    }
    if (path === "/api/remember") return reply({error: "Not in the replay. Run it yourself to teach H-MEM."}, 400);
    return reply({error: "not in the replay"}, 404);
  };
})();
