# Legacy BiteSpeed automations — snapshot 13 Oct 2026 (read-only audit; nothing changed)

Recovery = this file + the console UI (journeys/flows are not exportable). Re-check before any change.

## Marketing journeys (marketing.bitespeed.co → Journeys, all-time metrics)
| Journey | Live since | Recipients | Revenue | Notes / risk |
|---|---|---|---|---|
| Checkout Aban | 09 Sep 2026 15:03 | 451 | ₹44,450 | Trigger CHECKOUT_STARTED, exit ORDER_PLACED → wait 2 h → WA template `checkout1` → wait 12 h → WA `checkout2` (discount {{2}} = **AMRIT50**, {{3}}% off) → **AI Voice "Abandoned Cart"**, 2 retries every 4 h, + WA `copy_of_awaaz` follow-up. Builder shows **"Unpublished Changes"** (13 Oct 16:45 IST) — do NOT press "Save And Publish" blindly; pausing from the Journeys list keeps the published config and is reversible. **Unapproved voice calls + discount → pause (owner click B1)** |
| placed | 05 Sep 2026 19:22 | 148 | ₹0 | order-placed confirmation (Shopify ORDER_PLACED). Keep; adapter confirmation stays dry_run/duplicate-aware |
| shipped | 05 Sep 2026 19:23 | 121 | ₹0 | fires on FULFILLMENT_CREATED = label creation ("has been shipped" too early). Keep until engine tracking replaces it (B3) |
| delivered | 05 Sep 2026 19:24 | 106 | ₹0 | keep |
| Cancel | 05 Sep 2026 19:24 | 23 | ₹0 | keep |
| Cart Aban | 05 Sep 2026 19:26 | 0 | ₹0 | discount/scarcity copy, 0 recipients — pause candidate (B2) |
| Browser Aban | 05 Sep 2026 19:28 | 0 | ₹0 | pause candidate (B2) |
| Winback 60d | 05 Sep 2026 19:30 | 0 | ₹0 | pause candidate (B2) |
| Untitled Journey 9 | 11 Sep→live 11 Oct 2026 22:18 | 0 | ₹0 | **no trigger configured** ("Setup Trigger", 1 error) → cannot fire; delete to keep the list clean |

## Chatbot flows (chatbot.bitespeed.co → Flows)
Settings & Automation: **Welcome Message = AD-00 | MASTER WELCOME + ROUTER**; Fallback = none; OOO = none; Add-Product = none; working hours not set.

| Flow | Trigger (If message contains) | Behaviour | Conflict with engine |
|---|---|---|---|
| AD-00 MASTER WELCOME + ROUTER | Welcome (new conversation, no keyword) | menu router | double reply on first contact → set Welcome to none at cutover |
| AD-01 BILONA GHEE QUICK BUY | ghee, घी, ghi, bilona, बिलोना, bilona ghee, बिलोना घी, desi ghee, cow ghee, Amrit Golden Ghee, Golden Ghee, Ghee | packs 1/5/10 L hard-coded ₹3,400/₹3,300 unit → Google-Sheet order via API node, UTR/screenshot = "paid" (tags payment_submitted / payment_verification_pending), razorpay.me link | duplicate reply; unverified "paid"; no Shopify order → remove keyword trigger at cutover (engine owns) |
| AD-02 HONEY QUICK BUY | 🍯 Honey, शहद, हनी, honey, shehad, natural/organic/raw honey, honey price, शहद कीमत, buy honey, Natural Honey | tags order_intent, address collect | duplicate reply → remove trigger at cutover |
| AD-08 DESI COW MILK | milk, दूध, dudh, cow milk, desi cow milk, a2 milk, milk price, दूध कीमत, Desi Cow Milk | quotes ₹100/₹120 milk (product is ₹0/unavailable in Shopify) | quotes a coming-soon product → pause |
| AD-03 COW BUY-SELL, AD-04 QUICK ORDER, AD-05 ORDER TRACKING, AD-06 TALK TO TEAM, AD-07 JOB/VACANCY, Address, COD confirmation, COD to Prepaid, Basic bot flow | keyword/manual | intake / routing | keep (no overlap with the ghee/honey purchase path); AD-05 overlaps `track` intent only when its keyword is typed |
| AD-09…AD-28 | — | do not exist | — |

## Engine-side guard while legacy stays live
`FLOW_KEYWORDS_YIELD` (Render env) = the AD-01/02/08 trigger words above → engine stays silent on them for non-test numbers; cleared at cutover.

## 13 Oct 2026 16:50 IST — webhook recovery result
Console webhooks: exactly **one** entry — `https://adapter.amritdairy.in/webhooks/inbox/<current token>` with events conversation_created, conversation_status_changed, conversation_updated, message_created. The stale `amrit-adapter.onrender.com` entry (old token, every delivery 401) was deleted after the new entry had been proven with `200 OK` deliveries in the Render log. The value the owner pasted was the Render-generated INBOX_WEBHOOK_TOKEN (44-char base64 shape; a Shopify client secret is hex) → no Shopify credential exposure; no further rotation needed. Access logs now redact token-bearing paths.
