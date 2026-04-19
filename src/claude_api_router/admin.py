from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from claude_api_router import config as config_mod
from claude_api_router.config import ApiEntry, ProxyConfig, RouterConfig
from claude_api_router.health import ping
from claude_api_router.state import State


# Fields that require restarting the proxy for a change to take effect.
# (The HTTP listener and request-body buffer cap are set at bind-time.)
RESTART_REQUIRED_FIELDS = {"listen_host", "listen_port", "max_buffer_bytes"}


def _bucket_series(
    request_log: dict,
    *,
    bucket_sec: int,
    window_sec: int,
    now: float,
) -> tuple[list[int], dict[str, list[int]]]:
    """Bucket per-upstream request timestamps into fixed-size windows.

    Returns (buckets, series) where `buckets` is a list of bucket-start
    epoch seconds in ascending order and `series[name]` is the count in
    each bucket."""
    if bucket_sec <= 0:
        bucket_sec = 600
    # Round `now` down to bucket boundary, then step back `n` buckets.
    end_bucket = int(now // bucket_sec) * bucket_sec
    n_buckets = max(1, window_sec // bucket_sec)
    start_bucket = end_bucket - (n_buckets - 1) * bucket_sec
    buckets = [start_bucket + i * bucket_sec for i in range(n_buckets)]
    bucket_index = {b: i for i, b in enumerate(buckets)}
    earliest = buckets[0]

    series: dict[str, list[int]] = {}
    for name, stamps in request_log.items():
        counts = [0] * n_buckets
        for ts in stamps:
            if ts < earliest:
                continue
            b = int(ts // bucket_sec) * bucket_sec
            idx = bucket_index.get(b)
            if idx is not None:
                counts[idx] += 1
        if any(counts):
            series[name] = counts
    return buckets, series


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>claude-api-router</title>
<style>
  :root {
    --bg: #0f1115; --panel: #171a21; --border: #2a2f3a;
    --text: #e6e8ec; --muted: #8a92a5; --accent: #4f8cff; --danger: #ef4444;
    --ok: #22c55e; --warn: #f59e0b; --fail: #ef4444;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: var(--bg); color: var(--text);
               font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }

  header {
    position: sticky; top: 0; z-index: 20;
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
    padding: 10px 20px; border-bottom: 1px solid var(--border);
    background: var(--bg);
  }
  header h1 { font-size: 15px; margin: 0; font-weight: 600; }
  header .status { color: var(--muted); font-size: 12px; margin-right: auto; }
  header .status b { color: var(--text); font-weight: 500; }
  header .dirty { color: var(--warn); font-size: 12px; display: none; }
  header .dirty.on { display: inline-flex; align-items: center; }
  header .dirty::before { content: ""; display: inline-block; width: 8px; height: 8px;
                          border-radius: 50%; background: var(--warn); margin-right: 6px; }

  main { max-width: 1200px; margin: 0 auto; padding: 20px; }

  button {
    background: var(--panel); color: var(--text);
    border: 1px solid var(--border); border-radius: 6px;
    padding: 6px 12px; cursor: pointer; font: inherit; white-space: nowrap;
  }
  button:hover:not(:disabled) { border-color: var(--accent); }
  button:disabled { opacity: 0.4; cursor: not-allowed; }
  button.primary { background: var(--accent); border-color: var(--accent); color: white; }
  button.primary:disabled { background: #2a3a5a; border-color: #2a3a5a; color: #7a8aa0; }
  button.danger { border-color: #7a2a2a; color: var(--danger); background: transparent; }
  button.danger:hover:not(:disabled) { background: #2a1416; border-color: var(--danger); }

  details {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 8px; margin-bottom: 12px;
  }
  details > summary {
    padding: 10px 14px; cursor: pointer; user-select: none;
    font-weight: 500; list-style: none;
  }
  details > summary::-webkit-details-marker { display: none; }
  details > summary::before { content: "▸ "; color: var(--muted); }
  details[open] > summary::before { content: "▾ "; }
  details > summary .sub { color: var(--muted); font-weight: 400; margin-left: 8px; font-size: 12px; }
  .panel-body { padding: 0 14px 14px; border-top: 1px solid var(--border); padding-top: 14px; }

  pre {
    background: #0c0e13; border: 1px solid var(--border); border-radius: 6px;
    padding: 10px 12px; margin: 6px 0; overflow-x: auto;
    font: 12.5px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; color: #d6d9e0;
    position: relative;
  }
  pre .copy {
    position: absolute; top: 6px; right: 6px;
    padding: 2px 8px; font-size: 11px;
  }

  table {
    width: 100%; border-collapse: separate; border-spacing: 0;
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 8px; overflow: hidden;
  }
  th, td {
    padding: 8px 10px; border-bottom: 1px solid var(--border);
    text-align: left; vertical-align: middle;
  }
  th { font-size: 12px; color: var(--muted); font-weight: 500; background: #0c0e13; }
  tr:last-child td { border-bottom: none; }
  tbody tr { background: var(--panel); }

  input, select {
    background: #0c0e13; border: 1px solid var(--border); color: var(--text);
    border-radius: 4px; padding: 5px 8px; font: inherit; width: 100%;
  }
  input:focus, select:focus { outline: none; border-color: var(--accent); }
  input.priority { width: 60px; text-align: center; }
  .secret-cell { display: flex; gap: 4px; align-items: center; }
  .secret-cell input { flex: 1; }
  .secret-cell button { padding: 4px 8px; }

  .status-pill { display: inline-block; padding: 2px 8px; border-radius: 999px;
                 font-size: 11px; font-weight: 500; }
  .status-healthy    { background: #0d3a20; color: var(--ok); }
  .status-slow       { background: #3a2d08; color: var(--warn); }
  .status-failed     { background: #3a1414; color: var(--fail); }
  .status-auth_error { background: #3a1414; color: var(--fail); }
  .status-unknown    { background: #1e232e; color: var(--muted); }
  .lat { font-variant-numeric: tabular-nums; color: var(--muted); font-size: 12px; margin-left: 4px; }

  .toast { position: fixed; bottom: 20px; right: 20px; padding: 10px 14px;
           border-radius: 6px; font-size: 13px; z-index: 100;
           animation: in 0.15s ease-out; max-width: 420px; }
  @keyframes in { from { opacity: 0; transform: translateY(8px); } }
  .toast.ok   { background: #0d3a20; color: var(--ok);   border: 1px solid #22c55e44; }
  .toast.warn { background: #3a2d08; color: var(--warn); border: 1px solid #f59e0b44; }
  .toast.err  { background: #3a1414; color: var(--fail); border: 1px solid #ef444444; }

  .muted { color: var(--muted); }
  .empty { text-align: center; padding: 40px; color: var(--muted);
           background: var(--panel); border: 1px dashed var(--border);
           border-radius: 8px; }

  /* Settings grid */
  .settings-grid {
    display: grid; grid-template-columns: 240px 1fr auto; gap: 10px 16px;
    align-items: center;
  }
  .settings-grid label { font-size: 13px; }
  .settings-grid label code { color: var(--text); background: #0c0e13;
                               padding: 1px 5px; border-radius: 3px; font-size: 12px; }
  .settings-grid .hint { color: var(--muted); font-size: 12px; }
  .tag { display: inline-block; font-size: 10px; padding: 1px 6px; border-radius: 3px;
         background: #3a2d08; color: var(--warn); margin-left: 6px;
         letter-spacing: 0.02em; text-transform: uppercase; }
  .settings-actions { margin-top: 14px; display: flex; gap: 8px; }

  /* Traffic chart */
  .chart-head {
    display: flex; align-items: baseline; gap: 16px;
    margin-bottom: 10px; font-size: 13px;
  }
  .chart-head .window-select {
    margin-left: auto; color: var(--muted);
  }
  .chart-legend {
    display: flex; flex-wrap: wrap; gap: 14px 20px;
    margin-top: 10px; font-size: 12px;
  }
  .chart-legend .swatch {
    display: inline-block; width: 10px; height: 10px;
    border-radius: 2px; margin-right: 6px; vertical-align: baseline;
  }
  .chart-legend .name   { color: var(--text); }
  .chart-legend .count  { color: var(--muted); margin-left: 4px; }
  .chart-svg {
    display: block; width: 100%; height: 220px;
    background: #0c0e13; border: 1px solid var(--border); border-radius: 6px;
  }
  .chart-empty { color: var(--muted); padding: 22px; text-align: center; }
  .chart-wrap { position: relative; }

  /* Routing-table drag-to-reorder */
  tbody tr.drag-source { opacity: 0.5; }
  tbody tr.drag-over-top    { box-shadow: 0 -2px 0 var(--accent) inset; }
  tbody tr.drag-over-bottom { box-shadow: 0  2px 0 var(--accent) inset; }
  .drag-handle {
    width: 18px; text-align: center; cursor: grab; user-select: none;
    color: var(--muted); font-size: 14px;
  }
  .drag-handle:active { cursor: grabbing; }
  .priority-cell {
    text-align: center; color: var(--muted);
    font-variant-numeric: tabular-nums; font-size: 13px;
  }
</style>
</head>
<body>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js"></script>
<header>
  <h1>claude-api-router</h1>
  <span class="status" id="status">connecting…</span>
  <span class="dirty" id="dirty">unsaved changes</span>
  <button id="add">+ Add row</button>
  <button id="refresh">Refresh</button>
  <button id="revert">Revert</button>
  <button id="save" class="primary" disabled>Save</button>
</header>
<main>

  <details id="howto" open>
    <summary>How to point Claude Code at this router<span class="sub">environment variable setup</span></summary>
    <div class="panel-body">
      <p class="muted" style="margin-top: 0">
        Set these before launching <code>claude</code> (shell-specific syntax —
        the first block is for macOS/Linux, the second for PowerShell):
      </p>
      <pre id="envsh"><button class="copy" data-target="envsh">Copy</button></pre>
      <pre id="envps"><button class="copy" data-target="envps">Copy</button></pre>
      <p class="muted" style="margin-bottom: 0; font-size: 13px;">
        <code>ANTHROPIC_AUTH_TOKEN</code> must be non-empty so Claude Code
        starts, but its value is discarded — the router injects the
        credential configured for the currently active upstream.
        <code>ANTHROPIC_API_KEY</code> should be explicitly empty so Claude
        Code uses the <code>AUTH_TOKEN</code> path.
      </p>
    </div>
  </details>

  <details id="routing" open>
    <summary>Routing table<span class="sub">upstream APIs, by priority</span></summary>
    <div class="panel-body">
      <table id="tbl">
        <thead>
          <tr>
            <th style="width:24px"></th>
            <th style="width:44px">Pri</th>
            <th style="width:18%">Name</th>
            <th style="width:26%">Base URL</th>
            <th style="width:12%">Credential</th>
            <th style="width:22%">Secret</th>
            <th style="width:12%">Status</th>
            <th style="width:10%">Actions</th>
          </tr>
        </thead>
        <tbody id="rows"></tbody>
      </table>
      <div id="empty" class="empty" style="display:none; margin-top: 12px;">
        No entries yet. Click <b>+ Add row</b> in the toolbar to create one.
      </div>
    </div>
  </details>

  <details id="settings" open>
    <summary>Settings<span class="sub">ping interval, TTFB timeout, cooldowns, listen address</span></summary>
    <div class="panel-body">
      <div class="settings-grid" id="settings-grid">
        <!-- injected by JS -->
      </div>
      <div class="settings-actions">
        <button id="revert-settings">Revert</button>
        <button id="save-settings" class="primary">Save settings</button>
      </div>
    </div>
  </details>

  <details id="traffic" open>
    <summary>Traffic<span class="sub" id="traffic-sub">requests per upstream, hourly buckets</span></summary>
    <div class="panel-body">
      <div class="chart-head">
        <span id="traffic-total" class="muted"></span>
        <label class="window-select">
          window
          <select id="window-sel">
            <option value="86400" selected>day</option>
            <option value="604800">week</option>
          </select>
        </label>
      </div>
      <div class="chart-wrap">
        <div id="chart" class="chart-svg"></div>
      </div>
      <div id="chart-empty" class="chart-empty" style="display:none">
        No traffic recorded yet. Point Claude Code at this router and send a prompt.
      </div>
      <div id="chart-legend" class="chart-legend"></div>
    </div>
  </details>
</main>

<script>
let entries = [];
let saved   = [];
let health  = {};
let activeName = null;
let settings = {};
let savedSettings = {};
let listenUrl = "";
let chartInstance = null;

const $ = (id) => document.getElementById(id);
const rowsEl = $("rows");

// ---- Settings field metadata -----------------------------------------
// [key, label, type, hint, restart]
const SETTINGS_FIELDS = [
  ["health_check_interval", "Ping interval (s)",      "number", "Seconds between upgrade-probe cycles. Probes only fire when a more-preferred upstream is in cooldown.", false],
  ["ttfb_timeout",          "TTFB failover (s)",      "number", "If the upstream's first response byte takes longer than this, the request fails over to the next upstream.", false],
  ["degraded_cooldown",     "Failure cooldown (s)",   "number", "How long an upstream is skipped after a generic failure (TTFB, 5xx, connection error).", false],
  ["auth_failure_cooldown", "Auth cooldown (s)",      "number", "Longer cooldown for 401/403 since auth errors won't self-heal.", false],
  ["health_check_model",    "Probe model",            "text",   "Model used for the minimal /v1/messages probe (max_tokens=1). Per-entry override is also supported.", false],
  ["listen_host",           "Listen host",            "text",   "Interface to bind. 127.0.0.1 = localhost only.", true],
  ["listen_port",           "Listen port",            "number", "TCP port to bind.", true],
  ["max_buffer_bytes",      "Max request body (bytes)","number","Upper bound on request body the proxy will buffer for retry.", true],
];

// ---- Small helpers ---------------------------------------------------
function esc(s) { return String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
function fmtLat(ms) { return ms == null ? "" : `${Math.round(ms)}ms`; }

function toast(msg, kind) {
  const t = document.createElement("div");
  t.className = `toast ${kind || "ok"}`;
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 4000);
}

// ---- How-to env blocks ----------------------------------------------
function renderEnv() {
  if (!listenUrl) return;
  const sh = `export ANTHROPIC_BASE_URL=${listenUrl}
export ANTHROPIC_AUTH_TOKEN=placeholder
export ANTHROPIC_API_KEY=
claude`;
  const ps = `$env:ANTHROPIC_BASE_URL = "${listenUrl}"
$env:ANTHROPIC_AUTH_TOKEN = "placeholder"
$env:ANTHROPIC_API_KEY = ""
claude`;
  const shEl = $("envsh");
  const psEl = $("envps");
  // Preserve the Copy button already inside each <pre>.
  shEl.innerHTML = `<button class="copy" data-target="envsh">Copy</button>` + esc(sh);
  psEl.innerHTML = `<button class="copy" data-target="envps">Copy</button>` + esc(ps);
}

document.addEventListener("click", async (ev) => {
  const copy = ev.target.closest("button.copy");
  if (!copy) return;
  const pre = $(copy.dataset.target);
  if (!pre) return;
  // Take everything in the <pre> except the copy button's own text.
  const text = pre.textContent.replace(/^Copy/, "").trim();
  try {
    await navigator.clipboard.writeText(text);
    toast("Copied to clipboard.", "ok");
  } catch (e) {
    toast("Copy failed: " + e, "err");
  }
});

// ---- Upstream table --------------------------------------------------
function statusPill(name) {
  const h = health[name];
  const s = h ? h.status : "unknown";
  const lat = h && h.last_latency_ms != null ? ` <span class="lat">${fmtLat(h.last_latency_ms)}</span>` : "";
  return `<span class="status-pill status-${s}">${s}</span>${lat}`;
}

function setDirty(d) {
  $("save").disabled = !d;
  $("dirty").classList.toggle("on", d);
}

function markDirty() {
  setDirty(JSON.stringify(entries) !== JSON.stringify(saved));
}

function render() {
  rowsEl.innerHTML = "";
  // Priority is derived from the current array order — this is the
  // single source of truth. Sort once here if the data came in out of
  // order (e.g. first load), then always renumber on render so the
  // display and the config stay in sync.
  entries.sort((a, b) => (a.priority ?? 0) - (b.priority ?? 0));
  for (let i = 0; i < entries.length; i++) entries[i].priority = i + 1;

  if (entries.length === 0) {
    $("empty").style.display = "";
    return;
  }
  $("empty").style.display = "none";
  entries.forEach((e, i) => {
    const tr = document.createElement("tr");
    tr.draggable = true;
    const credType = e.api_key != null ? "api_key" : "auth_token";
    const secret = e.api_key ?? e.auth_token ?? "";
    const statusCell = (e.name ? statusPill(e.name) : '<span class="muted">(unsaved)</span>')
                     + (activeName === e.name ? ' <span title="currently active">*</span>' : '');
    tr.innerHTML = `
      <td class="drag-handle" title="Drag to reorder">⋮⋮</td>
      <td class="priority-cell">${e.priority}</td>
      <td><input data-k="name" value="${esc(e.name ?? "")}" placeholder="unique name"></td>
      <td><input data-k="base_url" value="${esc(e.base_url ?? "")}" placeholder="https://..."></td>
      <td>
        <select data-k="cred_type">
          <option value="api_key" ${credType === "api_key" ? "selected" : ""}>api_key</option>
          <option value="auth_token" ${credType === "auth_token" ? "selected" : ""}>auth_token</option>
        </select>
      </td>
      <td>
        <div class="secret-cell">
          <input data-k="secret" type="password" value="${esc(secret)}" placeholder="secret">
          <button data-act="reveal" title="Show/hide">👁</button>
        </div>
      </td>
      <td>${statusCell}</td>
      <td>
        <button data-act="test" title="Send a health ping">Test</button>
        <button data-act="del" class="danger" title="Delete row">×</button>
      </td>
    `;
    tr.dataset.idx = i;
    rowsEl.appendChild(tr);
  });
}

rowsEl.addEventListener("input", (ev) => {
  const tr = ev.target.closest("tr"); if (!tr) return;
  const i = parseInt(tr.dataset.idx);
  const k = ev.target.dataset.k;
  if (!k) return;
  const e = entries[i];
  if (k === "cred_type") {
    const secret = tr.querySelector('[data-k="secret"]').value;
    if (ev.target.value === "api_key")   { e.api_key = secret; e.auth_token = null; }
    else                                 { e.auth_token = secret; e.api_key = null; }
  }
  else if (k === "secret") {
    if (e.api_key != null) e.api_key = ev.target.value;
    else e.auth_token = ev.target.value;
  }
  else e[k] = ev.target.value;
  markDirty();
});

// ---- Drag-to-reorder ------------------------------------------------
let dragFromIdx = null;

rowsEl.addEventListener("dragstart", (ev) => {
  const tr = ev.target.closest("tr"); if (!tr) return;
  dragFromIdx = parseInt(tr.dataset.idx);
  tr.classList.add("drag-source");
  // Firefox needs some data set on the transfer to actually fire drag events.
  try { ev.dataTransfer.setData("text/plain", String(dragFromIdx)); } catch {}
  ev.dataTransfer.effectAllowed = "move";
});

rowsEl.addEventListener("dragend", (ev) => {
  const tr = ev.target.closest("tr"); if (tr) tr.classList.remove("drag-source");
  clearDragOverClasses();
  dragFromIdx = null;
});

function clearDragOverClasses() {
  for (const el of rowsEl.querySelectorAll(".drag-over-top, .drag-over-bottom")) {
    el.classList.remove("drag-over-top", "drag-over-bottom");
  }
}

rowsEl.addEventListener("dragover", (ev) => {
  if (dragFromIdx === null) return;
  const tr = ev.target.closest("tr"); if (!tr) return;
  ev.preventDefault();
  ev.dataTransfer.dropEffect = "move";
  clearDragOverClasses();
  const rect = tr.getBoundingClientRect();
  const above = (ev.clientY - rect.top) < rect.height / 2;
  tr.classList.add(above ? "drag-over-top" : "drag-over-bottom");
});

rowsEl.addEventListener("dragleave", (ev) => {
  // Clear classes only when we leave the tbody entirely, otherwise
  // moving between rows flickers.
  if (!rowsEl.contains(ev.relatedTarget)) clearDragOverClasses();
});

rowsEl.addEventListener("drop", (ev) => {
  if (dragFromIdx === null) return;
  const tr = ev.target.closest("tr"); if (!tr) return;
  ev.preventDefault();
  const targetIdx = parseInt(tr.dataset.idx);
  const rect = tr.getBoundingClientRect();
  const above = (ev.clientY - rect.top) < rect.height / 2;
  let insertAt = above ? targetIdx : targetIdx + 1;
  // Pull the source out; fix up the target if it shifted left.
  const [moved] = entries.splice(dragFromIdx, 1);
  if (dragFromIdx < insertAt) insertAt -= 1;
  entries.splice(insertAt, 0, moved);
  clearDragOverClasses();
  render();     // render() renumbers priority from the new row order
  markDirty();
});

rowsEl.addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button"); if (!btn) return;
  const tr = ev.target.closest("tr"); if (!tr) return;
  const i = parseInt(tr.dataset.idx);
  const act = btn.dataset.act;
  if (act === "del") {
    entries.splice(i, 1); render(); markDirty();
  } else if (act === "reveal") {
    const inp = tr.querySelector('[data-k="secret"]');
    inp.type = inp.type === "password" ? "text" : "password";
  } else if (act === "test") {
    const name = entries[i].name;
    if (!name) { toast("Save this row before testing.", "err"); return; }
    btn.disabled = true; btn.textContent = "…";
    try {
      const r = await fetch(`/_admin/api/test/${encodeURIComponent(name)}`, {method: "POST"});
      const j = await r.json();
      if (j.ok) toast(`${name}: ok (${Math.round(j.latency_ms)}ms)`, "ok");
      else      toast(`${name}: ${j.error || "failed"}`, "err");
      await loadHealth();
    } finally {
      btn.disabled = false; btn.textContent = "Test";
    }
  }
});

$("add").onclick = () => {
  // New rows go to the bottom; render() will assign the next priority.
  entries.push({ name: "", base_url: "", api_key: "", auth_token: null, priority: entries.length + 1 });
  render(); markDirty();
};

$("revert").onclick = () => {
  entries = JSON.parse(JSON.stringify(saved));
  render(); setDirty(false);
};

$("refresh").onclick = loadHealth;

$("save").onclick = async () => {
  $("save").disabled = true;
  try {
    const r = await fetch("/_admin/api/config", {
      method: "PUT",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ api: entries }),
    });
    const j = await r.json();
    if (!r.ok) { toast(j.error || "save failed", "err"); $("save").disabled = false; return; }
    saved = JSON.parse(JSON.stringify(j.api));
    entries = JSON.parse(JSON.stringify(j.api));
    render(); setDirty(false);
    toast("Saved.", "ok");
    await loadHealth();
  } catch (e) {
    toast(String(e), "err"); $("save").disabled = false;
  }
};

// ---- Settings panel --------------------------------------------------
function renderSettings() {
  const grid = $("settings-grid");
  grid.innerHTML = "";
  for (const [key, label, type, hint, restart] of SETTINGS_FIELDS) {
    const l = document.createElement("label");
    l.innerHTML = `<code>${key}</code> ${esc(label)}${restart ? '<span class="tag">restart</span>' : ""}`;
    const input = document.createElement("input");
    input.type = type;
    input.dataset.k = key;
    input.value = settings[key] ?? "";
    if (type === "number") input.step = "any";
    const h = document.createElement("div");
    h.className = "hint"; h.textContent = hint;
    grid.appendChild(l); grid.appendChild(input); grid.appendChild(h);
  }
}

$("settings-grid").addEventListener?.("input", () => {}); // placeholder, real listener attached after render

// Attach a delegated listener for settings inputs.
document.addEventListener("input", (ev) => {
  const inp = ev.target.closest("#settings-grid input");
  if (!inp) return;
  const k = inp.dataset.k;
  let v = inp.value;
  if (inp.type === "number") v = v === "" ? null : Number(v);
  settings[k] = v;
});

$("revert-settings").onclick = () => {
  settings = JSON.parse(JSON.stringify(savedSettings));
  renderSettings();
};

$("save-settings").onclick = async () => {
  $("save-settings").disabled = true;
  try {
    const r = await fetch("/_admin/api/settings", {
      method: "PUT",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ proxy: settings }),
    });
    const j = await r.json();
    if (!r.ok) { toast(j.error || "save failed", "err"); return; }
    savedSettings = JSON.parse(JSON.stringify(j.proxy));
    settings = JSON.parse(JSON.stringify(j.proxy));
    renderSettings();
    if (j.restart_required && j.restart_required.length) {
      toast(`Saved. Restart required for: ${j.restart_required.join(", ")}`, "warn");
    } else {
      toast("Settings saved.", "ok");
    }
    await loadHealth();
  } catch (e) {
    toast(String(e), "err");
  } finally {
    $("save-settings").disabled = false;
  }
};

// ---- Loaders ---------------------------------------------------------
async function loadAll() {
  const [cfgResp, setResp] = await Promise.all([
    fetch("/_admin/api/config"),
    fetch("/_admin/api/settings"),
  ]);
  const cfg = await cfgResp.json();
  const set = await setResp.json();
  entries = JSON.parse(JSON.stringify(cfg.api));
  saved   = JSON.parse(JSON.stringify(cfg.api));
  settings      = JSON.parse(JSON.stringify(set.proxy));
  savedSettings = JSON.parse(JSON.stringify(set.proxy));
  render(); setDirty(false);
  renderSettings();
  await loadHealth();
}

async function loadHealth() {
  try {
    const r = await fetch("/_admin/api/health", {cache: "no-store"});
    const j = await r.json();
    health = {};
    for (const h of j.health) health[h.name] = h;
    activeName = j.active_upstream;
    listenUrl = j.listen || "";
    $("status").innerHTML = listenUrl
      ? `listening on <b>${esc(listenUrl)}</b> &nbsp;•&nbsp; active: <b>${esc(activeName || "-")}</b>`
      : "";
    renderEnv();
    render();
  } catch (e) {
    $("status").textContent = "router unreachable";
  }
}

// ---- Traffic chart ---------------------------------------------------
// Colour palette: first 8 distinct hues, cycled thereafter. Stable by
// insertion order in the series dict.
const PALETTE = ["#4f8cff", "#22c55e", "#f59e0b", "#ef4444",
                 "#a855f7", "#06b6d4", "#ec4899", "#84cc16"];
let _upstreamColors = {};
function colorFor(name) {
  if (_upstreamColors[name]) return _upstreamColors[name];
  const idx = Object.keys(_upstreamColors).length;
  _upstreamColors[name] = PALETTE[idx % PALETTE.length];
  return _upstreamColors[name];
}

function fmtTime(epochSec, windowSec) {
  const d = new Date(epochSec * 1000);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  // For windows > 1 day, include month/day so labels disambiguate.
  if (windowSec > 86400) {
    const mo = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    return `${mo}-${dd} ${hh}h`;
  }
  return `${hh}:${mm}`;
}

function renderChart(data) {
  const buckets = data.buckets || [];
  const series  = data.series  || {};
  const names   = Object.keys(series);
  const totals  = data.totals  || {};
  const grandTotal = Object.values(totals).reduce((a, b) => a + b, 0);

  const chart = $("chart");
  const empty = $("chart-empty");
  const legend = $("chart-legend");
  const total = $("traffic-total");
  const sub   = $("traffic-sub");
  const bucketMin = Math.round(data.bucket_sec / 60);
  const bucketLabel = bucketMin >= 60 ? `${Math.round(bucketMin/60)}-hour` : `${bucketMin}-minute`;
  const windowLabel = data.window_sec >= 86400
    ? `${Math.round(data.window_sec / 86400)} day${data.window_sec > 86400 ? "s" : ""}`
    : `${Math.round(data.window_sec / 3600)}h`;
  sub.textContent = `requests per upstream, ${bucketLabel} buckets, last ${windowLabel}`;

  if (grandTotal === 0 || names.length === 0) {
    if (chartInstance) { chartInstance.clear(); }
    chart.style.visibility = "hidden";
    empty.style.display = "";
    legend.innerHTML = "";
    total.textContent = "no requests in window";
    return;
  }
  chart.style.visibility = "visible";
  empty.style.display = "none";
  total.textContent = `${grandTotal} total request${grandTotal === 1 ? "" : "s"}`;

  // Initialise ECharts lazily (script is loaded before this code runs).
  if (!chartInstance) {
    chartInstance = echarts.init(chart, null, { renderer: "canvas" });
    window.addEventListener("resize", () => chartInstance && chartInstance.resize());
  }

  const xLabels = buckets.map(b => fmtTime(b, data.window_sec));
  const sortedNames = names.slice().sort((a, b) => (totals[b] || 0) - (totals[a] || 0));

  const option = {
    backgroundColor: "transparent",
    color: sortedNames.map(colorFor),
    textStyle: { color: "#e6e8ec" },
    grid: { top: 20, right: 20, bottom: 30, left: 42 },
    tooltip: {
      trigger: "axis",
      axisPointer: {
        type: "cross",
        label: { backgroundColor: "#2a2f3a" },
        lineStyle: { color: "#4f8cff", width: 1 },
        crossStyle: { color: "#4f8cff" },
      },
      backgroundColor: "#0c0e13",
      borderColor: "#2a2f3a",
      textStyle: { color: "#e6e8ec", fontSize: 12 },
      extraCssText: "box-shadow: 0 4px 12px rgba(0,0,0,0.5);",
      formatter: (params) => {
        if (!params || !params.length) return "";
        const time = params[0].axisValueLabel;
        const rows = params.map(p => {
          const c = p.color;
          const v = p.value == null ? 0 : p.value;
          return `<div style="display:flex;align-items:center;gap:6px;margin:2px 0">
            <span style="display:inline-block;width:8px;height:8px;background:${c};border-radius:2px"></span>
            <span>${esc(p.seriesName)}</span>
            <span style="margin-left:auto;font-weight:500;font-variant-numeric:tabular-nums">${v}</span>
          </div>`;
        }).join("");
        return `<div style="color:#8a92a5;font-size:11px;margin-bottom:6px">${esc(time)}</div>${rows}`;
      },
    },
    xAxis: {
      type: "category",
      boundaryGap: false,
      data: xLabels,
      axisLine:  { lineStyle: { color: "#2a2f3a" } },
      axisTick:  { lineStyle: { color: "#2a2f3a" } },
      axisLabel: { color: "#8a92a5", fontSize: 10 },
      splitLine: { show: false },
    },
    yAxis: {
      type: "value",
      minInterval: 1,
      axisLine:  { lineStyle: { color: "#2a2f3a" } },
      axisLabel: { color: "#8a92a5", fontSize: 10 },
      splitLine: { lineStyle: { color: "#1e232e", type: "dashed" } },
    },
    series: sortedNames.map(name => ({
      name,
      type: "line",
      smooth: 0.3,
      showSymbol: false,
      symbol: "circle",
      symbolSize: 6,
      emphasis: { focus: "series", scale: true },
      lineStyle: { width: 2 },
      data: series[name],
    })),
  };

  // `notMerge: true` so removed upstreams don't ghost in later renders.
  chartInstance.setOption(option, { notMerge: true });

  legend.innerHTML = sortedNames.map(name => {
    const c = colorFor(name);
    const t = totals[name] || 0;
    return `<span><span class="swatch" style="background:${c}"></span>` +
           `<span class="name">${esc(name)}</span>` +
           `<span class="count">(${t})</span></span>`;
  }).join("");
}

async function loadStats() {
  const win = parseInt($("window-sel").value) || 86400;
  try {
    const r = await fetch(`/_admin/api/stats?bucket_sec=3600&window_sec=${win}`, {cache: "no-store"});
    const j = await r.json();
    renderChart(j);
  } catch (e) {
    // keep previous chart if endpoint fails
  }
}

$("window-sel").addEventListener("change", loadStats);

loadAll();
setInterval(loadHealth, 2500);
loadStats();
setInterval(loadStats, 30000);
</script>
</body>
</html>
"""


def _entry_to_wire(e: ApiEntry) -> dict[str, Any]:
    d: dict[str, Any] = {
        "name": e.name,
        "base_url": e.base_url,
        "priority": e.priority,
        "api_key": e.api_key,
        "auth_token": e.auth_token,
    }
    if e.health_check_model:
        d["health_check_model"] = e.health_check_model
    return d


def _entry_from_wire(raw: dict[str, Any]) -> ApiEntry:
    cleaned: dict[str, Any] = {}
    for k, v in raw.items():
        if isinstance(v, str) and v.strip() == "":
            cleaned[k] = None
        else:
            cleaned[k] = v
    return ApiEntry.model_validate(cleaned)


def register_admin(
    app: web.Application,
    cfg: RouterConfig,
    state: State,
    config_path: Path,
    stop_event: asyncio.Event | None = None,
) -> None:
    async def index(_request: web.Request) -> web.Response:
        return web.Response(text=INDEX_HTML, content_type="text/html")

    async def get_config(_request: web.Request) -> web.Response:
        return web.json_response({"api": [_entry_to_wire(e) for e in cfg.api]})

    async def put_config(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except Exception as e:
            return web.json_response({"error": f"invalid JSON: {e}"}, status=400)

        raw_entries = payload.get("api", [])
        if not isinstance(raw_entries, list):
            return web.json_response(
                {"error": "'api' must be a list"}, status=400
            )

        new_entries: list[ApiEntry] = []
        for i, raw in enumerate(raw_entries):
            try:
                new_entries.append(_entry_from_wire(raw))
            except Exception as e:
                return web.json_response(
                    {"error": f"row {i + 1}: {e}"}, status=400
                )

        names = [e.name for e in new_entries]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            return web.json_response(
                {"error": f"duplicate names: {', '.join(sorted(dupes))}"},
                status=400,
            )

        new_cfg = RouterConfig(proxy=cfg.proxy, api=new_entries)
        try:
            config_mod.save(new_cfg, config_path)
        except Exception as e:
            return web.json_response(
                {"error": f"could not write {config_path}: {e}"}, status=500
            )

        old_names = {e.name for e in cfg.api}
        new_names = {e.name for e in new_entries}
        cfg.api[:] = new_entries
        for gone in old_names - new_names:
            state.health.pop(gone, None)
            if state.active_upstream == gone:
                state.active_upstream = None

        added = new_names - old_names
        removed = old_names - new_names
        if added or removed:
            state.log(
                "info",
                f"config saved ({len(new_entries)} entries; "
                f"+{len(added)} -{len(removed)})",
            )
        else:
            state.log("info", f"config saved ({len(new_entries)} entries)")

        return web.json_response({"api": [_entry_to_wire(e) for e in cfg.api]})

    async def get_settings(_request: web.Request) -> web.Response:
        return web.json_response({"proxy": cfg.proxy.model_dump()})

    async def put_settings(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except Exception as e:
            return web.json_response({"error": f"invalid JSON: {e}"}, status=400)
        raw = payload.get("proxy", {})
        if not isinstance(raw, dict):
            return web.json_response(
                {"error": "'proxy' must be a dict"}, status=400
            )
        # Validate via pydantic. Start from the current values so clients
        # can send a partial dict if they want.
        merged = cfg.proxy.model_dump()
        merged.update(raw)
        try:
            new_proxy = ProxyConfig.model_validate(merged)
        except Exception as e:
            return web.json_response({"error": f"invalid settings: {e}"}, status=400)

        # Detect restart-required changes.
        needs_restart: list[str] = []
        for f in RESTART_REQUIRED_FIELDS:
            if getattr(cfg.proxy, f) != getattr(new_proxy, f):
                needs_restart.append(f)

        new_cfg = RouterConfig(proxy=new_proxy, api=cfg.api)
        try:
            config_mod.save(new_cfg, config_path)
        except Exception as e:
            return web.json_response(
                {"error": f"could not write {config_path}: {e}"}, status=500
            )

        # Hot-swap in memory. All runtime code re-reads cfg.proxy.X per
        # request / per loop iteration, so the new values take effect
        # immediately — except for the bind-time fields listed above,
        # which the client is told about via restart_required.
        cfg.proxy = new_proxy
        if needs_restart:
            state.log(
                "info",
                f"settings saved (restart required for: {', '.join(needs_restart)})",
            )
        else:
            state.log("info", "settings saved (hot-reloaded)")

        return web.json_response(
            {"proxy": cfg.proxy.model_dump(), "restart_required": needs_restart}
        )

    async def get_health(_request: web.Request) -> web.Response:
        out = []
        for e in cfg.api:
            h = state.health.get(e.name)
            out.append(
                {
                    "name": e.name,
                    "status": h.status if h else "unknown",
                    "last_latency_ms": h.last_latency_ms if h else None,
                    "last_error": h.last_error if h else None,
                    "cooldown_until": h.cooldown_until if h else 0,
                }
            )
        listen = f"http://{cfg.proxy.listen_host}:{cfg.proxy.listen_port}"
        return web.json_response(
            {
                "health": out,
                "active_upstream": state.active_upstream,
                "listen": listen,
                "now": time.time(),
            }
        )

    async def get_stats(request: web.Request) -> web.Response:
        try:
            bucket_sec = int(request.query.get("bucket_sec", "3600"))
            window_sec = int(request.query.get("window_sec", "86400"))
        except ValueError:
            return web.json_response(
                {"error": "bucket_sec and window_sec must be integers"}, status=400
            )
        now = time.time()
        buckets, series = _bucket_series(
            state.request_log,
            bucket_sec=bucket_sec,
            window_sec=window_sec,
            now=now,
        )
        totals = {name: sum(counts) for name, counts in series.items()}
        return web.json_response(
            {
                "now": now,
                "bucket_sec": bucket_sec,
                "window_sec": window_sec,
                "buckets": buckets,
                "series": series,
                "totals": totals,
            }
        )

    async def test_entry(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        entry = cfg.find(name)
        if entry is None:
            return web.json_response({"ok": False, "error": "no such entry"}, status=404)
        model = entry.health_model(cfg.proxy.health_check_model)
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            result = await ping(session, entry, model, timeout_sec=20.0)
        state.record_health(
            entry,
            ok=result.ok,
            latency_ms=result.latency_ms,
            error=result.error,
            status_code=result.status_code,
            auth_failure_cooldown=cfg.proxy.auth_failure_cooldown,
        )
        return web.json_response(
            {
                "ok": result.ok,
                "latency_ms": result.latency_ms or 0,
                "status_code": result.status_code,
                "error": result.error,
            }
        )

    app.router.add_get("/_admin", index)
    app.router.add_get("/_admin/", index)
    app.router.add_get("/_admin/api/config", get_config)
    app.router.add_put("/_admin/api/config", put_config)
    app.router.add_get("/_admin/api/settings", get_settings)
    app.router.add_put("/_admin/api/settings", put_settings)
    async def shutdown(_request: web.Request) -> web.Response:
        if stop_event is None:
            return web.json_response(
                {"error": "shutdown not wired (no stop event)"}, status=500
            )
        state.log("info", "shutdown requested via admin API")
        # Respond first, then set the stop event so this request completes
        # cleanly before the server tears down.
        async def _defer() -> None:
            await asyncio.sleep(0.2)
            stop_event.set()
        asyncio.create_task(_defer())
        return web.json_response({"ok": True, "message": "shutting down"}, status=202)

    app.router.add_get("/_admin/api/health", get_health)
    app.router.add_get("/_admin/api/stats", get_stats)
    app.router.add_post("/_admin/api/test/{name}", test_entry)
    app.router.add_post("/_admin/api/shutdown", shutdown)
