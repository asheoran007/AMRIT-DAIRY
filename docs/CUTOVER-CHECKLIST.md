# Controlled cutover — legacy → Automation V2 (owner-approved, step by step; rollback = GLOBAL_KILL_SWITCH=true)

Pre-conditions (all DONE 13 Oct): adapter LIVE on adapter.amritdairy.in, Shopify app installed (offline token), 7 Shopify
webhooks, inbox token rotated, backups on disk, 85/85 tests, V2 catalogue config/automation_v2.json (inactive).

1. Inbound: console webhook → `https://adapter.amritdairy.in/webhooks/inbox/<token>` with events conversation_created,
   conversation_status_changed, conversation_updated, message_created; a "hi" from +918814869007 shows `POST /webhooks/inbox/... 200`
   and an audit `bot.reply … dry_run` (Render log). Exactly one webhook row.
2. Outbound auth: official BiteSpeed token → Render `CHATWOOT_API_TOKEN` (owner) → `/health.console_api: true`.
3. Test only +918814869007: `SENDER_MODE=live_test`, `GLOBAL_KILL_SWITCH=false` (test window) → run DEPLOYMENT §6
   (ghee → packs → masked saved address → quote → link → TESTPAY ₹10 → gateway-verified order → hold → track → honey).
4. One flow responds: during the test AD-01 may also answer "ghee" for the test number (documented); customers are
   protected by FLOW_KEYWORDS_YIELD. Verify in the console that non-test numbers received exactly one answer.
5. Payment/order/shipment protection: order gateway = Razorpay via Shopify checkout, amount/variant/qty match, test order on
   fulfillment hold (v5 scopes) or held via admin, cancelled orders never shipped (ALERT task).
6. Disable overlapping legacy triggers (owner clicks): remove keyword nodes in AD-01/AD-02, pause AD-08, Welcome Message → none,
   pause Checkout Aban (or remove voice + AMRIT50), delete Untitled Journey 9; then clear `FLOW_KEYWORDS_YIELD`.
7. Activate: `GLOBAL_KILL_SWITCH=false`, `SENDER_MODE=live` (owner's go-live approval) — engine = AD-V2-00…15.
8. Monitor: /health (needs_attention, backup), Render failure e-mails, audit_log bot.reply / turn.error; rollback in 30 s with
   `GLOBAL_KILL_SWITCH=true`; never broadcasts or voice calls from the adapter.
