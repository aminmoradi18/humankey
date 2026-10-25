# One offline run (25 Oct 2026)

`./run.sh offline`: Zetaris query results replayed from the recording, every model call made live
(`nemotron-mini` through Ollama on the laptop GPU, 68 calls, all answered). These are that run's own logs:
`events.jsonl` (agents), `llm.jsonl` (model calls, with the model that answered), `receipts.jsonl`
(Zetaris queries), and its scores, `RESULTS.md`.

Compared with the recorded run that sample mode replays, the model decided some borderline invoices
differently (for example INV0001 was escalated, not cleared). No fraud was paid in either.
