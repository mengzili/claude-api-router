from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

from claude_api_router import config as config_mod
from claude_api_router.config import ApiEntry, ProxyConfig, RouterConfig
from claude_api_router.proxy import make_app
from claude_api_router.state import State


async def _fake_fast():
    async def handler(request: web.Request) -> web.Response:
        return web.json_response(
            {"ok": True, "key": request.headers.get("x-api-key", "")}
        )

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[attr-defined]
    return runner, f"http://127.0.0.1:{port}"


async def _start_proxy_with_admin(
    cfg: RouterConfig,
    state: State,
    config_path: Path,
    stop_event: asyncio.Event | None = None,
):
    app = make_app(cfg, state, config_path=config_path, stop_event=stop_event)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[attr-defined]
    return runner, f"http://127.0.0.1:{port}"


@pytest.mark.asyncio
async def test_admin_page_served_and_config_roundtrip(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    cfg = RouterConfig(
        proxy=ProxyConfig(listen_port=0, ttfb_timeout=1.0),
        api=[ApiEntry(name="a", base_url="https://example.com", api_key="k1", priority=1)],
    )
    config_mod.save(cfg, config_path)
    state = State()

    runner, url = await _start_proxy_with_admin(cfg, state, config_path)
    try:
        async with aiohttp.ClientSession() as s:
            # Index page
            async with s.get(f"{url}/_admin") as r:
                assert r.status == 200
                html = await r.text()
                assert "claude-api-router" in html
                assert "<table" in html

            # GET config
            async with s.get(f"{url}/_admin/api/config") as r:
                j = await r.json()
                assert len(j["api"]) == 1
                assert j["api"][0]["name"] == "a"

            # PUT config: add an entry, change priority
            payload = {
                "api": [
                    {
                        "name": "a",
                        "base_url": "https://example.com",
                        "api_key": "k1",
                        "auth_token": None,
                        "priority": 2,
                    },
                    {
                        "name": "b",
                        "base_url": "https://b.example",
                        "api_key": None,
                        "auth_token": "t2",
                        "priority": 1,
                    },
                ]
            }
            async with s.put(f"{url}/_admin/api/config", json=payload) as r:
                assert r.status == 200
                j = await r.json()
                assert len(j["api"]) == 2

        # Hot reload: in-memory cfg updated
        assert len(cfg.api) == 2
        assert cfg.find("b").auth_token == "t2"
        assert cfg.find("a").priority == 2

        # Disk: the config file was actually written
        reloaded = config_mod.load(config_path)
        assert [e.name for e in reloaded.api] == ["a", "b"]
        assert reloaded.find("b").auth_token == "t2"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_put_rejects_missing_credential(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    cfg = RouterConfig(proxy=ProxyConfig(listen_port=0), api=[])
    config_mod.save(cfg, config_path)
    state = State()

    runner, url = await _start_proxy_with_admin(cfg, state, config_path)
    try:
        async with aiohttp.ClientSession() as s:
            payload = {
                "api": [
                    {
                        "name": "a",
                        "base_url": "https://x.com",
                        "api_key": "",
                        "auth_token": "",
                        "priority": 1,
                    }
                ]
            }
            async with s.put(f"{url}/_admin/api/config", json=payload) as r:
                assert r.status == 400
                j = await r.json()
                assert "row 1" in j["error"]
        # in-memory and disk state unchanged
        assert cfg.api == []
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_put_rejects_duplicate_names(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    cfg = RouterConfig(proxy=ProxyConfig(listen_port=0), api=[])
    config_mod.save(cfg, config_path)
    state = State()

    runner, url = await _start_proxy_with_admin(cfg, state, config_path)
    try:
        async with aiohttp.ClientSession() as s:
            payload = {
                "api": [
                    {"name": "dup", "base_url": "https://a", "api_key": "k", "priority": 1},
                    {"name": "dup", "base_url": "https://b", "api_key": "k", "priority": 2},
                ]
            }
            async with s.put(f"{url}/_admin/api/config", json=payload) as r:
                assert r.status == 400
                j = await r.json()
                assert "duplicate" in j["error"].lower()
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_test_endpoint_pings_upstream(tmp_path: Path):
    fast_runner, fast_url = await _fake_fast()
    try:
        config_path = tmp_path / "config.toml"
        cfg = RouterConfig(
            proxy=ProxyConfig(listen_port=0),
            api=[ApiEntry(name="probe", base_url=fast_url, api_key="k", priority=1)],
        )
        config_mod.save(cfg, config_path)
        state = State()

        runner, url = await _start_proxy_with_admin(cfg, state, config_path)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(f"{url}/_admin/api/test/probe") as r:
                    assert r.status == 200
                    j = await r.json()
                    assert j["ok"] is True
                    assert j["latency_ms"] >= 0

                async with s.post(f"{url}/_admin/api/test/nope") as r:
                    assert r.status == 404
        finally:
            await runner.cleanup()
    finally:
        await fast_runner.cleanup()


@pytest.mark.asyncio
async def test_proxy_uses_hot_reloaded_entries(tmp_path: Path):
    # Start with NO entries. Admin adds one. Next proxy request must use it.
    fast_runner, fast_url = await _fake_fast()
    try:
        config_path = tmp_path / "config.toml"
        cfg = RouterConfig(proxy=ProxyConfig(listen_port=0, ttfb_timeout=1.0), api=[])
        config_mod.save(cfg, config_path)
        state = State()

        runner, url = await _start_proxy_with_admin(cfg, state, config_path)
        try:
            async with aiohttp.ClientSession() as s:
                # Initially 503 — no upstreams.
                async with s.post(f"{url}/v1/messages", json={}) as r:
                    assert r.status == 503

                # Add one via admin
                payload = {
                    "api": [
                        {
                            "name": "live",
                            "base_url": fast_url,
                            "api_key": "k1",
                            "priority": 1,
                        }
                    ]
                }
                async with s.put(f"{url}/_admin/api/config", json=payload) as r:
                    assert r.status == 200

                # Now the proxy request should succeed using the new entry.
                async with s.post(f"{url}/v1/messages", json={}) as r:
                    assert r.status == 200
                    j = await r.json()
                    assert j["key"] == "k1"
        finally:
            await runner.cleanup()
    finally:
        await fast_runner.cleanup()


@pytest.mark.asyncio
async def test_settings_get_and_partial_put(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    cfg = RouterConfig(
        proxy=ProxyConfig(listen_port=0, ttfb_timeout=20.0, health_check_interval=60.0),
        api=[],
    )
    config_mod.save(cfg, config_path)
    state = State()

    runner, url = await _start_proxy_with_admin(cfg, state, config_path)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{url}/_admin/api/settings") as r:
                assert r.status == 200
                j = await r.json()
                assert j["proxy"]["ttfb_timeout"] == 20.0

            # Partial update: only send ttfb_timeout; other fields preserved.
            async with s.put(
                f"{url}/_admin/api/settings",
                json={"proxy": {"ttfb_timeout": 5.0}},
            ) as r:
                assert r.status == 200
                j = await r.json()
                assert j["proxy"]["ttfb_timeout"] == 5.0
                assert j["proxy"]["health_check_interval"] == 60.0
                assert j["restart_required"] == []

        # Hot-reload: in-memory cfg.proxy updated.
        assert cfg.proxy.ttfb_timeout == 5.0
        # Disk: written.
        reloaded = config_mod.load(config_path)
        assert reloaded.proxy.ttfb_timeout == 5.0
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_settings_put_flags_restart_required(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    cfg = RouterConfig(proxy=ProxyConfig(listen_port=8787), api=[])
    config_mod.save(cfg, config_path)
    state = State()

    runner, url = await _start_proxy_with_admin(cfg, state, config_path)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.put(
                f"{url}/_admin/api/settings",
                json={"proxy": {"listen_port": 9999, "ttfb_timeout": 10.0}},
            ) as r:
                assert r.status == 200
                j = await r.json()
                assert "listen_port" in j["restart_required"]
                assert "ttfb_timeout" not in j["restart_required"]
        # Both persisted anyway — restart is about *effect*, not save.
        assert cfg.proxy.listen_port == 9999
        assert cfg.proxy.ttfb_timeout == 10.0
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_settings_put_rejects_bogus_value(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    cfg = RouterConfig(proxy=ProxyConfig(listen_port=0), api=[])
    config_mod.save(cfg, config_path)
    state = State()

    runner, url = await _start_proxy_with_admin(cfg, state, config_path)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.put(
                f"{url}/_admin/api/settings",
                json={"proxy": {"ttfb_timeout": "not-a-number"}},
            ) as r:
                assert r.status == 400
        # cfg unchanged
        assert cfg.proxy.ttfb_timeout == 10.0
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_stats_empty_returns_zero_series(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    cfg = RouterConfig(proxy=ProxyConfig(listen_port=0), api=[])
    config_mod.save(cfg, config_path)
    state = State()
    runner, url = await _start_proxy_with_admin(cfg, state, config_path)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{url}/_admin/api/stats") as r:
                assert r.status == 200
                j = await r.json()
                assert j["bucket_sec"] == 3600
                assert j["window_sec"] == 86400
                # 24h / 1h = 24 buckets
                assert len(j["buckets"]) == 24
                assert j["series"] == {}
                assert j["totals"] == {}
    finally:
        await runner.cleanup()


def test_stats_bucket_series_deterministic():
    """Exercise the pure bucketer directly so the test isn't flaky near
    a real wall-clock bucket boundary."""
    from claude_api_router.admin import _bucket_series

    # Pin `now` to the MIDDLE of a bucket so subtractions don't
    # accidentally cross boundaries. End bucket (index n-1) covers
    # [bucket_start, bucket_start+600); "20 minutes ago" from the middle
    # then unambiguously lands in bucket n-3.
    bucket_start = (int(1_700_000_000) // 600) * 600
    now = float(bucket_start + 300)  # half-way into the current bucket

    log = {
        "poe":   [now - 10, now - 20, now - 30],     # final bucket ×3
        "pincc": [now - 1200 + 10],                  # bucket n-3 ×1
    }
    buckets, series = _bucket_series(
        log, bucket_sec=600, window_sec=14400, now=now
    )
    assert len(buckets) == 24
    assert sum(series["poe"]) == 3
    assert sum(series["pincc"]) == 1
    assert series["poe"][-1] == 3
    assert series["pincc"][-3] == 1
    # Timestamps older than the window are dropped
    log["poe"].append(now - 20000)
    _, series = _bucket_series(
        log, bucket_sec=600, window_sec=14400, now=now
    )
    assert sum(series["poe"]) == 3  # still 3, out-of-window one ignored


@pytest.mark.asyncio
async def test_stats_endpoint_returns_recorded_requests(tmp_path: Path):
    """End-to-end: real HTTP request, real time.time(). Only asserts on
    totals so the test is immune to bucket-boundary races."""
    import time as _t

    config_path = tmp_path / "config.toml"
    cfg = RouterConfig(proxy=ProxyConfig(listen_port=0), api=[])
    config_mod.save(cfg, config_path)
    state = State()

    now = _t.time()
    for _ in range(3):
        state.record_request("poe", at=now - 10)
    state.record_request("pincc", at=now - 1200)

    runner, url = await _start_proxy_with_admin(cfg, state, config_path)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"{url}/_admin/api/stats?bucket_sec=600&window_sec=14400"
            ) as r:
                j = await r.json()
                assert j["totals"]["poe"] == 3
                assert j["totals"]["pincc"] == 1
                # Both must land *somewhere* in the 4h window.
                assert sum(j["series"]["poe"]) == 3
                assert sum(j["series"]["pincc"]) == 1
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_stats_rejects_non_integer_params(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    cfg = RouterConfig(proxy=ProxyConfig(listen_port=0), api=[])
    config_mod.save(cfg, config_path)
    state = State()
    runner, url = await _start_proxy_with_admin(cfg, state, config_path)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{url}/_admin/api/stats?bucket_sec=abc") as r:
                assert r.status == 400
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_shutdown_endpoint_sets_stop_event(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    cfg = RouterConfig(proxy=ProxyConfig(listen_port=0), api=[])
    config_mod.save(cfg, config_path)
    state = State()
    stop = asyncio.Event()
    runner, url = await _start_proxy_with_admin(cfg, state, config_path, stop)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{url}/_admin/api/shutdown") as r:
                assert r.status == 202
                j = await r.json()
                assert j["ok"] is True
        # Endpoint defers ~200ms before setting stop.
        await asyncio.wait_for(stop.wait(), timeout=2.0)
        assert stop.is_set()
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_shutdown_endpoint_errors_without_stop_event(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    cfg = RouterConfig(proxy=ProxyConfig(listen_port=0), api=[])
    config_mod.save(cfg, config_path)
    state = State()
    runner, url = await _start_proxy_with_admin(cfg, state, config_path, None)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{url}/_admin/api/shutdown") as r:
                assert r.status == 500
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_health_endpoint_shape(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    cfg = RouterConfig(
        proxy=ProxyConfig(listen_host="127.0.0.1", listen_port=8787),
        api=[ApiEntry(name="a", base_url="https://x", api_key="k", priority=1)],
    )
    config_mod.save(cfg, config_path)
    state = State()

    runner, url = await _start_proxy_with_admin(cfg, state, config_path)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{url}/_admin/api/health") as r:
                assert r.status == 200
                j = await r.json()
                assert "health" in j and isinstance(j["health"], list)
                assert j["health"][0]["name"] == "a"
                assert "listen" in j
                assert "active_upstream" in j
    finally:
        await runner.cleanup()
