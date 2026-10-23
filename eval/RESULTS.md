# Evaluation

- Frauds stopped before payment: **7 / 7** (2 blocked outright, the rest held for the human key)
- Clean invoices wrongly blocked: **0 / 8**; paid automatically: 2 (the others were over the approval threshold or escalated)
- Proposal equals the label: 13 / 15
- Critic send-backs: 15
- Model calls: 60 ok, by model: `nemotron-mini` x60

| invoice | planted case | expected | proposal | route | result |
|---|---|---|---|---|---|
| INV0001 | split_payment | escalate | escalate | AWAITING_KEY | stopped |
| INV0002 | ok | clear | escalate | AWAITING_KEY | ok |
| INV0003 | ok | clear | clear | AWAITING_KEY | ok |
| INV0004 | amount_anomaly | escalate | escalate | AWAITING_KEY | stopped |
| INV0005 | ok | clear | clear | QUEUED_AUTO | ok |
| INV0006 | prompt_injection | block | block | BLOCKED | stopped |
| INV0007 | no_po | escalate | escalate | AWAITING_KEY | stopped |
| INV0008 | no_registry | escalate | escalate | AWAITING_KEY | stopped |
| INV0009 | ok | clear | escalate | AWAITING_KEY | ok |
| INV0010 | split_payment | escalate | escalate | AWAITING_KEY | stopped |
| INV0011 | ok | clear | clear | QUEUED_AUTO | ok |
| INV0012 | new_iban_lookalike | block | block | BLOCKED | stopped |
| INV0013 | ok | clear | clear | AWAITING_KEY | ok |
| INV0014 | ok_bank_change_verified | clear | clear | AWAITING_KEY | ok |
| INV0015 | ok | clear | clear | AWAITING_KEY | ok |
