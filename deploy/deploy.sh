#!/usr/bin/env bash
# One-shot install on a fresh Ubuntu 22.04/24.04 VPS (1 vCPU / 1 GB is enough). Run as root:  bash deploy/deploy.sh
# Prereqs you do by hand first: (1) DNS A record adapter.<domain> -> this VPS, (2) ../.env filled from .env.example.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "adapter/.env missing — copy .env.example and fill it"; exit 1; }
[ -f deploy/.env.caddy ] || { echo "deploy/.env.caddy missing — copy deploy/.env.caddy.example"; exit 1; }
grep -q '^ADAPTER_HOST=.\+' deploy/.env.caddy || { echo "ADAPTER_HOST not set in deploy/.env.caddy"; exit 1; }
grep -Eq '^SHOPIFY_CLIENT_ID=.+|^SHOPIFY_ADMIN_TOKEN=.+' .env || { echo "Shopify credentials missing in .env (SHOPIFY_CLIENT_ID/SECRET or SHOPIFY_ADMIN_TOKEN)"; exit 1; }
grep -q '^INBOX_WEBHOOK_TOKEN=.\{20,\}' .env || { echo "INBOX_WEBHOOK_TOKEN missing/short in .env"; exit 1; }
chmod 600 .env deploy/.env.caddy
if ! command -v docker >/dev/null; then curl -fsSL https://get.docker.com | sh; fi
docker compose -f deploy/docker-compose.yml up -d --build
sleep 5
docker compose -f deploy/docker-compose.yml ps
echo "Now run: bash deploy/verify.sh https://$(grep ADAPTER_HOST deploy/.env.caddy | cut -d= -f2)"
