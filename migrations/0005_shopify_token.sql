-- Offline (non-expiring) Admin API token obtained via the authorization-code grant (custom distribution), one row per shop.
CREATE TABLE IF NOT EXISTS shopify_token (
  shop_domain TEXT PRIMARY KEY,
  access_token TEXT NOT NULL,
  scope TEXT,
  obtained_at TEXT NOT NULL
);
INSERT INTO schema_migrations(version, applied_at) VALUES ('0005_shopify_token', CURRENT_TIMESTAMP);
