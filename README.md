# claude-api-router

Local proxy that lets Claude Code use a **priority-ordered pool** of
Anthropic-compatible APIs with automatic health checking and mid-flight
failover when first-byte latency exceeds 20 s.

```
Claude Code ──► router (127.0.0.1:8787) ──► primary upstream
                         │                       │ (if TTFB > 20s
                         │                       ▼  or connect fail)
                         │                  fallback upstream
                         │
                         ├─ periodic health pings (minimal /v1/messages)
                         └─ live TUI dashboard
```

## Install

```bash
python -m venv .venv
.venv/Scripts/activate         # or: source .venv/bin/activate
pip install -e .
```

Python 3.11+ required (uses `tomllib` and `asyncio.timeout`).

## Configure

Three ways — pick whichever.

**Web admin** (recommended). Start the proxy, then open the admin page
in any browser:

```bash
claude-api-router start          # starts proxy on 127.0.0.1:8787
# open http://127.0.0.1:8787/_admin
```

The admin shows every entry in one table. Edit cells inline, toggle
api_key/auth_token per row, press **Test** to ping a single endpoint,
**Save** to atomically write the TOML and hot-reload. You can start with
zero entries and populate them entirely from the browser.

**Via CLI** (writes `~/.claude-api-router/config.toml`):

```bash
claude-api-router add --name anthropic --base-url https://api.anthropic.com \
    --api-key sk-ant-... --priority 1
claude-api-router add --name gateway   --base-url https://gw.example.com \
    --auth-token ...         --priority 2
claude-api-router list
```

**Or hand-edit** `~/.claude-api-router/config.toml`:

```toml
[proxy]
listen_host = "127.0.0.1"
listen_port = 8787
ttfb_timeout = 20
health_check_interval = 60
degraded_cooldown = 300
health_check_model = "claude-haiku-4-5-20251001"

[[api]]
name     = "anthropic"
base_url = "https://api.anthropic.com"
api_key  = "sk-ant-..."
priority = 1

[[api]]
name       = "gateway"
base_url   = "https://gw.example.com"
auth_token = "..."
priority   = 2
```

Each entry needs **exactly one** of `api_key` (sent as `x-api-key`) or
`auth_token` (sent as `Authorization: Bearer …`). Lower `priority` =
preferred.

## Run

```bash
claude-api-router start
```

Point Claude Code at the proxy:

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
export ANTHROPIC_API_KEY=placeholder      # router overrides with the real key
claude
```

The TUI shows each upstream's health, latency, cooldown, and which one is
currently active. Keybindings:

- `r` — clear cooldowns so the next health tick retries everything
- `p` — pause the health-check loop
- `q` — quit (stops the proxy too)

Use `--tui` to attach the Textual dashboard instead of the default
log-only output. Ctrl+C exits.

## How it decides

1. On each request, iterate upstreams in priority order (skipping any in
   cooldown).
2. Send the request to the first candidate. Wait up to `ttfb_timeout`
   seconds for the first response byte.
3. If the first byte arrives: start streaming it back to Claude Code and
   commit — no further failover is possible once bytes have been sent.
4. If the upstream times out, returns 5xx, or errors before the first
   byte: mark it degraded (cooldown `degraded_cooldown`s) and try the
   next candidate.
5. 401/403 responses trigger a longer cooldown (`auth_failure_cooldown`,
   default 30 min) since auth errors won't self-heal.
6. Health pings are **upgrade probes only**: the background loop pings
   an upstream **only if** it's more preferred than the currently active
   one AND currently in cooldown. No Claude Code traffic → no active
   upstream → no pings (zero tokens while idle). When pincc (priority 1)
   recovers from a cooldown, the next real request automatically gets
   promoted to it because the selector picks lowest priority available.

### Known approximation

"First-byte" is used as the latency signal, not "first content-block-
delta token." For SSE streams the first byte is usually `event:
message_start` arriving well before actual text. If an upstream stalls
between headers and the first token, the 20 s watchdog fires after the
first body chunk is seen. This is simpler than parsing SSE and still
catches fully-stalled upstreams. Swap in an SSE-aware watchdog if that
matters.

## Commands

```
claude-api-router start [--tui] [--config PATH]
claude-api-router add --name N --base-url U (--api-key K | --auth-token T) [--priority P]
claude-api-router remove NAME
claude-api-router list
claude-api-router test [NAME]      # one-shot health check, print results
```

## Manual end-to-end smoke test

1. `claude-api-router add --name primary --base-url https://api.anthropic.com --api-key sk-ant-... --priority 1`
2. `claude-api-router add --name slow    --base-url https://httpbin.org     --auth-token x        --priority 0`
   (httpbin's `/v1/messages` path will 404, so `primary` wins fallback)
3. `claude-api-router start`
4. In another shell, `ANTHROPIC_BASE_URL=http://127.0.0.1:8787 ANTHROPIC_API_KEY=anything claude`
5. Send a prompt, verify streaming works.
6. To force a TTFB failover, add a `--base-url` pointing at a deliberately
   slow endpoint (`https://httpbin.org/delay/30`) at priority 0 and watch
   the TUI log the switch around the 20 s mark.

## Development

```bash
pip install -e '.[dev]'
pytest
```

Tests spin up real aiohttp fake upstreams to exercise the streaming + failover
paths. They complete in a few seconds on a short `ttfb_timeout`.

## Out of scope (v1)

- Multi-user / remote proxy
- Rate-limit or token-budget awareness
- TLS on the listener (Claude Code ↔ localhost is plaintext)
- Persisted metrics / event history
