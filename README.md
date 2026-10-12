# amrit-adapter

WhatsApp (BiteSpeed) → Shopify order engine for Amrit Dairy. FastAPI API + background worker, SQLite on a persistent disk.

- Deploy: `render.yaml` (Render Blueprint, repo root = this folder) or `deploy/` (Docker Compose + Caddy, systemd).
- Secrets: only in the host's environment (`.env.example` lists them). Nothing secret is committed.
- Tests: `python -m pytest -q` (64 tests, no network).
- Docs: see the handoff bundle (DEPLOYMENT.md, RUNBOOK.md, SECURITY-REVIEW.md).
