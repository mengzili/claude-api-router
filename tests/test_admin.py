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
    cfg: RouterConfig, state: State, config_path: Path
):
    app = make_app(cfg, state, config_path=config_path)
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
        assert cfg.proxy.ttfb_timeout == 20.0
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
