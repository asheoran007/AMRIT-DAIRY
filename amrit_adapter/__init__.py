"""Amrit Dairy <-> providers <-> BiteSpeed adapter.

Design rules (from the implementation kit):
- Durable event inbox with unique (source, source_event_id); ack fast, process async.
- Verify provider webhooks per their documented scheme; a static token is a hint, not proof.
- Business-action idempotency keys in the outbox; duplicates never create a second action.
- Monotonic state machines for payments and shipments (late old events never downgrade).
- Pre-send recheck: consent, order state, human ownership, expiry, frequency caps, budget, kill switch.
- Sender is DRY-RUN until a BiteSpeed API contract is verified in writing.
"""
__version__ = "0.1.0"
