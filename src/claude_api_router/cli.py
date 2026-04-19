from __future__ import annotations

import asyncio
import signal
from pathlib import Path
from typing import Optional

import aiohttp
import typer

from claude_api_router import config as config_mod
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
    tui: bool = typer.Option(
        False, "--tui", help="Show the Textual dashboard in this terminal."
    ),
) -> None:
    """Start the proxy in the foreground. Manage config via the Web UI
    at http://<listen>/_admin. Ctrl+C to stop."""
    config_path = config or config_mod.DEFAULT_CONFIG_PATH
    cfg = config_mod.load_or_empty(config_path)
    if not config_path.exists():
        config_mod.save(cfg, config_path)
    if not cfg.api:
        typer.secho(
            f"No API entries yet. Open {_listen_url(cfg)}/_admin to add them.",
            fg=typer.colors.YELLOW,
        )
    asyncio.run(_run_start(cfg, config_path=config_path, show_tui=tui))


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
        # Windows ProactorEventLoop: SIGINT still surfaces as
        # KeyboardInterrupt below, which is enough.
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
        typer.echo(f"admin: {listen}/_admin  (Ctrl+C to stop)")

    try:
        await stop.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        stop.set()
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


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
    """Add a new API entry to the config."""
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
