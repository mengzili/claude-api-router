from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

import aiohttp
import typer

from claude_api_router import config as config_mod
from claude_api_router import daemon as daemon_mod
from claude_api_router.config import ApiEntry, RouterConfig
from claude_api_router.health import check_all, run_health_loop
from claude_api_router.proxy import run_proxy
from claude_api_router.state import State

app = typer.Typer(
    help="Local proxy that routes Claude Code traffic across multiple "
    "Anthropic-compatible APIs with priority-based failover.",
    no_args_is_help=True,
)


def _load(config_path: Optional[Path]) -> RouterConfig:
    try:
        return config_mod.load(config_path)
    except FileNotFoundError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    except Exception as e:
        typer.secho(f"config error: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)


def _save(cfg: RouterConfig, path: Optional[Path]) -> Path:
    return config_mod.save(cfg, path)


def _listen_url(cfg: RouterConfig) -> str:
    return f"http://{cfg.proxy.listen_host}:{cfg.proxy.listen_port}"


@app.command()
def start(
    config: Optional[Path] = typer.Option(
        None, "--config", "-c", help="Path to config.toml"
    ),
    foreground: bool = typer.Option(
        False, "--foreground", "-f",
        help="Stay attached to this terminal (Ctrl+C to stop).",
    ),
    tui: bool = typer.Option(
        False, "--tui",
        help="Show the Textual dashboard (implies --foreground).",
    ),
) -> None:
    """Start the router in the background. Use `claude-api-router stop`
    to stop it, or the browser at http://<listen>/_admin to manage it.

    With --foreground (or --tui), stays attached to the current shell
    so Ctrl+C stops it."""
    config_path = config or config_mod.DEFAULT_CONFIG_PATH
    cfg = config_mod.load_or_empty(config_path)
    if not config_path.exists():
        config_mod.save(cfg, config_path)

    if tui:
        foreground = True

    # Already running? Refuse the second start.
    existing = daemon_mod.running_pid()
    if existing is not None:
        typer.secho(
            f"router already running (pid {existing}) at {_listen_url(cfg)}/_admin\n"
            f"use `claude-api-router stop` first.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(1)

    if not foreground:
        # Spawn the same CLI with --foreground so it actually runs the
        # proxy, and return to the shell.
        child_argv = [
            sys.executable, "-m", "claude_api_router", "start",
            "--foreground", "--config", str(config_path),
        ]
        pid = daemon_mod.spawn_detached(child_argv)
        ok = daemon_mod.wait_for_admin(
            f"{_listen_url(cfg)}/_admin/api/health", timeout=8.0
        )
        if ok:
            typer.echo(f"router started (pid {pid}) at {_listen_url(cfg)}/_admin")
            typer.echo(f"logs: {daemon_mod.LOG_FILE}")
            typer.echo(f"stop: claude-api-router stop")
        else:
            typer.secho(
                f"router started (pid {pid}) but admin did not respond "
                f"within 8s. Check logs at {daemon_mod.LOG_FILE}.",
                fg=typer.colors.YELLOW,
                err=True,
            )
        return

    # Foreground: we are the process that actually runs the proxy.
    if not cfg.api:
        typer.secho(
            f"No API entries yet. Open {_listen_url(cfg)}/_admin to add them.",
            fg=typer.colors.YELLOW,
        )

    daemon_mod.write_pid(os.getpid())
    try:
        asyncio.run(_run_start(cfg, config_path=config_path, show_tui=tui))
    finally:
        daemon_mod.clear_pid()


async def _run_start(
    cfg: RouterConfig, *, config_path: Path, show_tui: bool
) -> None:
    state = State()
    stop = asyncio.Event()

    def _sig_handler() -> None:
        if not stop.is_set():
            state.log("info", "shutdown signal received")
            stop.set()

    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, _sig_handler)
        loop.add_signal_handler(signal.SIGTERM, _sig_handler)
    except NotImplementedError:
        # Windows ProactorEventLoop: SIGINT surfaces as KeyboardInterrupt
        # in the outer try/except; signal-based shutdown via
        # `claude-api-router stop` goes through the admin endpoint.
        pass

    proxy_task = asyncio.create_task(
        run_proxy(cfg, state, stop, config_path=config_path), name="proxy"
    )
    health_task = asyncio.create_task(run_health_loop(cfg, state, stop), name="health")
    tasks = [proxy_task, health_task]

    if show_tui:
        from claude_api_router.tui import run_tui
        tui_task = asyncio.create_task(run_tui(cfg, state, stop), name="tui")
        tasks.append(tui_task)
    else:
        listen = _listen_url(cfg)
        typer.echo(f"proxy: {listen}")
        typer.echo(f"admin: {listen}/_admin")

    try:
        await stop.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        stop.set()
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@app.command("stop")
def cmd_stop(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    timeout: float = typer.Option(
        10.0, "--timeout", help="Seconds to wait for graceful shutdown."
    ),
) -> None:
    """Stop a running router. Asks it to shut down via the admin
    endpoint; falls back to a signal/terminate if the endpoint is
    unreachable."""
    cfg = config_mod.load_or_empty(config)
    url = _listen_url(cfg)
    pid = daemon_mod.running_pid()
    if pid is None:
        typer.secho("router is not running", fg=typer.colors.YELLOW)
        raise typer.Exit(1)

    graceful = False
    try:
        req = urllib.request.Request(
            f"{url}/_admin/api/shutdown", method="POST"
        )
        with urllib.request.urlopen(req, timeout=3) as r:
            if r.status in (200, 202):
                graceful = True
    except urllib.error.URLError as e:
        typer.secho(
            f"admin at {url} unreachable ({e}); falling back to signal",
            fg=typer.colors.YELLOW,
            err=True,
        )

    if graceful:
        # Wait for the process to actually exit.
        import time as _t
        deadline = _t.time() + timeout
        while _t.time() < deadline:
            if not daemon_mod.pid_alive(pid):
                daemon_mod.clear_pid()
                typer.echo(f"stopped (pid {pid})")
                return
            _t.sleep(0.15)
        typer.secho(
            f"graceful shutdown requested but pid {pid} still alive after {timeout:.0f}s",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(2)

    # Fallback: terminate the process.
    try:
        if sys.platform == "win32":
            import ctypes
            PROCESS_TERMINATE = 0x0001
            kernel32 = ctypes.windll.kernel32
            h = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
            if not h:
                raise OSError(f"OpenProcess({pid}) failed")
            try:
                if not kernel32.TerminateProcess(h, 1):
                    raise OSError(f"TerminateProcess({pid}) failed")
            finally:
                kernel32.CloseHandle(h)
        else:
            os.kill(pid, signal.SIGTERM)
        daemon_mod.clear_pid()
        typer.echo(f"terminated (pid {pid})")
    except OSError as ex:
        typer.secho(f"terminate failed: {ex}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)


@app.command("status")
def cmd_status(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
) -> None:
    """Show whether the router is running, and where."""
    cfg = config_mod.load_or_empty(config)
    url = _listen_url(cfg)
    pid = daemon_mod.running_pid()
    if pid is None:
        typer.echo("stopped")
        raise typer.Exit(1)
    try:
        with urllib.request.urlopen(f"{url}/_admin/api/health", timeout=2) as r:
            info = json.loads(r.read().decode("utf-8"))
        active = info.get("active_upstream") or "-"
        typer.echo(f"running (pid {pid}) at {url}/_admin  active: {active}")
    except urllib.error.URLError:
        typer.echo(f"running (pid {pid}) but admin at {url} is not yet reachable")


@app.command("add")
def cmd_add(
    name: str = typer.Option(..., "--name", "-n"),
    base_url: str = typer.Option(..., "--base-url", "-u"),
    api_key: Optional[str] = typer.Option(None, "--api-key"),
    auth_token: Optional[str] = typer.Option(None, "--auth-token"),
    priority: int = typer.Option(10, "--priority", "-p"),
    health_model: Optional[str] = typer.Option(None, "--health-model"),
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
) -> None:
    """Add a new API entry to the config.

    If a router is already running, edits via the Web UI hot-reload;
    this CLI path only writes the file — restart to pick it up.
    """
    cfg = config_mod.load_or_empty(config)
    if cfg.find(name) is not None:
        typer.secho(f"entry '{name}' already exists", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    try:
        entry = ApiEntry(
            name=name,
            base_url=base_url,
            api_key=api_key,
            auth_token=auth_token,
            priority=priority,
            health_check_model=health_model,
        )
    except Exception as e:
        typer.secho(f"invalid entry: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    cfg.api.append(entry)
    path = _save(cfg, config)
    typer.echo(f"added '{name}' (priority {priority}) -> {path}")
    if daemon_mod.running_pid() is not None:
        typer.secho(
            "note: router is running; restart it to pick up this change "
            "(or use the Web UI which hot-reloads).",
            fg=typer.colors.YELLOW,
        )


@app.command("remove")
def cmd_remove(
    name: str = typer.Argument(...),
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
) -> None:
    """Remove an API entry from the config."""
    cfg = _load(config)
    before = len(cfg.api)
    cfg.api = [e for e in cfg.api if e.name != name]
    if len(cfg.api) == before:
        typer.secho(f"no entry named '{name}'", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    _save(cfg, config)
    typer.echo(f"removed '{name}'")
    if daemon_mod.running_pid() is not None:
        typer.secho(
            "note: router is running; restart it to pick up this change.",
            fg=typer.colors.YELLOW,
        )


@app.command("list")
def cmd_list(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
) -> None:
    """List configured API entries."""
    cfg = _load(config)
    if not cfg.api:
        typer.echo("(no entries)")
        return
    for entry in sorted(cfg.api, key=lambda e: (e.priority, e.name)):
        cred = "api_key" if entry.api_key else "auth_token"
        typer.echo(
            f"  [{entry.priority:>3}] {entry.name:<24} {entry.base_url}  ({cred})"
        )


@app.command("test")
def cmd_test(
    name: Optional[str] = typer.Argument(None),
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
) -> None:
    """Send a health-check ping to one or all entries and print results."""
    cfg = _load(config)
    entries = [cfg.find(name)] if name else list(cfg.api)
    if name and entries == [None]:
        typer.secho(f"no entry named '{name}'", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    async def _go() -> None:
        state = State()
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            await check_all(session, cfg, state)
        for entry in entries:
            if entry is None:
                continue
            h = state.health[entry.name]
            lat = f"{h.last_latency_ms:.0f}ms" if h.last_latency_ms else "-"
            err = f"  ({h.last_error})" if h.last_error else ""
            typer.echo(
                f"  {entry.name:<24} {h.status:<12} {lat}{err}"
            )

    asyncio.run(_go())


if __name__ == "__main__":
    app()
