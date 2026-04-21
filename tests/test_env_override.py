"""Per-entry env-override body rewrites."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_api_router import config as config_mod
from claude_api_router.config import ApiEntry, RouterConfig, apply_env_body_overrides


def test_no_env_is_noop():
    body = b'{"model":"claude-opus-4-7","max_tokens":1}'
    assert apply_env_body_overrides(body, None) is body
    assert apply_env_body_overrides(body, {}) is body


def test_opus_family_rewritten_by_family_key():
    body = b'{"model":"claude-opus-4-7","max_tokens":1}'
    out = apply_env_body_overrides(body, {"ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4-7-cc"})
    assert json.loads(out)["model"] == "claude-opus-4-7-cc"


def test_sonnet_family_untouched_by_opus_key():
    body = b'{"model":"claude-sonnet-4-6","max_tokens":1}'
    out = apply_env_body_overrides(body, {"ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4-7-cc"})
    # Sonnet body must be left alone — the opus key doesn't match this family.
    assert json.loads(out)["model"] == "claude-sonnet-4-6"


def test_anthropic_model_wins_when_family_key_absent():
    body = b'{"model":"claude-haiku-4-5-20251001"}'
    out = apply_env_body_overrides(body, {"ANTHROPIC_MODEL": "custom-haiku"})
    assert json.loads(out)["model"] == "custom-haiku"


def test_family_key_beats_anthropic_model():
    body = b'{"model":"claude-opus-4-7"}'
    out = apply_env_body_overrides(body, {
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "opus-rename",
        "ANTHROPIC_MODEL": "global-rename",
    })
    assert json.loads(out)["model"] == "opus-rename"


def test_unknown_keys_ignored():
    body = b'{"model":"claude-opus-4-7"}'
    out = apply_env_body_overrides(body, {"SOMETHING_ELSE": "ignored"})
    assert json.loads(out)["model"] == "claude-opus-4-7"


def test_invalid_json_body_returned_verbatim():
    body = b"not json at all"
    out = apply_env_body_overrides(body, {"ANTHROPIC_DEFAULT_OPUS_MODEL": "x"})
    assert out is body


def test_body_without_model_field_unchanged():
    body = b'{"max_tokens":1,"messages":[]}'
    out = apply_env_body_overrides(body, {"ANTHROPIC_DEFAULT_OPUS_MODEL": "x"})
    assert out is body


def test_same_value_returns_original_bytes():
    body = b'{"model":"claude-opus-4-7"}'
    out = apply_env_body_overrides(body, {"ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4-7"})
    # Override equals current — nothing to do, return original.
    assert out is body


def test_env_roundtrips_through_toml(tmp_path: Path):
    cfg = RouterConfig(api=[
        ApiEntry(
            name="autodl",
            base_url="https://www.autodl.art/api/v1/anthropic",
            auth_token="tok",
            priority=4,
            env={"ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4-7-cc"},
        ),
    ])
    path = tmp_path / "config.toml"
    config_mod.save(cfg, path)
    reloaded = config_mod.load(path)
    e = reloaded.find("autodl")
    assert e is not None
    assert e.env == {"ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4-7-cc"}


def test_empty_env_dict_dropped_from_toml(tmp_path: Path):
    cfg = RouterConfig(api=[
        ApiEntry(name="a", base_url="https://a", auth_token="t", priority=1, env={}),
    ])
    path = tmp_path / "config.toml"
    config_mod.save(cfg, path)
    # Reload and verify — empty env should round-trip as None-ish, not {}.
    reloaded = config_mod.load(path)
    assert reloaded.find("a").env is None or reloaded.find("a").env == {}
