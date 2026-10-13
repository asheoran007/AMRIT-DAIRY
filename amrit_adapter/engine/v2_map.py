"""Automation V2 stage map: which AD-V2-xx stage an engine turn belongs to (config/automation_v2.json is the human
catalogue; this is the runtime mapping used for audit lines and the /bot/turn response)."""
from __future__ import annotations

from .state import TurnResult

_BY_STATE = {"NEW": "AD-V2-00", "PRODUCT": "AD-V2-01", "PACK": "AD-V2-03", "QTY": "AD-V2-03", "ADDRESS_CONFIRM": "AD-V2-04",
             "ADDRESS_COLLECT": "AD-V2-05", "QUOTE": "AD-V2-06", "AWAITING_PAYMENT": "AD-V2-07", "ORDERED": "AD-V2-09", "HANDOFF": "AD-V2-13"}


def stage_for(r: TurnResult, product_key: str | None = None) -> str:
    if r.handoff:
        return "AD-V2-13"
    if "consent:withdrawn" in r.actions:
        return "AD-V2-15"
    if any(a.startswith("track") or a.startswith("tracking") for a in r.actions):
        return "AD-V2-10"
    if "label:lead_honey" in r.actions or (r.state == "PRODUCT" and product_key == "honey"):
        return "AD-V2-02"
    if "label:lead_ghee" in r.actions or r.state == "PRODUCT":
        return "AD-V2-01"
    if r.state == "AWAITING_PAYMENT" and "label:payment_verification_pending" in r.actions:
        return "AD-V2-08"
    return _BY_STATE.get(r.state, "AD-V2-00")
