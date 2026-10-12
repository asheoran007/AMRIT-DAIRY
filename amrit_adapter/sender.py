"""Outbound channel adapters.

BiteSpeed: as of 2026-10-11 no public API / custom-event / send contract is documented (help centre and search
checked; only a partner 'API key' is referenced by AppBrew's guide). Therefore the only implemented sender is
DryRunSender. LiveBiteSpeedSender raises until a written contract (endpoint, auth, idempotency key, response
ids, rate limits) is obtained from BiteSpeed and recorded in CAPABILITY-LIMITS.json.
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


class DryRunSender:
    """Records what WOULD be sent. Never touches a provider."""

    def __init__(self) -> None:
        self.log: list[tuple[str, dict]] = []

    def send(self, action_key: str, payload: dict) -> SendResult:
        self.log.append((action_key, payload))
        return SendResult("dry_run", f"dryrun:{action_key}", 0, "simulated; nothing delivered")


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
