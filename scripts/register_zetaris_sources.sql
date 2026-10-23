-- Our two sources in Zetaris. Run with: python3 scripts/setup_zetaris.py
-- ${ERP_DB_PASSWORD} and ${LEI_LIST} are filled in by the script.

-- 1. Northwind ERP: PostgreSQL (started by scripts/erp_db.sh), federated as a JDBC datasource.
CREATE DATASOURCE northwind_erp DESCRIBE BY "Northwind Fabrication ERP, synthetic PostgreSQL" OPTIONS (jdbcdriver "org.postgresql.Driver", jdbcurl "jdbc:postgresql://erp-db:5432/erp", username "northwind", password "${ERP_DB_PASSWORD}", fetchsize "1000");
REGISTER DATASOURCE TABLES FROM northwind_erp;

-- 2. GLEIF: the public Legal Entity Identifier registry (CC0), live REST API.
-- One request fetches the LEI records for every LEI in our vendor master.
CREATE LIGHTNING DATABASE GLEIF_REST DESCRIBE BY "GLEIF LEI registry, public REST API, CC0";
CREATE SCHEMASTORE CONTAINER gleif;
CREATE LIGHTNING REST TABLE lei_records FROM GLEIF_REST REQUEST(endpoint "https://api.gleif.org/api/v1/lei-records?filter%5Blei%5D=${LEI_LIST}&page%5Bsize%5D=50", method "get", response_type "json", http_encoding "URLENCODED") HEADER () BODY ();
CREATE SCHEMASTORE VIEW lei_entities WITH CONTAINER gleif AS SELECT r.attributes.lei AS lei, r.attributes.entity.legalName.name AS legal_name, r.attributes.entity.legalAddress.country AS country, r.attributes.entity.status AS entity_status, r.attributes.registration.status AS registration_status, r.attributes.registration.nextRenewalDate AS next_renewal FROM gleif_rest.lei_records LATERAL VIEW explode(data) AS r;
