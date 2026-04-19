import time

from claude_api_router.config import ApiEntry, RouterConfig
from claude_api_router.selector import ordered_all, ordered_available
from claude_api_router.state import State


def _cfg():
    return RouterConfig(
        api=[
            ApiEntry(name="a", base_url="https://a", api_key="k", priority=2),
            ApiEntry(name="b", base_url="https://b", api_key="k", priority=1),
            ApiEntry(name="c", base_url="https://c", api_key="k", priority=3),
        ]
    )


def test_ordered_all_by_priority():
    names = [e.name for e in ordered_all(_cfg())]
    assert names == ["b", "a", "c"]


def test_ordered_available_excludes_cooldowns():
    cfg = _cfg()
    state = State()
    for e in cfg.api:
        state.ensure(e)
    state.health["b"].status = "failed"
    state.health["b"].cooldown_until = time.time() + 60
    names = [e.name for e in ordered_available(cfg, state)]
    assert names == ["a", "c"]


def test_ordered_available_includes_expired_cooldowns():
    cfg = _cfg()
    state = State()
    for e in cfg.api:
        state.ensure(e)
    state.health["b"].status = "failed"
    state.health["b"].cooldown_until = time.time() - 1  # past
    names = [e.name for e in ordered_available(cfg, state)]
    assert names == ["b", "a", "c"]


def test_ordered_available_all_fail():
    cfg = _cfg()
    state = State()
    for e in cfg.api:
        state.ensure(e)
        state.health[e.name].status = "failed"
        state.health[e.name].cooldown_until = time.time() + 60
    assert ordered_available(cfg, state) == []


def test_unknown_entries_included():
    cfg = _cfg()
    state = State()
    # no health records -> is_available returns True for unknown
    names = [e.name for e in ordered_available(cfg, state)]
    assert names == ["b", "a", "c"]
