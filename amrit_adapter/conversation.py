"""Inbound WhatsApp message -> engine turn -> reply through the BiteSpeed support console (Chatwoot API).

Why this path and not the flow builder: BiteSpeed's flow-builder "API Call" node is fire-and-forget (URL/method/body only,
no response mapping - checked in the builder UI on 13 Oct 2026), so a flow cannot render what the engine answers.
The support console (chat.bitespeed.co) is Chatwoot, whose API is documented:
  POST {base}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages   header: api_access_token
  body: {"content": "...", "message_type": "outgoing", "private": false}
The console's webhook (Settings -> Integrations -> Webhooks, event message_created) delivers every inbound message to
/webhooks/inbox/<token>; this module answers it synchronously (chat latency), with these guards, in order:
  1. only message_created / incoming / non-private / with a sender phone;
  2. a conversation a human agent has taken (assignee set) is left alone;
  3. GLOBAL_KILL_SWITCH=true  -> engine runs, nothing is sent (recorded as dry_run);
  4. SENDER_MODE: dry_run -> never send; live_test -> send only to TEST_PHONE_ALLOWLIST; live -> send to everyone.
Everything is written to audit_log (action bot.reply) with the outcome, so dry runs are inspectable before go-live.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy.engine import Connection

from .db import audit
from .engine.state import render_plain

log = logging.getLogger("amrit.conversation")


@dataclass(frozen=True)
class ReplyOutcome:
    status: str          # skipped | dry_run | sent | failed
    reason: str
    reply_text: str = ""
    state: str = ""
    handoff: bool = False


class ChatwootClient:
    def __init__(self, base_url: str, account_id: int, api_token: str, timeout: float = 15.0) -> None:
        self.base = base_url.rstrip("/")
        self.account_id = account_id
        self.token = api_token
        self.timeout = timeout

    def send_text(self, conversation_id: int | str, content: str, private: bool = False) -> dict:
        r = httpx.post(f"{self.base}/api/v1/accounts/{self.account_id}/conversations/{conversation_id}/messages",
                       headers={"api_access_token": self.token, "Content-Type": "application/json"},
                       json={"content": content, "message_type": "outgoing", "private": private}, timeout=self.timeout)
        if r.status_code >= 300:
            raise RuntimeError(f"chatwoot HTTP {r.status_code}")
        return r.json()


def _phone(body: dict[str, Any]) -> str:
    sender = body.get("sender") or {}
    meta_sender = ((body.get("conversation") or {}).get("meta") or {}).get("sender") or {}
    p = str(sender.get("phone_number") or meta_sender.get("phone_number") or sender.get("identifier") or "").strip()
    if p and not p.startswith("+"):
        p = "+" + p.lstrip("0")
    return p


def _norm_phone(p: str) -> str:
    return "".join(ch for ch in p if ch.isdigit())


def handle_inbound(conn: Connection, body: dict[str, Any], build_engine, chat: ChatwootClient | None, sender_mode: str,
                   allowlist: tuple[str, ...], kill_switch: bool, yield_keywords: tuple[str, ...] = ()) -> ReplyOutcome:
    """Run one engine turn for an inbound console message and deliver the answer under the guards above.
    yield_keywords: while BiteSpeed keyword flows (AD-01 ghee / AD-02 honey / AD-08 milk) are still active, messages that
    contain one of their trigger words are left to that flow so the customer never gets two answers (FLOW_KEYWORDS_YIELD).
    Allowlisted test numbers are exempt so the engine journey can be tested end to end before the flows are retired."""
    if body.get("event") != "message_created":
        return ReplyOutcome("skipped", f"event {body.get('event')}")
    mtype = body.get("message_type")
    log.info("inbox message_created conv=%s type=%r private=%r content_len=%d sender_last4=%s id=%s assignee=%r",
             (body.get("conversation") or {}).get("id"), mtype, body.get("private"), len(str(body.get("content") or "")),
             _phone(body)[-4:], "y" if body.get("id") else "n",
             (body.get("conversation") or {}).get("assignee_id") or (((body.get("conversation") or {}).get("meta") or {}).get("assignee") or {}).get("id"))
    if mtype not in ("incoming", 0, "0"):
        return ReplyOutcome("skipped", f"not an incoming message (type={mtype!r})")
    if body.get("private"):
        return ReplyOutcome("skipped", "private note")
    conv = body.get("conversation") or {}
    conv_id = conv.get("id") or body.get("conversation_id")
    phone = _phone(body)
    if not conv_id or not phone:
        return ReplyOutcome("skipped", "no conversation id / sender phone")
    assignee = conv.get("assignee_id") or ((conv.get("meta") or {}).get("assignee") or {}).get("id")
    if assignee:
        return ReplyOutcome("skipped", "conversation owned by a human agent")
    text_in = str(body.get("content") or "").strip()
    name = (body.get("sender") or {}).get("name")
    low = text_in.lower()
    hit = next((k for k in yield_keywords if k and k.lower() in low), None)
    if hit and _norm_phone(phone) not in {_norm_phone(a) for a in allowlist}:
        audit(conn, "bot", "bot.yield", str(conv_id), {"phone_last4": phone[-4:], "keyword": hit})
        return ReplyOutcome("skipped", f"left to BiteSpeed keyword flow ('{hit}')")

    eng = build_engine()
    r = eng.turn(conn, phone, text_in, None, name)
    reply = render_plain(r.messages)
    note = f"🤖 handoff → {r.handoff_team or 'team'} (engine state {r.state})" if r.handoff else ""

    # delivery decision
    if kill_switch:
        outcome, why = "dry_run", "global kill switch engaged"
    elif sender_mode == "live":
        outcome, why = "send", "live"
    elif sender_mode == "live_test":
        if _norm_phone(phone) in {_norm_phone(a) for a in allowlist}:
            outcome, why = "send", "live_test: phone on allowlist"
        else:
            outcome, why = "dry_run", "live_test: phone not on allowlist"
    else:
        outcome, why = "dry_run", f"sender_mode={sender_mode}"
    if outcome == "send" and chat is None:
        outcome, why = "dry_run", "no reply channel configured (BITESPEED_API_KEY+CHANNEL_ID or CHATWOOT_API_TOKEN)"
    if chat is not None and hasattr(chat, "for_phone"):
        chat = chat.for_phone(phone)       # Messaging API channel: replies address the customer's number, not a console conversation

    status = outcome
    if outcome == "send":
        try:
            if reply:
                chat.send_text(conv_id, reply)
            if note:
                chat.send_text(conv_id, note, private=True)
            status = "sent"
        except Exception as exc:
            status, why = "failed", f"{type(exc).__name__}: {exc}"[:200]
    from .engine.v2_map import stage_for
    audit(conn, "bot", "bot.reply", str(conv_id), {"phone_last4": phone[-4:], "state": r.state, "v2": stage_for(r), "status": status,
                                                   "reason": why, "chars": len(reply), "handoff": r.handoff, "actions": r.actions})
    log.info("bot.reply conv=%s state=%s status=%s reason=%s chars=%d handoff=%s", conv_id, r.state, status, why, len(reply), r.handoff)
    return ReplyOutcome(status, why, reply, r.state, r.handoff)
