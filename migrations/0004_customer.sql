-- Shopify customer id resolved at address step (used for draftOrderCalculate / create)
ALTER TABLE conversation_session ADD COLUMN customer_id TEXT;
INSERT INTO schema_migrations(version, applied_at) VALUES ('0004_customer', CURRENT_TIMESTAMP);
