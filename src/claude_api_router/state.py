from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable, Literal

from claude_api_router.config import ApiEntry


HealthStatus = Literal["unknown", "healthy", "slow", "failed", "auth_error"]


@dataclass
class UpstreamHealth:
    name: str
    status: HealthStatus = "unknown"
    last_check: float | None = None
    last_latency_ms: float | None = None
    last_error: str | None = None
    cooldown_until: float = 0.0


@dataclass
class Event:
    at: float
    kind: str  # "switch" | "fail" | "health" | "info"
    message: str


@dataclass
class State:
    health: dict[str, UpstreamHealth] = field(default_factory=dict)
    events: deque[Event] = field(default_factory=lambda: deque(maxlen=100))
    active_upstream: str | None = None
    health_paused: bool = False

    def ensure(self, entry: ApiEntry) -> UpstreamHealth:
        h = self.health.get(entry.name)
        if h is None:
            h = UpstreamHealth(name=entry.name)
            self.health[entry.name] = h
        return h

    def record_health(
        self,
        entry: ApiEntry,
        *,
        ok: bool,
        latency_ms: float | None,
        error: str | None,
        status_code: int | None,
        auth_failure_cooldown: float,
    ) -> None:
        h = self.ensure(entry)
        h.last_check = time.time()
        h.last_latency_ms = latency_ms
        h.last_error = error
        if ok:
            h.status = "healthy"
            h.cooldown_until = 0.0
        elif status_code in (401, 403):
            h.status = "auth_error"
            h.cooldown_until = time.time() + auth_failure_cooldown
        else:
            h.status = "failed"

    def mark_slow(self, entry: ApiEntry, cooldown_sec: float, reason: str) -> None:
        h = self.ensure(entry)
        h.status = "slow"
        h.last_error = reason
        h.cooldown_until = time.time() + cooldown_sec
        self.log("fail", f"{entry.name}: {reason} (cooldown {int(cooldown_sec)}s)")

    def mark_failed(self, entry: ApiEntry, cooldown_sec: float, reason: str) -> None:
        h = self.ensure(entry)
        h.status = "failed"
        h.last_error = reason
        h.cooldown_until = time.time() + cooldown_sec
        self.log("fail", f"{entry.name}: {reason} (cooldown {int(cooldown_sec)}s)")

    def record_success(self, entry: ApiEntry) -> None:
        h = self.ensure(entry)
        if h.status != "healthy":
            self.log("health", f"{entry.name}: recovered")
        h.status = "healthy"
        h.cooldown_until = 0.0
        self.active_upstream = entry.name

    def is_available(self, entry: ApiEntry, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        h = self.health.get(entry.name)
        if h is None:
            return True  # give unknown entries a chance
        # cooldown_until is the single source of truth for "skip this".
        # Once it expires, the upstream is retryable — the next attempt will
        # either succeed (resetting status) or re-mark it failed.
        return h.cooldown_until <= now

    def log(self, kind: str, message: str) -> None:
        self.events.append(Event(at=time.time(), kind=kind, message=message))
