"""Inbound console (Chatwoot) webhook -> engine turn -> guarded delivery through the console API.
Covers: event filtering, human-owned conversations, kill switch, SENDER_MODE dry_run / live_test (allowlist) / live,
failure handling, dedupe on webhook redelivery, and the outbox ChatwootSender + GuardedSender."""
import json
import time

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
    mid = int(time.time() * 1000) % 10**9          # the API module's DB persists across tests: unique message id per run
    assert c.post("/webhooks/inbox/wrong", json=inbound()).status_code == 401
    r = c.post("/webhooks/inbox/t0k", json=inbound(mid=mid))
    assert r.status_code == 200 and r.json()["status"] == "sent" and sent and sent[0][0] == 777
    assert c.post("/webhooks/inbox/t0k", json=inbound(mid=mid)).text == "duplicate" and len(sent) == 1   # redelivery is a no-op
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


def test_inbox_token_with_slash_and_equals_is_accepted(monkeypatch):
    """Render's generateValue produced a base64 token containing '/' and '=': the path must capture it whole."""
    import amrit_adapter.api as api
    from amrit_adapter.config import settings
    monkeypatch.setattr(settings, "inbox_webhook_token", "abc/def="); monkeypatch.setattr(settings, "sender_mode", "dry_run")
    c = TestClient(api.app)
    assert c.post("/webhooks/inbox/abc/def=", json=inbound(event="conversation_created", mid=int(time.time()) % 10**9)).status_code == 200
    assert c.post("/webhooks/inbox/abc", json=inbound(event="conversation_created")).status_code == 401
    assert c.post("/bot/turn/abc/def=", json={"phone": PHONE, "text": "hi"}).status_code == 200


def test_engine_yields_to_active_bitespeed_keyword_flows_except_for_test_numbers(engine, build):
    """Transitional guard: while AD-01's 'ghee' keyword flow is live, customers get one answer (the flow's), not two."""
    chat = FakeChat()
    kws = ("ghee", "घी", "shahad")
    with engine.begin() as c:
        r = cv.handle_inbound(c, inbound("ghee price?", phone="+919999999999", mid=11), build, chat, "live", (PHONE,), False, kws)
        assert r.status == "skipped" and "keyword flow" in r.reason and not chat.sent
        r = cv.handle_inbound(c, inbound("namaste", phone="+919999999999", mid=12), build, chat, "live", (PHONE,), False, kws)
        assert r.status == "sent" and len(chat.sent) == 1              # non-keyword message: engine answers
        r = cv.handle_inbound(c, inbound("ghee", mid=13), build, chat, "live", (PHONE,), False, kws)
        assert r.status == "sent" and len(chat.sent) == 2              # allowlisted tester still gets the engine
        assert c.execute(text("SELECT COUNT(*) FROM audit_log WHERE action='bot.yield'")).scalar() == 1


def test_inbox_rejects_everything_while_no_token_is_configured(monkeypatch):
    """Token rotation window (var deleted, not yet regenerated): nothing is accepted, nothing is processed."""
    import amrit_adapter.api as api
    from amrit_adapter.config import settings
    monkeypatch.setattr(settings, "inbox_webhook_token", "")
    c = TestClient(api.app)
    assert c.post("/webhooks/inbox/", json=inbound(mid=int(time.time()) % 10**9)).status_code == 401
    assert c.post("/webhooks/inbox/anything", json=inbound(mid=int(time.time()) % 10**9)).status_code == 401
    assert c.post("/bot/turn/anything", json={"phone": PHONE, "text": "hi"}).status_code == 401


