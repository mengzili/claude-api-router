from __future__ import annotations

import json
import os
import sys
import tomllib
from pathlib import Path
from typing import Literal

import tomli_w
from pydantic import BaseModel, Field, field_validator, model_validator


DEFAULT_CONFIG_DIR = Path.home() / ".claude-api-router"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.toml"
DEFAULT_HEALTH_MODEL = "claude-opus-4-6"


class ApiEntry(BaseModel):
    name: str
    base_url: str
    api_key: str | None = None
    auth_token: str | None = None
    priority: int = 10
    health_check_model: str | None = None
    # Per-entry Claude-Code-style environment overrides applied to the
    # outbound request body. Supported keys match the env vars Claude Code
    # itself reads: ANTHROPIC_DEFAULT_OPUS_MODEL, ANTHROPIC_DEFAULT_SONNET_MODEL,
    # ANTHROPIC_DEFAULT_HAIKU_MODEL, ANTHROPIC_MODEL. Needed for gateways
    # that serve under non-canonical model names (e.g. autodl's
    # "claude-opus-4-7-cc").
    env: dict[str, str] | None = None

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("name")
    @classmethod
    def _name_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must be non-empty")
        return v.strip()

    @model_validator(mode="after")
    def _exactly_one_credential(self) -> "ApiEntry":
        has_key = bool(self.api_key)
        has_token = bool(self.auth_token)
        if has_key == has_token:
            raise ValueError(
                f"entry '{self.name}': set exactly one of api_key or auth_token"
            )
        return self

    def auth_headers(self) -> dict[str, str]:
        headers = {"anthropic-version": "2023-06-01"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        else:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        return headers

    def health_model(self, default: str) -> str:
        return self.health_check_model or default


class ProxyConfig(BaseModel):
    listen_host: str = "127.0.0.1"
    listen_port: int = 8787
    health_check_interval: float = 60.0
    ttfb_timeout: float = 20.0
    degraded_cooldown: float = 300.0
    auth_failure_cooldown: float = 1800.0
    health_check_model: str = DEFAULT_HEALTH_MODEL
    max_buffer_bytes: int = 25 * 1024 * 1024


class RouterConfig(BaseModel):
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    api: list[ApiEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_names(self) -> "RouterConfig":
        seen: set[str] = set()
        for entry in self.api:
            if entry.name in seen:
                raise ValueError(f"duplicate api entry name: {entry.name}")
            seen.add(entry.name)
        return self

    def find(self, name: str) -> ApiEntry | None:
        for entry in self.api:
            if entry.name == name:
                return entry
        return None


def load(path: Path | None = None) -> RouterConfig:
    path = path or DEFAULT_CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"config not found at {path}. Run `claude-api-router add ...` first."
        )
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    return RouterConfig.model_validate(data)


def save(cfg: RouterConfig, path: Path | None = None) -> Path:
    path = path or DEFAULT_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict = {
        "proxy": cfg.proxy.model_dump(),
        "api": [
            {
                k: v for k, v in entry.model_dump().items()
                # Drop None and empty {} so the TOML stays clean.
                if v is not None and v != {}
            }
            for entry in cfg.api
        ],
    }
    with open(path, "wb") as fh:
        tomli_w.dump(payload, fh)
    try:
        if os.name == "posix":
            os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def load_or_empty(path: Path | None = None) -> RouterConfig:
    path = path or DEFAULT_CONFIG_PATH
    if not path.exists():
        return RouterConfig()
    return load(path)


# ---------------------------------------------------------------------------
# Per-entry env overrides applied to JSON request bodies before forwarding to
# upstream. Mirrors the subset of Claude-Code env vars that affect which model
# id gets sent on the wire.
# ---------------------------------------------------------------------------

_OPUS_KEY   = "ANTHROPIC_DEFAULT_OPUS_MODEL"
_SONNET_KEY = "ANTHROPIC_DEFAULT_SONNET_MODEL"
_HAIKU_KEY  = "ANTHROPIC_DEFAULT_HAIKU_MODEL"
_MODEL_KEY  = "ANTHROPIC_MODEL"


def _resolve_model_override(env: dict[str, str], current: str) -> str | None:
    """Pick the override appropriate to the model family we're sending."""
    low = current.lower()
    if "opus" in low:
        return env.get(_OPUS_KEY) or env.get(_MODEL_KEY)
    if "sonnet" in low:
        return env.get(_SONNET_KEY) or env.get(_MODEL_KEY)
    if "haiku" in low:
        return env.get(_HAIKU_KEY) or env.get(_MODEL_KEY)
    return env.get(_MODEL_KEY)


def apply_env_body_overrides(body: bytes, env: dict[str, str] | None) -> bytes:
    """Rewrite the `model` field of a JSON request body per per-entry env.

    No-op when `env` is empty/None, when the body isn't JSON, or when the
    parsed body doesn't have a string `model` field. Never raises — if
    anything goes wrong, returns the body unchanged so the proxy keeps
    working.
    """
    if not env:
        return body
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return body
    if not isinstance(data, dict):
        return body
    current = data.get("model")
    if not isinstance(current, str):
        return body
    override = _resolve_model_override(env, current)
    if not override or override == current:
        return body
    data["model"] = override
    try:
        return json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        return body
