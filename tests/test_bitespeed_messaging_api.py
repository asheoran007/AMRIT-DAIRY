"""Official BiteSpeed WhatsApp Messaging API (doc 16 Oct 2026): request contract, key hygiene, reply-channel binding,
outbox sender, SENDER_MODE/allowlist/kill-switch guards and /health readiness. All HTTP is faked - nothing leaves."""
import json
import logging
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from amrit_adapter import bitespeed_api as bs
from amrit_adapter import conversation as cv
from amrit_adapter.sender import BitespeedApiSender, GuardedSender
from tests.test_console_reply_loop import build, inbound  # noqa: F401  (fixture + payload helper)
from tests.test_engine_sequence import PHONE

KEY = "bsk_test_SECRETVALUE_0123456789"
BASE = "https://2lkkwxk9b6.execute-api.us-east-1.amazonaws.com/whatsapp/messages/send/template"


class R:
    def __init__(self, code, body): self.status_code, self._b, self.content, self.text = code, body, b"x", json.dumps(body)
    def json(self): return self._b


@pytest.fixture()
def fake_post(monkeypatch):
    calls = []
    def _post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": json})
        if json.get("recipient", "").endswith("0000"):
            return R(403, {"message": "Forbidden"})
        return R(200, {"message": "Message queued successfully", "thirdPartyMessageId": (json.get("sourceContext") or {}).get("thirdPartyMessageId")})
    monkeypatch.setattr(bs.httpx, "post", _post)
    bs.status.update(last_send_at=None, last_outcome=None, last_http=None, sent=0, failed=0)
    return calls


def test_client_matches_documented_contract_and_never_leaks_key(fake_post, caplog):
    c = bs.BitespeedMessagingClient(KEY, "+91 7700004877", BASE)
    assert "SECRET" not in repr(c) and c.channel_id == "917700004877"
    with caplog.at_level(logging.INFO):
        r = c.send_text("+918814869007", "Namaste! Amrit Dairy", third_party_id="k1")
    call = fake_post[-1]
    assert r.ok and r.http == 200 and r.third_party_id == "k1" and r.message == "Message queued successfully"
    assert call["url"] == BASE and call["headers"] == {"Content-Type": "application/json", "X-API-Key": KEY}
    assert call["json"] == {"channelId": "917700004877", "recipient": "918814869007", "messageType": "text", "text": {"body": "Namaste! Amrit Dairy"},
                            "sourceContext": {"thirdPartyName": "amrit-adapter", "thirdPartyMessageId": "k1"}}
    assert "SECRET" not in caplog.text and "918814869007" not in caplog.text and "****9007" in caplog.text   # key + full phone never logged
    c.send_template("918814869007", "d8fad4f1-da3c-4320-abbe-17ad9bd49266", {"first_name": "Amit"})
    j = fake_post[-1]["json"]
    assert j["template_id"] == "d8fad4f1-da3c-4320-abbe-17ad9bd49266" and j["variables"] == {"first_name": "Amit"} and "messageType" not in j
    c.send_buttons("918814869007", "Choose:", [("ghee_1kg", "Ghee 1 kg"), ("honey", "Honey"), ("x", "A title longer than twenty chars"), ("extra", "4th")])
    btns = fake_post[-1]["json"]["interactive"]["action"]["buttons"]
    assert fake_post[-1]["json"]["messageType"] == "interactive" and len(btns) == 3 and btns[2]["reply"]["title"] == "A title longer than "
    bad = c.send_text("+910000000000", "x")
    assert not bad.ok and bad.http == 403 and bs.status["sent"] == 3 and bs.status["failed"] == 1 and bs.status["last_outcome"] == "rejected"
    with pytest.raises(ValueError):
        bs.BitespeedMessagingClient("", "917700004877")


def test_reply_channel_binds_phone_and_guards_apply(engine, build, fake_post):
    chan = bs.BitespeedReplyChannel(bs.BitespeedMessagingClient(KEY, "917700004877", BASE))
    with engine.begin() as c:
        r = cv.handle_inbound(c, inbound("ghee", mid=301), build, chan, "live_test", (PHONE,), True)        # kill switch wins
        assert r.status == "dry_run" and not fake_post
        r = cv.handle_inbound(c, inbound("ghee", phone="+919999999999", mid=302), build, chan, "live_test", (PHONE,), False)
        assert r.status == "dry_run" and "not on allowlist" in r.reason and not fake_post
        r = cv.handle_inbound(c, inbound("ghee", mid=303), build, chan, "live_test", (PHONE,), False)
        assert r.status == "sent" and len(fake_post) == 2                       # native video + ONE interactive pack list
        v, lst = fake_post[0]["json"], fake_post[1]["json"]
        assert v["messageType"] == "video" and v["media"]["link"].endswith("ghee-approved.mp4")
        assert lst["recipient"] == PHONE.lstrip("+") and lst["messageType"] == "interactive" and lst["interactive"]["type"] == "list"
        assert [row["id"] for row in lst["interactive"]["action"]["sections"][0]["rows"]] == ["pack:1 Ltr", "pack:5 Ltr", "pack:10 Ltr"]
        assert all(len(row["title"]) <= 24 for row in lst["interactive"]["action"]["sections"][0]["rows"])
        assert lst["interactive"]["footer"]["text"] == "हर घर अमृत 🌿"
        assert lst["sourceContext"]["thirdPartyMessageId"].startswith("conv777-")
        r = cv.handle_inbound(c, inbound("ghee", phone="+910000000000", mid=304), build, chan, "live", (), False)   # provider 403 -> failed, audited
        assert r.status == "failed" and "HTTP 403" in r.reason
        rows = c.execute(text("SELECT detail_json FROM audit_log WHERE action='bot.reply' ORDER BY rowid DESC LIMIT 1")).fetchall()
    assert json.loads(rows[0][0])["status"] == "failed"