def test_access_log_redacts_tokens_and_health_reports_inbox_stats(monkeypatch):
    import logging
    from amrit_adapter.worker import _RedactTokens
    f = _RedactTokens()
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '"POST /webhooks/inbox/abc%2Fdef= HTTP/1.1" 200 OK  GET /bot/turn/zzz GET /x?token=sec&y=1', (), None)
    assert f.filter(rec) and "abc" not in rec.getMessage() and "zzz" not in rec.getMessage() and "sec" not in rec.getMessage()
    assert "/webhooks/inbox/<redacted>" in rec.getMessage() and "token=<redacted>" in rec.getMessage()
    # Regression (Render log 14-15 Oct: 'ValueError: not enough values to unpack (expected 5, got 0)'): a real uvicorn
    # access record carries 5 args and uvicorn's AccessFormatter unpacks them - redaction must keep them intact.
    from uvicorn.logging import AccessFormatter
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                            ("10.0.0.1:1", "POST", "/webhooks/inbox/abc%2Fdef=", "1.1", 200), None)
    assert f.filter(rec) and len(rec.args) == 5 and rec.args[4] == 200
    line = AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False).format(rec)
    assert "abc" not in line and '/webhooks/inbox/<redacted>' in line and "200" in line
    import amrit_adapter.api as api
    from amrit_adapter.config import settings
    monkeypatch.setattr(settings, "inbox_webhook_token", "t0k"); monkeypatch.setattr(settings, "sender_mode", "dry_run")
    c = TestClient(api.app)
    before = dict(api.inbox_stats)
    c.post("/webhooks/inbox/wrong", json=inbound())
    mid = int(time.time() * 1000) % 10**9
    c.post("/webhooks/inbox/t0k", json=inbound(mid=mid)); c.post("/webhooks/inbox/t0k", json=inbound(mid=mid))
    h = c.get("/health").json()["inbox"]
    assert h["rejected"] == before["rejected"] + 1 and h["accepted"] == before["accepted"] + 1 and h["duplicates"] == before["duplicates"] + 1
    assert h["last_accepted"]["conversation_id"] == 777 and h["last_accepted"]["result"].startswith("dry_run")


def test_console_auth_status_is_read_only_and_reported(monkeypatch):
    import amrit_adapter.api as api
    from amrit_adapter.config import settings
    monkeypatch.setattr(settings, "chatwoot_api_token", ""); api._console_check.update(checked_at=None)
    assert api.console_auth_status(force=True)["status"] == "not_configured"
    monkeypatch.setattr(settings, "chatwoot_api_token", "cw-secret")
    calls = []
    class R:
        status_code = 200
        def json(self): return {"id": 1, "name": "Amrit Dairy Care", "accounts": [{"id": 3122}]}
    monkeypatch.setattr(api, "console_auth_status", api.console_auth_status)
    import httpx
    monkeypatch.setattr(httpx, "get", lambda url, headers=None, timeout=None: (calls.append((url, headers["api_access_token"])), R())[1])
    st = api.console_auth_status(force=True)
    assert st["status"] == "ok" and st["agent"] == "Amrit Dairy Care" and st["accounts_ok"] is True
    assert calls and calls[0][0].endswith("/api/v1/profile") and calls[0][1] == "cw-secret"
    h = TestClient(api.app).get("/health").json()
    assert h["console_auth"]["status"] == "ok" and "checked_at" not in h["console_auth"] and "cw-secret" not in str(h)


def test_health_reports_bitespeed_messaging_api_presence_without_using_it(monkeypatch):
    """BITESPEED_API_KEY (official shop-scoped Messaging API) is reported as present/absent only - never its value - and is
    not used for sending until the official docs fix the base URL/endpoints (used_for_sending stays False)."""
    import amrit_adapter.api as api
    from amrit_adapter.config import settings
    c = TestClient(api.app)
    monkeypatch.setattr(settings, "bitespeed_api_key", ""); monkeypatch.setattr(settings, "bitespeed_api_base", "")
    assert c.get("/health").json()["bitespeed_api"] == {"configured": False, "base_url_set": False, "status": "not_configured", "used_for_sending": False}
    monkeypatch.setattr(settings, "bitespeed_api_key", "bsk_live_secret_value_1234567890")
    body = c.get("/health").text
    b = c.get("/health").json()["bitespeed_api"]
    assert b["configured"] is True and b["status"] == "key_present_awaiting_docs" and b["used_for_sending"] is False
    assert "bsk_live" not in body and "1234567890" not in body
    monkeypatch.setattr(settings, "bitespeed_api_base", "https://example.invalid/v1")
    assert c.get("/health").json()["bitespeed_api"]["status"] == "key_and_base_present_unverified"
