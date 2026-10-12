"""Provider webhook verification + normalization into InternalEvent.

Documented schemes (fetched 2026-10-11, see SECURITY-REVIEW.md for URLs):
- Shopify: header X-Shopify-Hmac-SHA256 = base64(HMAC-SHA256(app client secret, raw body)).
           X-Shopify-Webhook-Id is unique per delivery; X-Shopify-Event-Id shared across retries.
- Razorpay: header X-Razorpay-Signature = hex(HMAC-SHA256(webhook secret, raw body)); x-razorpay-event-id unique.
- Shiprocket: optional static token in header x-api-key; NO signature. Treated as static_token_hint;
              sensitive state must be re-verified via authenticated Shiprocket API before customer-facing use.
"""
