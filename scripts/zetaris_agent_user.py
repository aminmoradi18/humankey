"""A least-privilege Zetaris identity for the agents, and a check of what Zetaris itself refuses.

Usage: python3 scripts/zetaris_agent_user.py
Needs ZETARIS_AGENT_USER / ZETARIS_AGENT_PASSWORD in .env: a non-administrator user created once in the
Zetaris UI (Management > Users, level General). USL access also needs three interface steps that SQL grants
do not replace: View on Unified Semantic Layer (Management > Permissions), the role under the USL namespace's
Access Control, and the role with SELECT per table under the model's Access Control. The builder login (ZETARIS_USER) creates the role and
the grants; then every check runs as the agent user, so the answers come from Zetaris, not from our
own write guard.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.env import load_env  # noqa: E402
from zetaris.client import ZetarisClient  # noqa: E402

ROLE = "humankey_readonly"
USL = "lightning.metastore.humankey.northwind"

load_env()
user = os.environ.get("ZETARIS_AGENT_USER", "").upper()
if not user:
    sys.exit("set ZETARIS_AGENT_USER and ZETARIS_AGENT_PASSWORD in .env first")
builder = ZetarisClient(mode="live")
for sql in (f"CREATE ROLE '{ROLE}' DESCRIBE BY 'HumanKey agents read only'",
            f"ASSIGN USER '{user}' TO ROLE '{ROLE}'",
            f"GRANT SELECT ON USL NAMESPACE {USL} TO ROLE '{ROLE}'",
            f"ASSIGN DATASOURCE northwind_erp TO ROLE '{ROLE}'",
            f"GRANT SELECT ON DATASOURCE NAMESPACE northwind_erp TO ROLE '{ROLE}'"):
    try:
        builder.setup(sql)
    except Exception as e:  # already there on a second run
        if "exist" not in str(e).lower() and "already" not in str(e).lower():
            print("setup:", sql[:60], "->", str(e)[:120])

os.environ["ZETARIS_USER"], os.environ["ZETARIS_PASSWORD"] = user, os.environ["ZETARIS_AGENT_PASSWORD"]
agent = ZetarisClient(mode="live")
CHECKS = [
    ("read the ERP", "SELECT count(*) AS n FROM northwind_erp.invoices", True),
    ("write to the ERP", "INSERT INTO northwind_erp.payments SELECT * FROM northwind_erp.payments LIMIT 1", False),
    ("register a new source", "CREATE LIGHTNING DATABASE evil DESCRIBE BY 'x'", False),
    ("read the USL (granted)", f"SELECT count(*) AS n FROM {USL}.invoices", True),
    ("run a DQ rule on the USL (granted)", f"RUN DQ vendor_id TABLE {USL}.invoices", True),
    ("read the GLEIF view directly", "SELECT lei FROM gleif.lei_entities", False),
]
for name, sql, expected in CHECKS:
    try:
        agent.setup(sql)
        ok, why = True, "allowed"
    except Exception as e:
        ok, why = False, str(e).splitlines()[0][:150]
    verdict = "as expected" if ok == expected else "NOT as expected"
    print(f"{name:36} {'allowed' if ok else 'refused':8} {verdict:16} {'' if ok else why}")
