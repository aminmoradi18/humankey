-- Northwind ERP schema (ARCHITECTURE.md section 3). Recreated on every load.
DROP TABLE IF EXISTS vendors, purchase_orders, payments, emails, bank_change_requests, invoices;
CREATE TABLE vendors (vendor_id text PRIMARY KEY, name text, legal_name text, country text,
  registry_id text, email_domain text, iban_last4 text, iban_hash text, bank_since date, contact_name text);
CREATE TABLE purchase_orders (po_id text PRIMARY KEY, vendor_id text, amount numeric(12,2),
  currency text, created_at timestamp, status text);
CREATE TABLE payments (payment_id text PRIMARY KEY, vendor_id text, invoice_id text,
  amount numeric(12,2), paid_at timestamp, iban_hash text, iban_last4 text);
CREATE TABLE emails (email_id text PRIMARY KEY, from_addr text, from_domain text, to_addr text,
  received_at timestamp, subject text, body text);
CREATE TABLE bank_change_requests (request_id text PRIMARY KEY, vendor_id text, new_iban_hash text,
  new_iban_last4 text, requested_at timestamp, channel text, verified_by text);
CREATE TABLE invoices (invoice_id text PRIMARY KEY, vendor_id text, po_id text, amount numeric(12,2),
  currency text, issued_at timestamp, iban_hash text, iban_last4 text, memo text, email_id text);
