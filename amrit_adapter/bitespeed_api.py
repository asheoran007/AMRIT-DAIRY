"""Official BiteSpeed WhatsApp Messaging API (shop-scoped key; documentation received from BiteSpeed 16 Oct 2026).

Contract (from the doc, verbatim fields):
  POST {BITESPEED_API_BASE}            default https://2lkkwxk9b6.execute-api.us-east-1.amazonaws.com/whatsapp/messages/send/template
  headers  Content-Type: application/json, X-API-Key: <store key>
  template: {channelId, template_id, recipient (digits, no '+'), variables{...}, sourceContext{thirdPartyMessageId}}
  free-text: {channelId, recipient, messageType: text|image|video|document|interactive, text{body, context{message_id}} | media{...} | interactive{...},
              variables{...}, sourceContext{thirdPartyName, thirdPartyMessageId, ...}}
  response: {"message": "Message queued successfully", "thirdPartyMessageId": ...}
channelId = the store's WhatsApp number as digits (doc: "in most cases same as phone number"); BiteSpeed provides it, no API to fetch it.
There is NO read-only / probe endpoint, so authentication can only be verified by a real send (first one: controlled live_test
to TEST_PHONE_ALLOWLIST). The key is never logged; the URL carries no secret. `status` is what /health shows.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger("amrit.bitespeed_api")
DEFAULT_BASE = "https://2lkkwxk9b6.execute-api.us-east-1.amazonaws.com/whatsapp/messages/send/template"
THIRD_PARTY_NAME = "amrit-adapter"

status: dict[str, Any] = {"last_send_at": None, "last_outcome": None, "last_http": None, "sent": 0, "failed": 0}


@dataclass(frozen=True)
class ApiResult:
    ok: bool
    http: int | None
    message: str
    third_party_id: str | None = None


def digits(phone: str) -> str:
    return "".join(ch for ch in (phone or "") if ch.isdigit())


class BitespeedMessagingClient:
    def __init__(self, api_key: str, channel_id: str, base_url: str = DEFAULT_BASE, timeout: float = 15.0) -> None:
        if not api_key or not channel_id:
            raise ValueError("BITESPEED_API_KEY and BITESPEED_CHANNEL_ID are both required")
        self._key = api_key
        self.channel_id = digits(channel_id)
        self.base = (base_url or DEFAULT_BASE).rstrip("/")
        self.timeout = timeout

    def __repr__(self) -> str:  # never leak the key through repr/logging
        return f"BitespeedMessagingClient(channel={self.channel_id}, base={self.base})"

    def _post(self, body: dict[str, Any]) -> ApiResult:
        status["last_send_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            r = httpx.post(self.base, headers={"Content-Type": "application/json", "X-API-Key": self._key}, json=body, timeout=self.timeout)
        except Exception as exc:
            status.update(last_outcome=f"error_{type(exc).__name__}", last_http=None); status["failed"] += 1
            raise
        try:
            data = r.json() if r.content else {}
        except ValueError:
            data = {"raw": r.text[:200]}
        ok = 200 <= r.status_code < 300
        msg = str((data or {}).get("message") or (data or {}).get("error") or data)[:200]
        status.update(last_outcome="queued" if ok else "rejected", last_http=r.status_code)
        status["sent" if ok else "failed"] += 1
        log.info("bitespeed api %s http=%s type=%s to=****%s msg=%s", "ok" if ok else "FAIL", r.status_code,
                 body.get("messageType") or "template", body.get("recipient", "")[-4:], msg)
        return ApiResult(ok, r.status_code, msg, (data or {}).get("thirdPartyMessageId"))

    def _ctx(self, third_party_id: str | None) -> dict[str, Any]:
        ctx: dict[str, Any] = {"thirdPartyName": THIRD_PARTY_NAME}
        if third_party_id:
            ctx["thirdPartyMessageId"] = third_party_id
        return ctx

    def send_text(self, phone: str, body: str, third_party_id: str | None = None, reply_to_wamid: str | None = None) -> ApiResult:
        text: dict[str, Any] = {"body": body}
        if reply_to_wamid:
            text["context"] = {"message_id": reply_to_wamid}
        return self._post({"channelId": self.channel_id, "recipient": digits(phone), "messageType": "text", "text": text,
                           "sourceContext": self._ctx(third_party_id)})

    def send_buttons(self, phone: str, body: str, buttons: list[tuple[str, str]], third_party_id: str | None = None,
                     footer: str | None = None) -> ApiResult:
        """WhatsApp reply buttons (max 3, title <= 20 chars per WhatsApp rules)."""
        btns = [{"type": "reply", "reply": {"id": bid, "title": title[:20]}} for bid, title in buttons[:3]]
        inter: dict[str, Any] = {"type": "button", "body": {"text": body[:1024]}, "action": {"buttons": btns}}
        if footer:
            inter["footer"] = {"text": footer[:60]}
        return self._post({"channelId": self.channel_id, "recipient": digits(phone), "messageType": "interactive",
                           "interactive": inter, "sourceContext": self._ctx(third_party_id)})

    def send_media(self, phone: str, kind: str, link: str, caption: str = "", third_party_id: str | None = None) -> ApiResult:
        return self._post({"channelId": self.channel_id, "recipient": digits(phone), "messageType": kind,
                           "media": {"link": link, **({"caption": caption[:1024]} if caption else {})}, "sourceContext": self._ctx(third_party_id)})

    def send_list(self, phone: str, body: str, rows: list[tuple[str, str, str]], button: str = "विकल्प चुनें",
                  footer: str | None = None, third_party_id: str | None = None) -> ApiResult:
        """WhatsApp list message (≤10 rows; row title ≤24, description ≤72, button ≤20, body ≤1024)."""
        inter: dict[str, Any] = {"type": "list", "body": {"text": body[:1024]},
                                 "action": {"button": button[:20], "sections": [{"title": "विकल्प"[:24], "rows": [
                                     {"id": rid[:200], "title": t[:24], **({"description": d[:72]} if d else {})} for rid, t, d in rows[:10]]}]}}
        if footer:
            inter["footer"] = {"text": footer[:60]}
        return self._post({"channelId": self.channel_id, "recipient": digits(phone), "messageType": "interactive",
                           "interactive": inter, "sourceContext": self._ctx(third_party_id)})

    def send_template(self, phone: str, template_id: str, variables: dict[str, str], third_party_id: str | None = None) -> ApiResult:
        return self._post({"channelId": self.channel_id, "recipient": digits(phone), "template_id": template_id,
                           "variables": variables, "sourceContext": self._ctx(third_party_id)})


class BitespeedReplyChannel:
    """conversation.py reply channel over the Messaging API. Replies go to the customer's phone (not a console
    conversation), so handle_inbound binds the phone first (for_phone). Private notes cannot be posted through this API
    and are logged only."""

    def __init__(self, client: BitespeedMessagingClient, phone: str = "") -> None:
        self.client, self.phone = client, phone

    def for_phone(self, phone: str) -> "BitespeedReplyChannel":
        return BitespeedReplyChannel(self.client, phone)

    def send_messages(self, conversation_id: int | str, messages: list) -> dict:
        """Engine messages as native WhatsApp messages: buttons (≤3) -> reply buttons, longer option sets -> list,
        text -> text. If an interactive send is rejected, that message falls back to numbered plain text so the
        customer is never left without an answer."""
        from .engine.state import FOOTER, render_plain
        sent = 0
        for i, m in enumerate(messages):
            tid = f"conv{conversation_id}-{int(time.time())}-{i}"
            res = None
            try:
                if m.type in ("video", "image") and m.media_url:
                    res = self.client.send_media(self.phone, m.type, m.media_url, m.text, third_party_id=tid)
                elif m.buttons and len(m.buttons) <= 3 and len(m.text) <= 1024:
                    res = self.client.send_buttons(self.phone, m.text, m.buttons, third_party_id=tid, footer=FOOTER)
                elif (m.rows or m.buttons) and len(m.text) <= 1024:
                    rows = list(m.rows) or [(bid, title, "") for bid, title in m.buttons]
                    res = self.client.send_list(self.phone, m.text, rows, footer=FOOTER, third_party_id=tid)
            except Exception as exc:
                log.info("interactive send error %s -> plain text fallback", type(exc).__name__)
                res = None
            if res is None or not res.ok:
                if res is not None:
                    log.info("interactive rejected http=%s -> plain text fallback", res.http)
                plain = self.client.send_text(self.phone, render_plain([m]), third_party_id=tid)
                if not plain.ok:
                    raise RuntimeError(f"bitespeed api HTTP {plain.http}: {plain.message}")
            sent += 1
        return {"queued": sent}

    def send_text(self, conversation_id: int | str, content: str, private: bool = False) -> dict:
        if private:
            log.info("private note not deliverable via messaging api (conv=%s): %s", conversation_id, content[:80])
            return {"skipped": "private"}
        res = self.client.send_text(self.phone, content, third_party_id=f"conv{conversation_id}-{int(time.time())}")
        if not res.ok:
            raise RuntimeError(f"bitespeed api HTTP {res.http}: {res.message}")
        return {"queued": True, "thirdPartyMessageId": res.third_party_id}
