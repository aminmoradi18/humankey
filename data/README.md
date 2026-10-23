# Data

| What | Source | Licence | Size | In Zetaris as |
|---|---|---|---|---|
| Vendors, POs, payments, e-mails, bank-change requests, invoices | `data/gen.py` (synthetic, seed 7) | project | 12 vendors, 146 POs, 132 payments, 15 invoices | `northwind_erp.*` (PostgreSQL datasource) |
| Legal entity records of the vendors | GLEIF LEI API, https://api.gleif.org | CC0 | one request, 11 records | `gleif.lei_entities` (REST table + view) |
| Correct answers | `data/labels.csv` | project | 15 rows | never loaded, never shown to the agents |

The vendors are real companies with their real LEIs; every invoice, amount, account and e-mail is invented.
The planted cases are impersonation frauds (look-alike domain + new account, invented vendor with an unknown
LEI, no PO, amount spike, split invoices, prompt injection) plus clean invoices and a verified bank change.

Regenerate: `python3 data/gen.py`, then `./run.sh setup`.
`data/snapshots/` and `data/llm_cache/` hold a recorded run so `./run.sh sample` works without Zetaris or a model.
