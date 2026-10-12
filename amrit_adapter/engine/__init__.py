"""Reusable all-product WhatsApp sales engine (brief §2–§10).

Pipeline per inbound message:
  intent.detect -> catalog lookup -> session state machine (state.py) -> leads/tags -> quote -> draft order
  -> (Shopify checkout pays via Razorpay gateway) -> verified `orders/paid` webhook -> order confirmation.
Everything the customer sees is produced as message specs (text / buttons / list / video) so the same engine can be
rendered by a BiteSpeed flow (API Call node) or any other channel adapter.
"""
