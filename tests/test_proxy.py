"""Integration tests for the streaming proxy using real aiohttp fake upstreams."""
from __future__ import annotations

import asyncio
import json
from typing import Callable

import aiohttp
import pytest
from aiohttp import web

from claude_api_router.config import ApiEntry, ProxyConfig, RouterConfig
from claude_api_router.proxy import make_app
from claude_api_router.state import State


async def _fake_upstream(behavior: str, *, chunks: list[bytes] | None = None):
    """Return a started TestServer that responds according to `behavior`."""

    async def handler(request: web.Request) -> web.StreamResponse:
        # Echo the x-api-key so tests can assert auth injection.
        api_key = request.headers.get("x-api-key", "")
        if behavior == "fast":
            body = chunks or [b'{"ok":true,"key":"' + api_key.encode() + b'"}']
            resp = web.StreamResponse(status=200, headers={"Content-Type": "application/json"})
            await resp.prepare(request)
            for c in body:
                await resp.write(c)
            await resp.write_eof()
            return resp
        if behavior == "sse":
            resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
            await resp.prepare(request)
            for c in chunks or [b"event: a\ndata: 1\n\n", b"event: b\ndata: 2\n\n"]:
                await resp.write(c)
                await asyncio.sleep(0.01)
            await resp.write_eof()
            return resp
        if behavior == "slow":
            # Headers flush, then long delay before any body — triggers TTFB timeout.
            resp = web.StreamResponse(status=200)
            await resp.prepare(request)
            await asyncio.sleep(5)  # longer than test's ttfb_timeout
            await resp.write(b"too late")
            await resp.write_eof()
            return resp
        if behavior == "reset":
            # Force connection reset by not sending a valid response.
            raise ConnectionResetError("boom")
        if behavior == "http5xx":
            return web.Response(status=502, text="bad gateway")
        if behavior == "auth401":
            return web.Response(status=401, text="nope")
        raise AssertionError(f"unknown behavior: {behavior}")

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    # Grab the bound port
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[attr-defined]
    return runner, f"http://127.0.0.1:{port}"


async def _start_proxy(cfg: RouterConfig, state: State):
    app = make_app(cfg, state)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[attr-defined]
    return runner, f"http://127.0.0.1:{port}"


def _make_cfg(urls_by_name: list[tuple[str, str, int]], ttfb: float = 1.0) -> RouterConfig:
    return RouterConfig(
        proxy=ProxyConfig(
            listen_host="127.0.0.1",
            listen_port=0,
            health_check_interval=3600,
            ttfb_timeout=ttfb,
            degraded_cooldown=60,
        ),
        api=[
            ApiEntry(name=n, base_url=u, api_key=f"key-{n}", priority=p)
            for (n, u, p) in urls_by_name
        ],
    )


@pytest.mark.asyncio
async def test_happy_path_uses_highest_priority():
    fast_runner, fast_url = await _fake_upstream("fast")
    try:
        cfg = _make_cfg([("primary", fast_url, 1)])
        state = State()
        pr_runner, pr_url = await _start_proxy(cfg, state)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(f"{pr_url}/v1/messages", json={"x": 1}) as r:
                    assert r.status == 200
                    body = await r.json()
                    assert body["ok"] is True
                    assert body["key"] == "key-primary"
            assert state.active_upstream == "primary"
        finally:
            await pr_runner.cleanup()
    finally:
        await fast_runner.cleanup()


@pytest.mark.asyncio
async def test_failover_on_ttfb_timeout():
    slow_runner, slow_url = await _fake_upstream("slow")
    fast_runner, fast_url = await _fake_upstream("fast")
    try:
        cfg = _make_cfg(
            [("primary", slow_url, 1), ("fallback", fast_url, 2)], ttfb=0.5
        )
        state = State()
        pr_runner, pr_url = await _start_proxy(cfg, state)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(f"{pr_url}/v1/messages", json={"x": 1}) as r:
                    assert r.status == 200
                    body = await r.json()
                    assert body["key"] == "key-fallback"
            assert state.active_upstream == "fallback"
            assert state.health["primary"].status == "slow"
            assert state.health["primary"].cooldown_until > 0
        finally:
            await pr_runner.cleanup()
    finally:
        await slow_runner.cleanup()
        await fast_runner.cleanup()


