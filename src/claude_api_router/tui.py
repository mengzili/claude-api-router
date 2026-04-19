from __future__ import annotations

import asyncio
import time
from datetime import datetime

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import DataTable, Footer, Header, RichLog, Static

from claude_api_router import selector
from claude_api_router.config import RouterConfig
from claude_api_router.state import State


class RouterTUI(App):
    CSS = """
    Screen { layout: vertical; }
    #status-bar { height: 1; padding: 0 1; color: $accent; }
    DataTable { height: 40%; }
    RichLog { height: 1fr; border: solid $primary; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh_now", "Refresh"),
        Binding("p", "toggle_pause", "Pause health"),
    ]

    def __init__(self, cfg: RouterConfig, state: State, stop: asyncio.Event):
        super().__init__()
        self.cfg = cfg
        self.state = state
        self.stop_event = stop
        self._last_event_count = 0
        self._refresh_health_event: asyncio.Event | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="status-bar")
        yield DataTable(id="upstreams", cursor_type="row")
        yield RichLog(id="events", highlight=True, markup=True, wrap=False)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#upstreams", DataTable)
        table.add_columns("priority", "name", "status", "latency", "cooldown", "active")
        self.set_interval(0.5, self._tick)
        self._tick()

    def _status_cell(self, status: str) -> str:
        color = {
            "healthy": "green",
            "unknown": "dim",
            "slow": "yellow",
            "failed": "red",
            "auth_error": "red",
        }.get(status, "white")
        return f"[{color}]{status}[/{color}]"

    def _tick(self) -> None:
        table = self.query_one("#upstreams", DataTable)
        now = time.time()
        ordered = selector.ordered_all(self.cfg)

        table.clear()
        for entry in ordered:
            h = self.state.health.get(entry.name)
            status = h.status if h else "unknown"
            lat = (
                f"{h.last_latency_ms:.0f}ms"
                if h and h.last_latency_ms is not None
                else "-"
            )
            if h and h.cooldown_until > now:
                cd = f"{int(h.cooldown_until - now)}s"
            else:
                cd = "-"
            active = "*" if self.state.active_upstream == entry.name else ""
            table.add_row(
                str(entry.priority),
                entry.name,
                self._status_cell(status),
                lat,
                cd,
                active,
                key=entry.name,
            )

        # Status bar
        bar = self.query_one("#status-bar", Static)
        paused = " [PAUSED]" if self.state.health_paused else ""
        bar.update(
            f"listening on http://{self.cfg.proxy.listen_host}:"
            f"{self.cfg.proxy.listen_port} -> active: "
            f"{self.state.active_upstream or '-'}{paused}"
        )

        # Drain new events into the RichLog
        log = self.query_one("#events", RichLog)
        events = list(self.state.events)
        new = events[self._last_event_count :]
        for ev in new:
            ts = datetime.fromtimestamp(ev.at).strftime("%H:%M:%S")
            color = {
                "switch": "cyan",
                "fail": "red",
                "health": "green",
                "info": "white",
            }.get(ev.kind, "white")
            log.write(f"[dim]{ts}[/dim] [{color}]{ev.kind:<7}[/{color}] {ev.message}")
        self._last_event_count = len(events)

    def action_toggle_pause(self) -> None:
        self.state.health_paused = not self.state.health_paused
        self.state.log(
            "info",
            f"health checks {'paused' if self.state.health_paused else 'resumed'}",
        )

    def action_refresh_now(self) -> None:
        # Nudges: clear cooldowns on non-auth failures and let the health
        # loop re-check on its next tick. For a truly instant recheck the
        # user can also just wait briefly.
        now = time.time()
        for h in self.state.health.values():
            if h.status != "auth_error":
                h.cooldown_until = min(h.cooldown_until, now + 1)
        self.state.log("info", "manual refresh requested (cooldowns cleared)")

    async def action_quit(self) -> None:
        self.state.log("info", "TUI quit requested")
        self.stop_event.set()
        self.exit()


async def run_tui(cfg: RouterConfig, state: State, stop: asyncio.Event) -> None:
    tui = RouterTUI(cfg, state, stop)
    try:
        await tui.run_async()
    finally:
        if not stop.is_set():
            stop.set()
