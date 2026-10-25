# One run through Meterless Nightshift (25 Oct 2026)

`LLM_PROVIDER=meterless ./run.sh offline`: Zetaris query results replayed from the recording, every model
call made live through a real Nightshift 1.2.3 (Windows 11 laptop on the same Wi-Fi, hotspot mode, Runtime
token). HumanKey ran on the Linux laptop and called `http://<windows-laptop>:8088/v1`. Nightshift held the
OpenRouter key and routed each request to `openrouter/nvidia/nemotron-3.5-lightning`.

- 61 model calls, 61 answered, each with a Nightshift compute receipt (`llm.jsonl`, field `receipt`:
  receipt id, routed model, cost, egress, timings).
- Cost by the receipts: $0.0135 for the whole run (Nightshift's own baseline for the same calls: $0.96).
- Median call 5.9 s, slowest 30 s.
- Scores (`console.txt`): frauds stopped before payment 9 / 9, clean invoices wrongly blocked 0 / 8,
  proposal equals the label 15 / 17.

`first_attempt/` is the run before it, kept as it happened: 57 calls, 44 answered. Nightshift reports a
reasoning model that thinks past its token budget as HTTP 502 `..._empty_response`; our provider did not
know that code, so it did not retry with more room. Those invoices fell back to "AI offline: ask" and went
to the person; no fraud was paid. `llm/provider.py` now treats that 502 as an empty answer (retry with a
larger budget) and a 503 as a dropped link, which is the run above.

Not recorded here: the free `:free` route of the same model returned empty answers under parallel load,
so the run used the paid route of the same model.