@pytest.mark.asyncio
async def test_failover_on_connection_error():
    dead_runner, dead_url = await _fake_upstream("reset")
    fast_runner, fast_url = await _fake_upstream("fast")
    try:
        cfg = _make_cfg([("broken", dead_url, 1), ("good", fast_url, 2)])
        state = State()
        pr_runner, pr_url = await _start_proxy(cfg, state)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(f"{pr_url}/v1/messages", json={"x": 1}) as r:
                    body = await r.json()
                    assert r.status == 200
                    assert body["key"] == "key-good"
        finally:
            await pr_runner.cleanup()
    finally:
        await dead_runner.cleanup()
        await fast_runner.cleanup()


@pytest.mark.asyncio
async def test_failover_on_5xx():
    bad_runner, bad_url = await _fake_upstream("http5xx")
    fast_runner, fast_url = await _fake_upstream("fast")
    try:
        cfg = _make_cfg([("bad", bad_url, 1), ("good", fast_url, 2)])
        state = State()
        pr_runner, pr_url = await _start_proxy(cfg, state)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(f"{pr_url}/v1/messages", json={}) as r:
                    assert r.status == 200
                    assert (await r.json())["key"] == "key-good"
            assert state.health["bad"].status == "failed"
        finally:
            await pr_runner.cleanup()
    finally:
        await bad_runner.cleanup()
        await fast_runner.cleanup()


@pytest.mark.asyncio
async def test_auth_error_longer_cooldown():
    auth_runner, auth_url = await _fake_upstream("auth401")
    fast_runner, fast_url = await _fake_upstream("fast")
    try:
        cfg = _make_cfg([("authbad", auth_url, 1), ("good", fast_url, 2)])
        state = State()
        pr_runner, pr_url = await _start_proxy(cfg, state)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(f"{pr_url}/v1/messages", json={}) as r:
                    assert r.status == 200
            h = state.health["authbad"]
            assert h.status == "auth_error"
            # auth cooldown should be well beyond normal degraded_cooldown
            import time as _t
            assert h.cooldown_until - _t.time() > cfg.proxy.degraded_cooldown
        finally:
            await pr_runner.cleanup()
    finally:
        await auth_runner.cleanup()
        await fast_runner.cleanup()


@pytest.mark.asyncio
async def test_all_fail_returns_503():
    d1_runner, d1_url = await _fake_upstream("reset")
    d2_runner, d2_url = await _fake_upstream("reset")
    try:
        cfg = _make_cfg([("a", d1_url, 1), ("b", d2_url, 2)])
        state = State()
        pr_runner, pr_url = await _start_proxy(cfg, state)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(f"{pr_url}/v1/messages", json={}) as r:
                    assert r.status == 503
                    body = await r.json()
                    assert body["error"] == "all_upstreams_failed"
                    assert len(body["attempts"]) == 2
        finally:
            await pr_runner.cleanup()
    finally:
        await d1_runner.cleanup()
        await d2_runner.cleanup()


@pytest.mark.asyncio
async def test_sse_chunks_pass_through():
    chunks = [
        b"event: message_start\ndata: {\"type\":\"message_start\"}\n\n",
        b"event: content_block_delta\ndata: {\"delta\":{\"text\":\"hello\"}}\n\n",
        b"event: message_stop\ndata: {}\n\n",
    ]
    sse_runner, sse_url = await _fake_upstream("sse", chunks=chunks)
    try:
        cfg = _make_cfg([("primary", sse_url, 1)])
        state = State()
        pr_runner, pr_url = await _start_proxy(cfg, state)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(f"{pr_url}/v1/messages", json={"stream": True}) as r:
                    assert r.status == 200
                    assert "text/event-stream" in r.headers.get("Content-Type", "")
                    body = await r.read()
                    for c in chunks:
                        assert c in body
        finally:
            await pr_runner.cleanup()
    finally:
        await sse_runner.cleanup()
