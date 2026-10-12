"""Wallet / budget guard (F39).

- Top-ups are funding, not evidence of spend. Spend is estimated per queued action and later replaced by
  actual ledger entries when a provider ledger export is available.
- Operational reserve: promotional sends pause before the reserve is consumed; service sends continue and
  escalate instead of silently dropping.
- Global kill switch defaults ON (no promotional sends) until the owner approves a budget.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .db import new_id, now_iso


@dataclass(frozen=True)
class BudgetState:
    balance_minor: int
    reserve_minor: int
    monthly_cap_minor: int | None
    month_spend_minor: int
    kill_switch: bool

    def allows(self, category: str, est_cost_minor: int) -> tuple[bool, str]:
        if category == "promotional":
            if self.kill_switch:
                return False, "global kill switch engaged"
            if self.monthly_cap_minor is None:
                return False, "no approved monthly budget"
            if self.month_spend_minor + est_cost_minor > self.monthly_cap_minor:
                return False, "monthly promotional cap reached"
            if self.balance_minor - est_cost_minor < self.reserve_minor:
                return False, "would consume operational reserve"
            return True, "ok"
        # service / internal: allowed while balance covers it; escalate when low but never drop silently
        if self.balance_minor - est_cost_minor < 0:
            return False, "wallet exhausted: escalate to owner (service message held, not dropped)"
        return True, "ok"


def post(conn: Connection, brand_id: str, kind: str, amount_minor: int, category: str | None = None, reference: str | None = None) -> None:
    conn.execute(
        text("INSERT INTO budget_ledger(entry_id, brand_id, at, kind, category, amount_minor, reference) VALUES (:i,:b,:t,:k,:c,:a,:r)"),
        {"i": new_id(), "b": brand_id, "t": now_iso(), "k": kind, "c": category, "a": amount_minor, "r": reference},
    )


def state(conn: Connection, brand_id: str, reserve_minor: int, monthly_cap_minor: int | None, kill_switch: bool, month_prefix: str) -> BudgetState:
    bal = conn.execute(text("SELECT COALESCE(SUM(amount_minor),0) FROM budget_ledger WHERE brand_id=:b"), {"b": brand_id}).scalar() or 0
    spend = conn.execute(
        text("""SELECT COALESCE(-SUM(amount_minor),0) FROM budget_ledger WHERE brand_id=:b AND category='promotional'
                AND amount_minor<0 AND at LIKE :m"""), {"b": brand_id, "m": f"{month_prefix}%"}
    ).scalar() or 0
    return BudgetState(int(bal), reserve_minor, monthly_cap_minor, int(spend), kill_switch)


def runway_days(conn: Connection, brand_id: str, trailing_days: int = 7) -> float | None:
    """Trailing-average daily spend -> days of runway. None when there is no spend history."""
    rows = conn.execute(
        text("SELECT at, amount_minor FROM budget_ledger WHERE brand_id=:b AND amount_minor<0 ORDER BY at DESC"), {"b": brand_id}
    ).fetchall()
    if not rows:
        return None
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=trailing_days)).isoformat()
    spend = sum(-r.amount_minor for r in rows if r.at >= cutoff)
    if spend <= 0:
        return None
    bal = conn.execute(text("SELECT COALESCE(SUM(amount_minor),0) FROM budget_ledger WHERE brand_id=:b"), {"b": brand_id}).scalar() or 0
    return round(bal / (spend / trailing_days), 1)