def test_outbox_sender_and_mode_guard(fake_post):
    s = BitespeedApiSender(bs.BitespeedMessagingClient(KEY, "917700004877", BASE))
    res = s.send("ord-1156-confirm", {"phone": PHONE, "message": "Order confirmed"})
    assert res.outcome == "sent" and res.provider_object_id == "bitespeed:ord-1156-confirm"
    res = s.send("tpl-1", {"phone": PHONE, "template_id": "t-1", "variables": {"first_name": "Amit"}})
    assert res.outcome == "sent" and fake_post[-1]["json"]["template_id"] == "t-1"
    assert s.send("bad", {"phone": "+910000000000", "message": "x"}).outcome == "failed"
    g = GuardedSender(s, "live_test", (PHONE,))
    n = len(fake_post)
    assert g.send("k", {"phone": "+919999999999", "message": "x"}).outcome == "dry_run" and len(fake_post) == n
    assert GuardedSender(s, "dry_run", (PHONE,)).send("k", {"phone": PHONE, "message": "x"}).outcome == "dry_run" and len(fake_post) == n


def test_worker_prefers_messaging_api_then_console_then_dry_run(monkeypatch):
    from amrit_adapter import worker as w
    from amrit_adapter.config import settings
    from amrit_adapter.sender import ChatwootSender, DryRunSender
    monkeypatch.setattr(settings, "sender_mode", "live_test"); monkeypatch.setattr(settings, "bitespeed_api_key", KEY)
    monkeypatch.setattr(settings, "bitespeed_channel_id", "917700004877"); monkeypatch.setattr(settings, "chatwoot_api_token", "cw")
    assert isinstance(w.build_sender().inner, BitespeedApiSender)
    monkeypatch.setattr(settings, "bitespeed_channel_id", "")
    assert isinstance(w.build_sender().inner, ChatwootSender)
    monkeypatch.setattr(settings, "chatwoot_api_token", "")
    assert isinstance(w.build_sender(), DryRunSender)
    monkeypatch.setattr(settings, "sender_mode", "dry_run"); monkeypatch.setattr(settings, "bitespeed_channel_id", "917700004877")
    assert isinstance(w.build_sender(), DryRunSender)


def test_health_reports_readiness_never_the_key(monkeypatch, fake_post):
    import amrit_adapter.api as api
    from amrit_adapter.config import settings
    c = TestClient(api.app)
    monkeypatch.setattr(settings, "bitespeed_api_key", ""); monkeypatch.setattr(settings, "bitespeed_channel_id", "")
    b = c.get("/health").json()["bitespeed_api"]
    assert b["status"] == "not_configured" and b["used_for_sending"] is False and b["verified"] is False
    monkeypatch.setattr(settings, "bitespeed_api_key", KEY)
    assert c.get("/health").json()["bitespeed_api"]["status"] == "key_present_missing_channel_id"
    monkeypatch.setattr(settings, "bitespeed_channel_id", "917700004877")
    body = c.get("/health").text
    b = json.loads(body)["bitespeed_api"]
    assert b["status"] == "ready_unverified_no_probe_endpoint" and b["used_for_sending"] is True and b["channel_id_set"] is True
    assert "SECRET" not in body and KEY not in body
    settings.bitespeed_client().send_text(PHONE, "hi")                       # first real (faked) send flips verified
    b = c.get("/health").json()["bitespeed_api"]
    assert b["status"] == "ok" and b["verified"] is True and b["last_send"]["sent"] == 1 and b["last_send"]["last_http"] == 200


