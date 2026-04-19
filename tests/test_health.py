import time

from claude_api_router.config import ApiEntry, RouterConfig
from claude_api_router.health import preferred_probe_targets
from claude_api_router.state import State


def _cfg():
    return RouterConfig(
        api=[
            ApiEntry(name="pincc", base_url="https://p", auth_token="t", priority=1),
            ApiEntry(name="mid",   base_url="https://m", auth_token="t", priority=3),
            ApiEntry(name="poe",   base_url="https://o", auth_token="t", priority=5),
        ]
    )


def _mark_cooldown(state: State, cfg: RouterConfig, name: str, seconds: int = 120):
    entry = cfg.find(name)
    state.mark_failed(entry, seconds, "test")


def test_no_active_upstream_no_probes():
    cfg = _cfg()
    state = State()
    # active_upstream is None by default; nothing to probe.
    assert preferred_probe_targets(cfg, state) == []


def test_active_is_most_preferred_no_probes():
    cfg = _cfg()
    state = State()
    state.record_success(cfg.find("pincc"))
    # Even if mid/poe are in cooldown, active is already best.
    _mark_cooldown(state, cfg, "mid")
    _mark_cooldown(state, cfg, "poe")
    assert preferred_probe_targets(cfg, state) == []


def test_active_degraded_targets_more_preferred_in_cooldown():
    cfg = _cfg()
    state = State()
    state.record_success(cfg.find("poe"))         # priority 5
    _mark_cooldown(state, cfg, "pincc")           # priority 1, blocked
    _mark_cooldown(state, cfg, "mid")             # priority 3, blocked
    targets = [e.name for e in preferred_probe_targets(cfg, state)]
    assert targets == ["pincc", "mid"]


def test_more_preferred_already_available_not_probed():
    # pincc has no cooldown => next real request will try it naturally;
    # no need to probe.
    cfg = _cfg()
    state = State()
    state.record_success(cfg.find("poe"))
    # mid in cooldown, pincc healthy/unknown (no cooldown)
    _mark_cooldown(state, cfg, "mid")
    targets = [e.name for e in preferred_probe_targets(cfg, state)]
    assert targets == ["mid"]


def test_cooldown_expiry_removes_from_probe_list():
    cfg = _cfg()
    state = State()
    state.record_success(cfg.find("poe"))
    _mark_cooldown(state, cfg, "pincc", seconds=0)  # already expired
    # time.time() > 0-second-cooldown, so it's now available => not a target
    time.sleep(0.01)
    assert preferred_probe_targets(cfg, state) == []


def test_probe_success_clears_cooldown_via_record_health():
    # Integration-level: record_health(ok=True) must clear cooldown so
    # the entry leaves the probe list without a separate "clear" call.
    cfg = _cfg()
    state = State()
    state.record_success(cfg.find("poe"))
    _mark_cooldown(state, cfg, "pincc")
    assert [e.name for e in preferred_probe_targets(cfg, state)] == ["pincc"]

    # Simulate a successful probe.
    state.record_health(
        cfg.find("pincc"),
        ok=True,
        latency_ms=100.0,
        error=None,
        status_code=200,
        auth_failure_cooldown=1800,
    )
    assert preferred_probe_targets(cfg, state) == []
    assert state.is_available(cfg.find("pincc")) is True
