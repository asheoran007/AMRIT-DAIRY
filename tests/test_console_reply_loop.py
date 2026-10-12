"""Inbound console (Chatwoot) webhook -> engine turn -> guarded delivery through the console API.
Covers: event filtering, human-owned conversations, kill switch, SENDER_MODE dry_run / live_test (allowlist) / live,
failure handling, dedupe on webhook redelivery, and the outbox ChatwootSender + GuardedSender."""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from amrit_adapter import conversation as cv
from amrit_adapter.engine.state import Engine
from amrit_adapter.sender import ChatwootSender, GuardedSender
from tests.conftest import BRAND
from tests.test_engine_sequence import PHONE, saved_customer, verified_catalog
from amrit_adapter.engine.shopify_client import FakeShopifyClient


class FakeChat:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send_text(self, conversation_id, content, private=False):
        if self.fail:
            raise RuntimeError("chatwoot HTTP 500")
        self.sent.append((conversation_id, content, private))
        return {"id": len(self.sent)}


def inbound(content="ghee", phone=PHONE, event="message_created", mtype="incoming", private=False, assignee=None, mid=101):
    return {"event": event, "id": mid, "content": content, "message_type": mtype, "private": private, "created_at": 1760300000 + mid,
            "sender": {"id": 5, "name": "Amit", "phone_number": phone},
            "conversation": {"id": 777, "status": "open", "assignee_id": assignee, "meta": {"sender": {"phone_number": phone}}},
            "account": {"id": 3122}}


@pytest.fixture()
def build(engine, tmp_path):
    def _b():
        return Engine(verified_catalog(tmp_path), FakeShopifyClient({PHONE: saved_customer()}), BRAND)
    return _b


def test_filters_then_replies_only_to_incoming_customer_text(engine, build):
    chat = FakeChat()
    with engine.begin() as c:
        assert cv.handle_inbound(c, inbound(event="conversation_created"), build, chat, "live", (), False).status == "skipped"
        assert cv.handle_inbound(c, inbound(mtype="outgoing"), build, chat, "live", (), False).status == "skipped"
        assert cv.handle_inbound(c, inbound(private=True), build, chat, "live", (), False).status == "skipped"
        r = cv.handle_inbound(c, inbound(assignee=42), build, chat, "live", (), False)
        assert r.status == "skipped" and "human" in r.reason
        r = cv.handle_inbound(c, inbound("ghee"), build, chat, "live", (), False)
    assert r.status == "sent" and chat.sent and chat.sent[0][0] == 777 and "Ghee" in chat.sent[0][1] or "घी" in chat.sent[0][1]
    assert r.state and "1" in r.reply_text   # numbered pack options rendered as plain text


def test_kill_switch_and_modes_decide_delivery(engine, build):
    chat = FakeChat()
    with engine.begin() as c:
        r = cv.handle_inbound(c, inbound(mid=1), build, chat, "live", (), True)
        assert r.status == "dry_run" and "kill switch" in r.reason and not chat.sent
        r = cv.handle_inbound(c, inbound(mid=2), build, chat, "dry_run", (), False)
        assert r.status == "dry_run" and not chat.sent
        r = cv.handle_inbound(c, inbound(mid=3, phone="+919999999999"), build, chat, "live_test", (PHONE,), False)
        assert r.status == "dry_run" and "not on allowlist" in r.reason and not chat.sent
        r = cv.handle_inbound(c, inbound(mid=4), build, chat, "live_test", (PHONE,), False)
        assert r.status == "sent" and len(chat.sent) == 1
        r = cv.handle_inbound(c, inbound(mid=5), build, None, "live", (), False)
        assert r.status == "dry_run" and "CHATWOOT_API_TOKEN" in r.reason
        r = cv.handle_inbound(c, inbound(mid=6), build, FakeChat(fail=True), "live", (), False)
        assert r.status == "failed"
        rows = c.execute(text("SELECT detail_json FROM audit_log WHERE action='bot.reply'")).fetchall()
    assert len(rows) == 6 and all(json.loads(x[0])["phone_last4"] == "9007" for x in rows[:2])   # audited, no full phone stored


def test_webhook_endpoint_runs_loop_and_dedupes_redelivery(monkeypatch):
    import amrit_adapter.api as api
    from amrit_adapter.config import settings
    monkeypatch.setattr(settings, "inbox_webhook_token", "t0k"); monkeypatch.setattr(settings, "sender_mode", "live_test")
    monkeypatch.setattr(settings, "global_kill_switch", False); monkeypatch.setattr(settings, "test_phone_allowlist", PHONE)
    monkeypatch.setattr(settings, "chatwoot_api_token", "cw-secret")
    sent = []
    monkeypatch.setattr(cv.ChatwootClient, "send_text", lambda self, cid, content, private=False: sent.append((cid, content, private)) or {"id": 1})
    c = TestClient(api.app)
    assert c.post("/webhooks/inbox/wrong", json=inbound()).status_code == 401
    r = c.post("/webhooks/inbox/t0k", json=inbound(mid=900))
    assert r.status_code == 200 and r.json()["status"] == "sent" and sent and sent[0][0] == 777
    assert c.post("/webhooks/inbox/t0k", json=inbound(mid=900)).text == "duplicate" and len(sent) == 1   # redelivery is a no-op
    assert c.get("/health").json()["console_api"] is True


def test_outbox_chatwoot_sender_and_guard(monkeypatch):
    calls = []

    class R:
        def __init__(self, status_code, body): self.status_code, self._b = status_code, body
        def json(self): return self._b

    def fake_request(method, url, headers=None, timeout=None, params=None, json=None):
        calls.append((method, url.split("/accounts/3122")[1], params, json))
        assert headers["api_access_token"] == "cw-secret"
        if url.endswith("/contacts/search"):
            return R(200, {"payload": [{"id": 9, "phone_number": PHONE}]})
        if url.endswith("/contacts/9/conversations"):
            return R(200, {"payload": [{"id": 70, "last_activity_at": 1}, {"id": 71, "last_activity_at": 5}]})
        if url.endswith("/conversations/71/messages"):
            return R(200, {"id": 123})
        return R(404, {})

    s = ChatwootSender("https://chat.bitespeed.co", 3122, "cw-secret")
    monkeypatch.setattr(s._http, "request", fake_request)
    res = s.send("k1", {"phone": PHONE, "message": "Order confirmed"})
    assert res.outcome == "sent" and res.provider_object_id == "chatwoot:123" and calls[-1][1] == "/conversations/71/messages"
    g = GuardedSender(s, "live_test", (PHONE,))
    assert g.send("k2", {"phone": "+919999999999", "message": "x"}).outcome == "dry_run"
    assert g.send("k3", {"phone": PHONE, "message": "x"}).outcome == "sent"
    assert GuardedSender(s, "dry_run", (PHONE,)).send("k4", {"phone": PHONE, "message": "x"}).outcome == "dry_run"
