-- Register the Northwind ERP Postgres (started by scripts/erp_db.sh) in Zetaris.
-- Run with: python3 scripts/setup_zetaris.py   (password comes from ERP_DB_PASSWORD in .env)
CREATE DATASOURCE northwind_erp DESCRIBE BY "Northwind Fabrication ERP, synthetic PostgreSQL" OPTIONS (jdbcdriver "org.postgresql.Driver", jdbcurl "jdbc:postgresql://erp-db:5432/erp", username "northwind", password "${ERP_DB_PASSWORD}", fetchsize "1000");
REGISTER DATASOURCE TABLES FROM northwind_erp;
