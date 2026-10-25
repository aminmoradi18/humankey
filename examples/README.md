# Sample inputs and outputs

Three invoices from one real run (`./run.sh offline` on 25 Oct 2026): Zetaris query results replayed from
the recording, every model call made live (`nemotron-mini` through Ollama, 68 calls). The full logs of that
run are in `evidence/offline_run/`. The files here were copied from those logs by
`python3 scripts/make_examples.py evidence/offline_run INV0006 INV0003 INV0016`; none were written by hand.
Its scores are in `evidence/offline_run/RESULTS.md`. All company data is synthetic (`data/gen.py`).

| Invoice | Input | What the run decided |
|---|---|---|
| INV0006 | $1,863, a clean invoice (the generator's label) | `QUEUED_AUTO`: under the auto-pay limit, no risk found |
| INV0003 | $1,846, new bank account, e-mail from a look-alike domain | `BLOCKED`: the Investigator proposed "clear"; the Critic sent it back twice, and the Risk agent blocked it |
| INV0016 | $1,892, memo that tells the AI to approve | `BLOCKED`: the Guard flagged the injection |

`input.json` is the invoice and its vendor as the agents' data had them. `output.json` is every event the run
logged for that invoice (plan, draft, critiques, revision, World Model update, decision, contract).
A second run can word the plan differently, and a borderline invoice can land on a different route; the
gate rules (risk flags, limits, the key) stay the same.
