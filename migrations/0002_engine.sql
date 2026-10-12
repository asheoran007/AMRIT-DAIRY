-- 0002_engine.sql: conversation engine state, draft-order link, lead tags, retention helper

CREATE TABLE IF NOT EXISTS conversation_session (
  session_id TEXT PRIMARY KEY,
  brand_id TEXT NOT NULL,
  phone_e164 TEXT NOT NULL,
  contact_id TEXT,
  state TEXT NOT NULL,              -- NEW|PRODUCT|PACK|QTY|ADDRESS_CONFIRM|ADDRESS_COLLECT|QUOTE|AWAITING_PAYMENT|PAID|ORDERED|TRACKING|HANDOFF
  product_key TEXT,
  variant_id TEXT,
  quantity INTEGER,
  address_json TEXT,                -- confirmed address for THIS purchase only
  address_confirmed INTEGER NOT NULL DEFAULT 0,
  quote_json TEXT,
  quote_ref TEXT,                   -- unique quote reference (idempotency key for draft order)
  draft_order_id TEXT,
  invoice_url TEXT,
  shopify_order_id TEXT,
  shopify_order_name TEXT,
  payment_id TEXT,
  last_message_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (brand_id, phone_e164)
);

CREATE TABLE IF NOT EXISTS lead_tag (
  lead_id TEXT NOT NULL REFERENCES lead(lead_id),
  tag TEXT NOT NULL,
  added_at TEXT NOT NULL,
  PRIMARY KEY (lead_id, tag)
);

-- draft order -> session mapping, unique so a duplicate webhook can never create a second order record
CREATE TABLE IF NOT EXISTS draft_order_ref (
  draft_order_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES conversation_session(session_id),
  quote_ref TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  completed_order_id TEXT,
  completed_at TEXT
);

INSERT INTO schema_migrations(version, applied_at) VALUES ('0002_engine', CURRENT_TIMESTAMP);
