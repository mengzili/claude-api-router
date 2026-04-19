from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from claude_api_router import selector
from claude_api_router.admin import register_admin
from claude_api_router.config import ApiEntry, RouterConfig
from claude_api_router.state import State


# Headers we must not forward from the client to the upstream.
# - host: aiohttp re-derives from upstream URL
# - content-length / transfer-encoding: aiohttp sets correctly based on data
# - x-api-key / authorization: replaced with upstream credentials
# - connection / te / upgrade / proxy-*: hop-by-hop per RFC 7230 §6.1
_REQUEST_STRIP = {
    "host",
    "content-length",
    "transfer-encoding",
    "x-api-key",
    "authorization",
    "connection",
    "keep-alive",
    "te",
    "trailers",
    "upgrade",
    "proxy-authenticate",
    "proxy-authorization",
    "accept-encoding",  # let aiohttp handle; we auto-decompress upstream
}

# Headers not to pass through from upstream response to client.
_RESPONSE_STRIP = {
    "content-length",
    "content-encoding",  # auto_decompress means we serve decoded bytes
    "transfer-encoding",
    "connection",
    "keep-alive",
}


def _build_upstream_headers(
    client_headers: aiohttp.typedefs.LooseHeaders, entry: ApiEntry
) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in client_headers.items():
        if k.lower() in _REQUEST_STRIP:
            continue
        out[k] = v
    out.update(entry.auth_headers())
    return out


def _filter_response_headers(
    upstream_headers: aiohttp.typedefs.LooseHeaders,
) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in upstream_headers.items():
        if k.lower() in _RESPONSE_STRIP:
            continue
        out[k] = v
    return out


async def _try_upstream(
    session: aiohttp.ClientSession,
    entry: ApiEntry,
    method: str,
    path_qs: str,
    body: bytes,
    client_headers: aiohttp.typedefs.LooseHeaders,
    ttfb_timeout: float,
) -> tuple[aiohttp.ClientResponse, bytes, Any]:
    """Send request to upstream, wait for first body chunk.

    Returns (response, first_chunk, request_cm). Caller is responsible for
    exiting the context manager and reading the rest of the body.
    """
    url = f"{entry.base_url}{path_qs}"
    headers = _build_upstream_headers(client_headers, entry)
    cm = session.request(method, url, data=body, headers=headers)
    async with asyncio.timeout(ttfb_timeout):
        upstream = await cm.__aenter__()
        first = await upstream.content.readany()
    return upstream, first, cm