def test_flow_builder_turn_delivers_over_messaging_api_under_guards(monkeypatch, fake_post):
    """/bot/turn (flow-builder API Call node) is the inbound path: the adapter answers over the Messaging API itself.
    Kill switch / dry_run -> computed but not sent; live_test + allowlisted phone -> one API send; audited as source=flow."""
    import amrit_adapter.api as api
    from amrit_adapter.config import settings
    monkeypatch.setattr(settings, "inbox_webhook_token", "t0k"); monkeypatch.setattr(settings, "bitespeed_api_key", KEY)
    monkeypatch.setattr(settings, "bitespeed_channel_id", "917700004877"); monkeypatch.setattr(settings, "test_phone_allowlist", PHONE)
    c = TestClient(api.app)
    monkeypatch.setattr(settings, "sender_mode", "live_test"); monkeypatch.setattr(settings, "global_kill_switch", True)
    j = c.post("/bot/turn/t0k", json={"phone": PHONE.lstrip("+"), "text": "hi", "name": "Amit"}).json()
    assert j["reply_text"] and j["delivery"]["status"] == "dry_run" and "kill switch" in j["delivery"]["reason"] and not fake_post
    monkeypatch.setattr(settings, "global_kill_switch", False)
    j = c.post("/bot/turn/t0k", json={"phone": "+919999999999", "text": "hi"}).json()
    assert j["delivery"]["status"] == "dry_run" and "allowlist" in j["delivery"]["reason"] and not fake_post
    j = c.post("/bot/turn/t0k", json={"phone": PHONE, "text": "hi"}).json()
    assert j["delivery"]["status"] == "sent" and len(fake_post) == 1          # welcome menu as ONE native list
    body = fake_post[0]["json"]
    assert body["recipient"] == PHONE.lstrip("+") and body["interactive"]["type"] == "list" and "AMRIT DAIRY" in body["interactive"]["body"]["text"]
    assert c.post("/bot/turn/wrong", json={"phone": PHONE, "text": "hi"}).status_code == 401


def test_flow_builder_turn_tolerates_variable_aliases_and_unresolved_templates(monkeypatch, fake_post):
    import amrit_adapter.api as api
    from amrit_adapter.config import settings
    monkeypatch.setattr(settings, "inbox_webhook_token", "t0k"); monkeypatch.setattr(settings, "sender_mode", "dry_run")
    c = TestClient(api.app)
    body = {"phone": "{{phone}}", "phone_number": PHONE.lstrip("+"), "text": "{{message}}", "last_message": "hi", "name": "{{name}}"}
    j = c.post("/bot/turn/t0k", json=body).json()
    assert j["reply_text"] and j["delivery"]["status"] == "dry_run"
    assert c.post("/bot/turn/t0k", json={"phone": "{{phone}}", "text": "hi"}).status_code == 400


def test_flow_builder_turn_finds_phone_and_text_under_unknown_variable_names(monkeypatch, fake_post):
    import amrit_adapter.api as api
    from amrit_adapter.config import settings
    monkeypatch.setattr(settings, "inbox_webhook_token", "t0k"); monkeypatch.setattr(settings, "sender_mode", "dry_run")
    c = TestClient(api.app)
    body = {"a": "{{phone}}", "contact": {"phoneNumber": PHONE.lstrip("+"), "firstName": "Amit"}, "msg": "products dikhao", "x": ""}
    j = c.post("/bot/turn/t0k", json=body).json()
    assert j["reply_text"] and j["delivery"]["status"] == "dry_run"



def test_rejected_interactive_falls_back_to_numbered_text(monkeypatch, fake_post):
    calls = []
    def _post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        if json.get("messageType") == "interactive":
            return R(400, {"message": "unsupported interactive"})
        return R(200, {"message": "Message queued successfully"})
    monkeypatch.setattr(bs.httpx, "post", _post)
    from amrit_adapter.engine.state import Msg
    chan = bs.BitespeedReplyChannel(bs.BitespeedMessagingClient(KEY, "917700004877", BASE), PHONE)
    chan.send_messages(1, [Msg("buttons", "कितने पैक?", [("qty:1", "1 पैक"), ("qty:2", "2 पैक")])])
    assert [c["messageType"] for c in calls] == ["interactive", "text"] and "1️⃣ 1 पैक" in calls[1]["text"]["body"]


def test_tapped_button_title_maps_back_to_option(engine):
    from amrit_adapter.engine.state import Engine
    from amrit_adapter.engine.catalog import Catalog
    from amrit_adapter.engine.shopify_client import FakeShopifyClient
    from tests.conftest import BRAND
    e = Engine(Catalog.load(), FakeShopifyClient(), BRAND)
    with engine.begin() as c:
        r = e.turn(c, PHONE, "ghee")
        title = r.messages[-1].rows[1][1]                    # "5 लीटर — ₹17,000" as WhatsApp shows the tapped row
        r = e.turn(c, PHONE, title)
        assert r.state == "QTY"
        r = e.turn(c, PHONE, "2 पैक")                        # tapped reply button
        assert r.state in ("ADDRESS_CONFIRM", "ADDRESS_COLLECT")
