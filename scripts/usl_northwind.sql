-- Unified Semantic Layer "northwind": one model over both sources (ERP + GLEIF), compiled as ONE statement.
-- Activation queries and data-quality rules: scripts/setup_zetaris.py (USL_ACTIVATE, USL_DQ).
COMPILE USL northwind DEPLOY NAMESPACE lightning.metastore.humankey DDL
CREATE TABLE vendors (
  vendor_id varchar(16) NOT NULL PRIMARY KEY,
  name varchar(200),
  legal_name varchar(200),
  country varchar(8),
  lei varchar(32),
  email_domain varchar(200),
  iban_last4 varchar(8),
  iban_hash varchar(32),
  bank_since date,
  contact_name varchar(100),
  gleif_legal_name varchar(200),
  gleif_entity_status varchar(32),
  gleif_registration_status varchar(32)
);
CREATE TABLE invoices (
  invoice_id varchar(16) NOT NULL PRIMARY KEY,
  vendor_id varchar(16) FOREIGN KEY REFERENCES vendors(vendor_id),
  po_id varchar(16),
  amount decimal(12,2),
  currency varchar(8),
  issued_at timestamp,
  iban_hash varchar(32),
  iban_last4 varchar(8),
  memo varchar(2000),
  email_id varchar(16)
);
CREATE TABLE payments (
  payment_id varchar(16) NOT NULL PRIMARY KEY,
  vendor_id varchar(16) FOREIGN KEY REFERENCES vendors(vendor_id),
  invoice_id varchar(16),
  amount decimal(12,2),
  paid_at timestamp,
  iban_hash varchar(32),
  iban_last4 varchar(8)
);
CREATE TABLE purchase_orders (
  po_id varchar(16) NOT NULL PRIMARY KEY,
  vendor_id varchar(16) FOREIGN KEY REFERENCES vendors(vendor_id),
  amount decimal(12,2),
  currency varchar(8),
  created_at timestamp,
  status varchar(16)
);
CREATE TABLE bank_change_requests (
  request_id varchar(16) NOT NULL PRIMARY KEY,
  vendor_id varchar(16) FOREIGN KEY REFERENCES vendors(vendor_id),
  new_iban_hash varchar(32),
  new_iban_last4 varchar(8),
  requested_at timestamp,
  channel varchar(32),
  verified_by varchar(200)
);
CREATE TABLE emails (
  email_id varchar(16) NOT NULL PRIMARY KEY,
  from_addr varchar(200),
  from_domain varchar(200),
  to_addr varchar(200),
  received_at timestamp,
  subject varchar(500),
  body varchar(4000)
)
