#!/bin/bash
# Start the "Northwind ERP" Postgres that Zetaris federates as a datasource,
# and (re)load data/out/*.csv into it. Idempotent.
set -euo pipefail
cd "$(dirname "$0")/.."
NAME=humankey-erp
NET=${ZETARIS_NETWORK:-zetaris_default}
PGPASS=${ERP_DB_PASSWORD:-northwind}

if ! docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  docker run -d --name "$NAME" --network "$NET" --network-alias erp-db \
    -e POSTGRES_USER=northwind -e POSTGRES_PASSWORD="$PGPASS" -e POSTGRES_DB=erp \
    -p 127.0.0.1:55432:5432 postgres:16 >/dev/null
fi
docker start "$NAME" >/dev/null
until docker exec "$NAME" psql -U northwind -d erp -tAc "select 1" >/dev/null 2>&1; do sleep 1; done

docker exec -i "$NAME" psql -q -v ON_ERROR_STOP=1 -U northwind -d erp < scripts/erp_schema.sql
for t in vendors purchase_orders payments emails bank_change_requests invoices; do
  docker exec -i "$NAME" psql -q -v ON_ERROR_STOP=1 -U northwind -d erp \
    -c "\\copy $t FROM STDIN WITH (FORMAT csv, HEADER true)" < "${DATA_OUT:-data/out}/$t.csv"
  printf '%-22s %s rows\n' "$t" "$(docker exec "$NAME" psql -tA -U northwind -d erp -c "select count(*) from $t")"
done
