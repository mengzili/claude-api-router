from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import aiohttp

from claude_api_router.config import ApiEntry, RouterConfig
from claude_api_router.state import State


@dataclass
class HealthResult:
    ok: bool
    latency_ms: float | None
    status_code: int | None
    error: str | None


async def ping(
    session: aiohttp.ClientSession,
    entry: ApiEntry,
    model: str,
    timeout_sec: float = 15.0,
) -> HealthResult:
    payload = {
        "model": model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "hi"}],
    }
    url = f"{entry.base_url}/v1/messages"
    t0 = time.monotonic()
    try:
        async with session.post(
            url,
            json=payload,
            headers=entry.auth_headers(),
            timeout=aiohttp.ClientTimeout(total=timeout_sec),
        ) as r:
            await r.read()
            latency = (time.monotonic() - t0) * 1000
            return HealthResult(
                ok=(r.status == 200),
                latency_ms=latency,
                status_code=r.status,
                error=None if r.status == 200 else f"HTTP {r.status}",
            )
    except asyncio.TimeoutError:
        return HealthResult(ok=False, latency_ms=None, status_code=None, error="timeout")
    except aiohttp.ClientError as e:
        return HealthResult(ok=False, latency_ms=None, status_code=None, error=str(e))
    except Exception as e:
        return HealthResult(ok=False, latency_ms=None, status_code=None, error=repr(e))


def preferred_probe_targets(
    cfg: RouterConfig, state: State, now: float | None = None
) -> list[ApiEntry]:
    """Entries we want to ping proactively right now.

    Rule: only probe entries that are more preferred (lower `priority`)
    than the current active upstream AND currently blocked by cooldown.

    Consequences:
      - No active upstream (no traffic yet) -> no probes. Pings wait
        for the first real request.
      - Active upstream is already the most preferred -> no probes.
        There is nothing to upgrade to.
      - Higher-priority entry naturally rejoins the pool after cooldown
        expires -> the next real request will try it; no need to probe.
    """
    active_name = state.active_upstream
    if active_name is None:
        return []
    active = cfg.find(active_name)
    if active is None:
        return []
    now = now if now is not None else time.time()
    return [
        e
        for e in cfg.api
        if e.priority < active.priority and not state.is_available(e, now)
    ]


async def _probe_one(
    session: aiohttp.ClientSession,
    cfg: RouterConfig,
    state: State,
    entry: ApiEntry,
) -> None:
    model = entry.health_model(cfg.proxy.health_check_model)
    prev = state.health.get(entry.name)
    prev_status = prev.status if prev else "unknown"
    result = await ping(session, entry, model)
    state.record_health(
        entry,
        ok=result.ok,
        latency_ms=result.latency_ms,
        error=result.error,
        status_code=result.status_code,
        auth_failure_cooldown=cfg.proxy.auth_failure_cooldown,
    )
    new_status = state.health[entry.name].status
    if new_status != prev_status:
        state.log("health", f"{entry.name}: {prev_status} -> {new_status}")


async def check_all(
    session: aiohttp.ClientSession,
    cfg: RouterConfig,
    state: State,
) -> None:
    """Ping every configured entry. Used by the CLI `test` command for
    on-demand diagnostics — not by the background loop."""
    if cfg.api:
        await asyncio.gather(*(_probe_one(session, cfg, state, e) for e in cfg.api))


async def run_health_loop(
    cfg: RouterConfig,
    state: State,
    stop: asyncio.Event,
) -> None:
    """Background loop that probes only when there is an obvious upgrade
    target (a more-preferred upstream sitting in cooldown)."""
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while not stop.is_set():
            if not state.health_paused:
                try:
                    targets = preferred_probe_targets(cfg, state)
                    if targets:
                        await asyncio.gather(
                            *(_probe_one(session, cfg, state, e) for e in targets)
                        )
                except Exception as e:
                    state.log("info", f"health loop error: {e!r}")
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=cfg.proxy.health_check_interval
                )
            except asyncio.TimeoutError:
                pass
