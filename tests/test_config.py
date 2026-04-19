from pathlib import Path

import pytest

from claude_api_router import config as cfg_mod
from claude_api_router.config import ApiEntry, ProxyConfig, RouterConfig


def test_api_entry_requires_exactly_one_credential():
    with pytest.raises(Exception):
        ApiEntry(name="x", base_url="https://a")
    with pytest.raises(Exception):
        ApiEntry(name="x", base_url="https://a", api_key="k", auth_token="t")


def test_api_entry_strips_trailing_slash():
    e = ApiEntry(name="x", base_url="https://a.com/", api_key="k")
    assert e.base_url == "https://a.com"


def test_api_entry_auth_headers():
    e1 = ApiEntry(name="x", base_url="https://a", api_key="k1")
    assert e1.auth_headers()["x-api-key"] == "k1"
    assert "Authorization" not in e1.auth_headers()
    e2 = ApiEntry(name="y", base_url="https://b", auth_token="t2")
    assert e2.auth_headers()["Authorization"] == "Bearer t2"
    assert "x-api-key" not in e2.auth_headers()


def test_router_config_rejects_duplicate_names():
    with pytest.raises(Exception):
        RouterConfig(
            api=[
                ApiEntry(name="a", base_url="https://1", api_key="k"),
                ApiEntry(name="a", base_url="https://2", api_key="k"),
            ]
        )


def test_config_roundtrip(tmp_path: Path):
    path = tmp_path / "config.toml"
    original = RouterConfig(
        proxy=ProxyConfig(listen_port=9000, ttfb_timeout=15.0),
        api=[
            ApiEntry(name="primary", base_url="https://a.com", api_key="k1", priority=1),
            ApiEntry(
                name="fallback",
                base_url="https://b.com",
                auth_token="t2",
                priority=2,
                health_check_model="claude-sonnet-4-6",
            ),
        ],
    )
    cfg_mod.save(original, path)
    loaded = cfg_mod.load(path)
    assert loaded.proxy.listen_port == 9000
    assert loaded.proxy.ttfb_timeout == 15.0
    assert len(loaded.api) == 2
    assert loaded.find("primary").api_key == "k1"
    assert loaded.find("fallback").auth_token == "t2"
    assert loaded.find("fallback").health_check_model == "claude-sonnet-4-6"


def test_load_missing_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        cfg_mod.load(tmp_path / "nope.toml")


def test_load_or_empty_missing_returns_empty(tmp_path: Path):
    cfg = cfg_mod.load_or_empty(tmp_path / "nope.toml")
    assert cfg.api == []
