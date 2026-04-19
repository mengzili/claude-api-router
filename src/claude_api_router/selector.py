from __future__ import annotations

from claude_api_router.config import ApiEntry, RouterConfig
from claude_api_router.state import State


def ordered_available(
    cfg: RouterConfig, state: State, now: float | None = None
) -> list[ApiEntry]:
    candidates = [e for e in cfg.api if state.is_available(e, now)]
    candidates.sort(key=lambda e: (e.priority, e.name))
    return candidates


def ordered_all(cfg: RouterConfig) -> list[ApiEntry]:
    return sorted(cfg.api, key=lambda e: (e.priority, e.name))
