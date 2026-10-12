-- 0001_init.sql  (SQLite + PostgreSQL compatible subset; ids are app-generated UUID text)
-- Minimum data model from the kit: contact/channel, brand, consent ledger, identity map,
-- product cache, order refs, payment attempts, shipment refs/events, leads, support cases,
-- event inbox, outbox jobs, action ledger, audit log, rate-limit state, budget ledger.

CREATE TABLE IF NOT EXISTS schema_migrations (
  version TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS brand (
  brand_id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  store_domain TEXT,               -- e.g. kmaz1y-dq.myshopify.com (verified from order notification 2026-10-10)
  timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata',
  currency TEXT NOT NULL DEFAULT 'INR'
);

CREATE TABLE IF NOT EXISTS contact (
  contact_id TEXT PRIMARY KEY,
  brand_id TEXT NOT NULL REFERENCES brand(brand_id),
  display_name TEXT,
  created_at TEXT NOT NULL,
  review_required INTEGER NOT NULL DEFAULT 0,   -- shared/alternate numbers, cross-brand matches
  review_reason TEXT
);

CREATE TABLE IF NOT EXISTS contact_channel (
  channel_id TEXT PRIMARY KEY,
  contact_id TEXT NOT NULL REFERENCES contact(contact_id),
  brand_id TEXT NOT NULL REFERENCES brand(brand_id),
  channel_type TEXT NOT NULL,       -- whatsapp|email|phone|instagram
  address TEXT NOT NULL,            -- E.164 for phone/whatsapp, lowercase email
  verified INTEGER NOT NULL DEFAULT 0,
  UNIQUE (brand_id, channel_type, address)
);

-- Source identity mapping: brand + provider + provider id -> contact (never merge by phone alone)
CREATE TABLE IF NOT EXISTS identity_map (
  brand_id TEXT NOT NULL REFERENCES brand(brand_id),
  source TEXT NOT NULL,             -- shopify|razorpay|shiprocket|bitespeed
  source_id TEXT NOT NULL,
  contact_id TEXT NOT NULL REFERENCES contact(contact_id),
  PRIMARY KEY (brand_id, source, source_id)
);

-- Consent ledger: append-only, by purpose and channel; latest row wins per (contact, channel_type, purpose)
CREATE TABLE IF NOT EXISTS consent_ledger (
  consent_id TEXT PRIMARY KEY,
  brand_id TEXT NOT NULL REFERENCES brand(brand_id),
  contact_id TEXT NOT NULL REFERENCES contact(contact_id),
  channel_type TEXT NOT NULL,
  purpose TEXT NOT NULL,            -- marketing|service|voice_callback|cow_enquiry|export_waitlist
  status TEXT NOT NULL,             -- granted|withdrawn|stop_all
  evidence TEXT,                    -- redacted reference (message id / form id), never raw PII dump
  recorded_at TEXT NOT NULL,
  effective_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_consent_lookup ON consent_ledger(brand_id, contact_id, channel_type, purpose, effective_at);

-- Product/variant cache with freshness; never quote from a stale row
CREATE TABLE IF NOT EXISTS product_variant_cache (
  brand_id TEXT NOT NULL REFERENCES brand(brand_id),
  source TEXT NOT NULL,
  variant_id TEXT NOT NULL,
  product_id TEXT NOT NULL,
  sku TEXT,
  title TEXT NOT NULL,
  price_minor INTEGER NOT NULL,     -- INR paise
  currency TEXT NOT NULL,
  available INTEGER NOT NULL,
  approved_for_sale INTEGER NOT NULL DEFAULT 0,  -- owner approval flag (planned-only SKUs stay 0)
  fetched_at TEXT NOT NULL,
  PRIMARY KEY (brand_id, source, variant_id)
);

CREATE TABLE IF NOT EXISTS order_ref (
  order_key TEXT PRIMARY KEY,       -- brand_id:source:source_order_id
  brand_id TEXT NOT NULL REFERENCES brand(brand_id),
  source TEXT NOT NULL,
  source_order_id TEXT NOT NULL,
  order_number TEXT,
  contact_id TEXT REFERENCES contact(contact_id),
  currency TEXT NOT NULL,
  total_minor INTEGER NOT NULL,
  paid_minor INTEGER NOT NULL DEFAULT 0,
  financial_state TEXT NOT NULL,    -- unpaid|partially_paid|paid|refunded|partially_refunded|disputed|cod_pending
  fulfilment_state TEXT NOT NULL,   -- unfulfilled|eligible|fulfilled|cancelled
  payment_mode TEXT,                -- prepaid|cod
  cod_decision TEXT,                -- approved_zone_A|approved_zone_B|approved_zone_C|not_approved|manual_review
  cod_decision_reason TEXT,
  cod_policy_version TEXT,
  human_owner TEXT,                 -- agent/team owning the case; non-null pauses bots and promos
  last_source_updated_at TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payment_attempt (
  attempt_key TEXT PRIMARY KEY,     -- provider:payment_id
  order_key TEXT NOT NULL REFERENCES order_ref(order_key),
  provider TEXT NOT NULL,           -- razorpay|cashfree|shopify_gateway
  provider_payment_id TEXT NOT NULL,
  provider_order_or_link_id TEXT,
  amount_minor INTEGER NOT NULL,
  currency TEXT NOT NULL,
  status TEXT NOT NULL,             -- created|failed|captured|refunded|disputed
  verified INTEGER NOT NULL DEFAULT 0,   -- 1 only after signature OR authenticated API verification
  occurred_at TEXT NOT NULL,
  received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shipment_ref (
  shipment_key TEXT PRIMARY KEY,    -- brand_id:shiprocket:sr_order_id or awb
  order_key TEXT NOT NULL REFERENCES order_ref(order_key),
  awb TEXT,
  courier TEXT,
  state TEXT NOT NULL,              -- see shipments.STATE_RANK
  state_at TEXT,
  exception_open INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shipment_event (
  event_key TEXT PRIMARY KEY,       -- shipment_key:status_id:timestamp
  shipment_key TEXT NOT NULL REFERENCES shipment_ref(shipment_key),
  provider_status TEXT NOT NULL,
  provider_status_id TEXT,
  normalized_state TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  received_at TEXT NOT NULL,
  applied INTEGER NOT NULL          -- 0 when ignored as stale/out-of-order
);

CREATE TABLE IF NOT EXISTS lead (
  lead_id TEXT PRIMARY KEY,
  brand_id TEXT NOT NULL REFERENCES brand(brand_id),
  contact_id TEXT NOT NULL REFERENCES contact(contact_id),
  lead_type TEXT NOT NULL,          -- cow_buyer|cow_seller|wholesale|export_waitlist|subscription_interest
  stage TEXT NOT NULL,
  external_ref TEXT,                -- cow registry id / subscription id (reference only)
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS support_case (
  case_id TEXT PRIMARY KEY,
  brand_id TEXT NOT NULL REFERENCES brand(brand_id),
  contact_id TEXT REFERENCES contact(contact_id),
  order_key TEXT REFERENCES order_ref(order_key),
  case_type TEXT NOT NULL,          -- complaint|payment_review|ndr|refund|medical|other
  owner_team TEXT,
  priority TEXT NOT NULL DEFAULT 'normal',
  status TEXT NOT NULL,             -- open|resolved
  due_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- Durable event inbox: unique per (source, source_event_id)
CREATE TABLE IF NOT EXISTS event_inbox (
  event_id TEXT PRIMARY KEY,
  brand_id TEXT NOT NULL,
  source TEXT NOT NULL,
  source_event_id TEXT NOT NULL,
  source_object_id TEXT,
  event_type TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  received_at TEXT NOT NULL,
  schema_version TEXT NOT NULL,
  correlation_id TEXT NOT NULL,
  auth_level TEXT NOT NULL,         -- verified_signature|static_token_hint|unauthenticated
  payload_json TEXT NOT NULL,
  processed_at TEXT,
  UNIQUE (source, source_event_id)
);

-- Outbox / job queue. action_key is the business idempotency key (e.g. order_key:abandoned_reminder_1)
CREATE TABLE IF NOT EXISTS outbox_job (
  job_id TEXT PRIMARY KEY,
  brand_id TEXT NOT NULL,
  action_key TEXT NOT NULL UNIQUE,
  job_type TEXT NOT NULL,           -- send_message|create_payment_link|internal_task|report
  category TEXT NOT NULL,           -- promotional|service|internal
  contact_id TEXT,
  order_key TEXT,
  payload_json TEXT NOT NULL,
  run_after TEXT NOT NULL,
  expires_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 5,
  status TEXT NOT NULL,             -- queued|running|done|cancelled|dead_letter|reconcile_required
  last_error TEXT,
  cancel_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_outbox_due ON outbox_job(status, run_after);

-- Ledger of executed external actions (what provider object resulted)
CREATE TABLE IF NOT EXISTS action_ledger (
  action_key TEXT PRIMARY KEY,
  job_id TEXT REFERENCES outbox_job(job_id),
  provider TEXT NOT NULL,
  provider_object_id TEXT,
  outcome TEXT NOT NULL,            -- sent|dry_run|failed|unknown_timeout
  cost_minor INTEGER NOT NULL DEFAULT 0,
  executed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
  audit_id TEXT PRIMARY KEY,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  subject TEXT,
  detail_json TEXT
);

-- Rate-limit / frequency-cap state per contact and category
CREATE TABLE IF NOT EXISTS touch_log (
  touch_id TEXT PRIMARY KEY,
  brand_id TEXT NOT NULL,
  contact_id TEXT NOT NULL,
  category TEXT NOT NULL,           -- promotional|service
  channel_type TEXT NOT NULL,
  sent_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_touch ON touch_log(brand_id, contact_id, category, sent_at);

CREATE TABLE IF NOT EXISTS budget_ledger (
  entry_id TEXT PRIMARY KEY,
  brand_id TEXT NOT NULL,
  at TEXT NOT NULL,
  kind TEXT NOT NULL,               -- topup|charge_estimate|charge_actual|adjustment
  category TEXT,                    -- promotional|service|voice|ai
  amount_minor INTEGER NOT NULL,    -- positive = credit, negative = spend
  reference TEXT
);

INSERT INTO schema_migrations(version, applied_at) VALUES ('0001_init', CURRENT_TIMESTAMP);
