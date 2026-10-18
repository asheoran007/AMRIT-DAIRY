"""Outbound channel adapters.

Conversational replies go out synchronously from the inbox webhook (conversation.py). This module serves the OUTBOX
(scheduled/service jobs such as the paid-order confirmation): the BiteSpeed support console is Chatwoot, whose API is
documented, so ChatwootSender finds the contact by phone, picks its latest conversation and posts an outgoing message.
GuardedSender enforces SENDER_MODE (dry_run | live_test = TEST_PHONE_ALLOWLIST only | live) in front of it.
BiteSpeed's official Messaging API (doc received 2026-10-16) is served by BitespeedApiSender and preferred when configured.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SendResult:
    outcome: str                # dry_run | sent | failed | unknown_timeout
    provider_object_id: str | None
    cost_minor: int
    detail: str = ""


class Sender(Protocol):
    def send(self, action_key: str, payload: dict) -> SendResult: ...


def _message_text(payload: dict) -> str:
    """Outbox payload 'message' is either plain text or an engine Msg dict ({type, text, buttons?...})."""
    m = payload.get("message")
    if isinstance(m, dict):
        return str(m.get("text") or "")
    return str(m or "")


class DryRunSender:
    """Records what WOULD be sent. Never touches a provider."""

    def __init__(self) -> None:
        self.log: list[tuple[str, dict]] = []

    def send(self, action_key: str, payload: dict) -> SendResult:
        self.log.append((action_key, payload))
        return SendResult("dry_run", f"dryrun:{action_key}", 0, "simulated; nothing delivered")


class ChatwootSender:
    """Outbox -> Chatwoot: payload needs {"phone": "+91...", "message": "..."}; optional "conversation_id" skips the lookup."""

    def __init__(self, base_url: str, account_id: int, api_token: str, timeout: float = 15.0) -> None:
        import httpx
        self._http = httpx
        self.base = base_url.rstrip("/"); self.account_id = account_id; self.token = api_token; self.timeout = timeout

    def _req(self, method: str, path: str, **kw):
        r = self._http.request(method, f"{self.base}/api/v1/accounts/{self.account_id}{path}",
                               headers={"api_access_token": self.token, "Content-Type": "application/json"}, timeout=self.timeout, **kw)
        if r.status_code >= 300:
            raise RuntimeError(f"chatwoot HTTP {r.status_code} on {path}")
        return r.json()

    def conversation_for_phone(self, phone: str) -> int | None:
        digits = "".join(ch for ch in phone if ch.isdigit())
        found = self._req("GET", "/contacts/search", params={"q": digits, "sort": "-last_activity_at"})
        for c in (found.get("payload") or []):
            if "".join(ch for ch in str(c.get("phone_number") or "") if ch.isdigit()).endswith(digits[-10:]):
                convs = self._req("GET", f"/contacts/{c['id']}/conversations").get("payload") or []
                convs = sorted(convs, key=lambda x: x.get("last_activity_at") or 0, reverse=True)
                return convs[0]["id"] if convs else None
        return None

    def send(self, action_key: str, payload: dict) -> SendResult:
        try:
            conv = payload.get("conversation_id") or self.conversation_for_phone(str(payload.get("phone") or ""))
            if not conv:
                return SendResult("failed", None, 0, "no console conversation for this phone")
            res = self._req("POST", f"/conversations/{conv}/messages",
                            json={"content": _message_text(payload), "message_type": "outgoing", "private": False})
            return SendResult("sent", f"chatwoot:{res.get('id')}", 0, f"conversation {conv}")
        except Exception as exc:  # network ambiguity -> reconcile path, never blind retry
            kind = type(exc).__name__
            if "Timeout" in kind:
                return SendResult("unknown_timeout", None, 0, f"{kind}: provider may have accepted the request")
            return SendResult("failed", None, 0, f"{kind}: {exc}"[:200])


class BitespeedApiSender:
    """Outbox -> official BiteSpeed Messaging API: payload {"phone": "+91...", "message": "..."} as a free-text message
    (24 h customer-service window) or {"template_id": ..., "variables": {...}} as a template message."""

    def __init__(self, client) -> None:
        self.client = client

    def send(self, action_key: str, payload: dict) -> SendResult:
        try:
            phone = str(payload.get("phone") or "")
            if payload.get("template_id"):
                res = self.client.send_template(phone, payload["template_id"], dict(payload.get("variables") or {}), third_party_id=action_key)
            else:
                res = self.client.send_text(phone, _message_text(payload), third_party_id=action_key)
            if res.ok:
                return SendResult("sent", f"bitespeed:{res.third_party_id or action_key}", 0, res.message)
            return SendResult("failed", None, 0, f"HTTP {res.http}: {res.message}")
        except Exception as exc:
            kind = type(exc).__name__
            if "Timeout" in kind:
                return SendResult("unknown_timeout", None, 0, f"{kind}: provider may have accepted the request")
            return SendResult("failed", None, 0, f"{kind}: {exc}"[:200])


class GuardedSender:
    """SENDER_MODE in front of a live sender: dry_run -> never; live_test -> allowlisted phones only; live -> all."""

    def __init__(self, inner: Sender, mode: str, allowlist: tuple[str, ...]) -> None:
        self.inner, self.mode = inner, mode
        self.allow = {"".join(ch for ch in a if ch.isdigit())[-10:] for a in allowlist}
        self.dry = DryRunSender()

    def send(self, action_key: str, payload: dict) -> SendResult:
        if self.mode == "live":
            return self.inner.send(action_key, payload)
        phone = "".join(ch for ch in str(payload.get("phone") or "") if ch.isdigit())[-10:]
        if self.mode == "live_test" and phone and phone in self.allow:
            return self.inner.send(action_key, payload)
        r = self.dry.send(action_key, payload)
        return SendResult(r.outcome, r.provider_object_id, 0, f"{self.mode}: not delivered")


class LiveBiteSpeedSender:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def send(self, action_key: str, payload: dict) -> SendResult:  # pragma: no cover - intentionally blocked
        raise NotImplementedError(
            "BiteSpeed send API contract is UNVERIFIED (no public docs as of 2026-10-11). "
            "Obtain the written contract from BiteSpeed and implement with the idempotency key = action_key."
        )


class FlakySender:
    """Test double that times out ambiguously on the first call (for T19 reconciliation tests)."""

    def __init__(self, inner: Sender) -> None:
        self.inner = inner
        self.calls = 0

    def send(self, action_key: str, payload: dict) -> SendResult:
        self.calls += 1
        if self.calls == 1:
            return SendResult("unknown_timeout", None, 0, "ambiguous timeout: provider may have accepted the request")
        return self.inner.send(action_key, payload)
