-- Numbered-reply fallback: the engine remembers the ids of the options it last presented so a plain "2" maps to option 2
-- when the channel renders buttons/lists as numbered text (BiteSpeed API Call + Text node path).
ALTER TABLE conversation_session ADD COLUMN last_options_json TEXT;
INSERT INTO schema_migrations(version, applied_at) VALUES ('0003_options', CURRENT_TIMESTAMP);
