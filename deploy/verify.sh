#!/usr/bin/env bash
# Post-deploy checks (no secrets needed). Usage: bash deploy/verify.sh https://adapter.amritdairy.in
set -u
H="${1:?base url}"
echo "1) health:";            curl -sS "$H/health"; echo
echo "2) unsigned shopify  (expect 401):"; curl -sS -o /dev/null -w "%{http_code}\n" -X POST "$H/webhooks/shopify" -d '{}'
echo "3) unsigned razorpay (expect 401):"; curl -sS -o /dev/null -w "%{http_code}\n" -X POST "$H/webhooks/razorpay" -d '{}'
echo "4) bad bot token     (expect 401):"; curl -sS -o /dev/null -w "%{http_code}\n" -X POST "$H/bot/turn/wrong" -d '{"phone":"+910000000000"}'
echo "5) unknown path      (expect 404):"; curl -sS -o /dev/null -w "%{http_code}\n" "$H/admin"
echo "6) TLS:";               curl -sSI "$H/health" | head -1