def make_app(
    cfg: RouterConfig,
    state: State,
    config_path: Path | None = None,
) -> web.Application:
    # Session is created lazily per-run and attached via on_startup so it
    # lives on the same loop as the server.
    app = web.Application(client_max_size=cfg.proxy.max_buffer_bytes)

    async def on_startup(app: web.Application) -> None:
        connector = aiohttp.TCPConnector(limit=0, force_close=False)
        app["session"] = aiohttp.ClientSession(
            connector=connector,
            auto_decompress=True,
            # No total timeout here; TTFB is enforced explicitly.
        )

    async def on_cleanup(app: web.Application) -> None:
        session = app.get("session")
        if session is not None:
            await session.close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    async def handle(request: web.Request) -> web.StreamResponse:
        session: aiohttp.ClientSession = request.app["session"]
        try:
            body = await request.read()
        except Exception as e:
            return web.json_response(
                {"error": "bad_request_body", "detail": str(e)}, status=400
            )

        method = request.method
        path_qs = request.rel_url.raw_path_qs

        attempts: list[dict[str, str]] = []
        candidates = selector.ordered_available(cfg, state)
        if not candidates:
            # Fall back to trying everything in priority order rather than
            # immediately 503'ing — health state may just be stale.
            candidates = selector.ordered_all(cfg)

        for entry in candidates:
            upstream = None
            cm = None
            committed = False
            try:
                upstream, first, cm = await _try_upstream(
                    session,
                    entry,
                    method,
                    path_qs,
                    body,
                    request.headers,
                    cfg.proxy.ttfb_timeout,
                )

                # Non-2xx counts as a failure worth trying the next upstream
                # for — gateways often return 502/503 on transient issues.
                if upstream.status >= 500:
                    msg = f"HTTP {upstream.status}"
                    state.mark_failed(entry, cfg.proxy.degraded_cooldown, msg)
                    attempts.append({"upstream": entry.name, "reason": msg})
                    continue
                if upstream.status in (401, 403):
                    msg = f"HTTP {upstream.status} (auth)"
                    state.record_health(
                        entry,
                        ok=False,
                        latency_ms=None,
                        error=msg,
                        status_code=upstream.status,
                        auth_failure_cooldown=cfg.proxy.auth_failure_cooldown,
                    )
                    attempts.append({"upstream": entry.name, "reason": msg})
                    continue

                # Commit to this upstream — start streaming to client.
                committed = True
                prev_active = state.active_upstream
                if prev_active and prev_active != entry.name:
                    state.log(
                        "switch",
                        f"{prev_active} -> {entry.name}",
                    )
                state.record_success(entry)
                state.record_request(entry.name)

                resp = web.StreamResponse(
                    status=upstream.status,
                    headers=_filter_response_headers(upstream.headers),
                )
                await resp.prepare(request)
                if first:
                    await resp.write(first)
                async for chunk in upstream.content.iter_any():
                    if not chunk:
                        break
                    await resp.write(chunk)
                await resp.write_eof()
                return resp

            except (asyncio.TimeoutError, TimeoutError) as e:
                if committed:
                    # Stall happened mid-stream; client has already seen
                    # bytes — no recovery.
                    state.mark_slow(
                        entry, cfg.proxy.degraded_cooldown, "mid-stream stall"
                    )
                    raise
                reason = f"ttfb>{cfg.proxy.ttfb_timeout:.0f}s"
                state.mark_slow(entry, cfg.proxy.degraded_cooldown, reason)
                attempts.append({"upstream": entry.name, "reason": reason})
            except aiohttp.ClientError as e:
                if committed:
                    state.mark_failed(
                        entry, cfg.proxy.degraded_cooldown, f"mid-stream: {e}"
                    )
                    raise
                reason = f"client_error: {e}"
                state.mark_failed(entry, cfg.proxy.degraded_cooldown, reason)
                attempts.append({"upstream": entry.name, "reason": reason})
            except Exception as e:
                if committed:
                    raise
                reason = f"unexpected: {e!r}"
                state.mark_failed(entry, cfg.proxy.degraded_cooldown, reason)
                attempts.append({"upstream": entry.name, "reason": reason})
            finally:
                if cm is not None and not committed:
                    try:
                        await cm.__aexit__(None, None, None)
                    except Exception:
                        pass
                elif cm is not None and committed:
                    try:
                        await cm.__aexit__(None, None, None)
                    except Exception:
                        pass

        return web.json_response(
            {"error": "all_upstreams_failed", "attempts": attempts},
            status=503,
        )

    # Admin routes must be registered BEFORE the catch-all so they match
    # first. Only attach admin when we have a config path to save back to.
    if config_path is not None:
        register_admin(app, cfg, state, config_path)

    app.router.add_route("*", "/{tail:.*}", handle)
    return app


async def run_proxy(
    cfg: RouterConfig,
    state: State,
    stop: asyncio.Event,
    config_path: Path | None = None,
) -> None:
    app = make_app(cfg, state, config_path=config_path)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg.proxy.listen_host, cfg.proxy.listen_port)
    await site.start()
    listen_url = f"http://{cfg.proxy.listen_host}:{cfg.proxy.listen_port}"
    state.log("info", f"proxy listening on {listen_url}")
    if config_path is not None:
        state.log("info", f"admin UI at {listen_url}/_admin")
    try:
        await stop.wait()
    finally:
        await runner.cleanup()
