"""Tiny HTTP health server + live dashboard.

- ``/``          : live HTML dashboard of the running agents (auto-refreshing).
- ``/status``    : JSON snapshot of every agent and what it's doing.
- ``/healthz``   : liveness (process + coordinator alive).
- ``/readyz``    : readiness (every monitoring agent has polled).
- ``/incidents`` : current open incidents.

Implemented on the stdlib ``http.server`` to avoid pulling a web framework
into a background sidecar concern.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

import pykka

from .status import StatusBoard

_log = logging.getLogger("obs_agents.health")


class HealthState:
    """Shared, read-only-ish view the probes and dashboard report on."""

    def __init__(
        self,
        monitors: list[pykka.ActorRef],
        coordinator: pykka.ActorRef,
        board: StatusBoard | None = None,
    ):
        self._monitors = monitors
        self._coordinator = coordinator
        self._board = board

    def agents(self) -> list[dict]:
        return self._board.snapshot() if self._board is not None else []

    def is_live(self) -> bool:
        return self._coordinator.is_alive()

    def is_ready(self) -> bool:
        if not self._coordinator.is_alive():
            return False
        # Every monitor must be alive and have polled at least once.
        for ref in self._monitors:
            if not ref.is_alive():
                return False
            proxy = ref.proxy()
            try:
                if not proxy.healthy.get(timeout=2):
                    return False
            except pykka.Timeout:
                return False
        return True

    def incidents(self) -> list[dict]:
        try:
            return self._coordinator.ask({"query": "incidents"}, timeout=2) or []
        except pykka.Timeout:
            return []

    def resolved(self) -> list[dict]:
        try:
            return self._coordinator.ask({"query": "incident_history"}, timeout=2) or []
        except pykka.Timeout:
            return []


def _make_handler(state: HealthState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: dict) -> None:
            self._raw(code, "application/json", json.dumps(payload).encode())

        def _raw(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - stdlib API
            if self.path in ("/", "/index.html"):
                self._raw(200, "text/html; charset=utf-8", _DASHBOARD_HTML)
            elif self.path == "/status":
                self._send(200, {
                    "agents": state.agents(),
                    "incidents": state.incidents(),
                    "resolved": state.resolved(),
                    "ready": state.is_ready(),
                })
            elif self.path == "/healthz":
                ok = state.is_live()
                self._send(200 if ok else 503, {"status": "ok" if ok else "down"})
            elif self.path == "/readyz":
                ok = state.is_ready()
                self._send(200 if ok else 503, {"ready": ok})
            elif self.path == "/incidents":
                self._send(200, {"incidents": state.incidents()})
            else:
                self._send(404, {"error": "not found"})

        def log_message(self, *_args) -> None:  # silence default stderr logging
            pass

    return Handler


_DASHBOARD_HTML = b"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Incident-Response Agents</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: system-ui, sans-serif; margin: 0; background:#10101c; color:#e8e8f0; }
  header { padding:18px 24px; border-bottom:1px solid #2a2a44; display:flex; align-items:center; gap:16px; }
  h1 { font-size:1.15rem; margin:0; font-weight:600; }
  .badge { font-size:.8rem; padding:3px 10px; border-radius:999px; background:#2a2a44; }
  .badge.ok { background:#14532d; } .badge.no { background:#5b1a1a; }
  main { padding:20px 24px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(240px,1fr)); gap:14px; }
  .card { background:#1a1a2e; border:1px solid #2a2a44; border-radius:10px; padding:14px; transition:border-color .3s; }
  .card.act { border-color:#e94560; }
  .card .top { display:flex; align-items:center; gap:8px; font-weight:600; }
  .kind { font-size:1.4rem; }
  .state { margin-top:8px; font-size:.95rem; }
  .detail { color:#9a9ab0; font-size:.8rem; margin-top:4px; min-height:1em; word-break:break-word; }
  .counters { margin-top:10px; display:flex; flex-wrap:wrap; gap:6px; }
  .chip { font-size:.7rem; background:#26263f; padding:2px 7px; border-radius:6px; color:#b8b8d0; }
  h2 { font-size:.95rem; color:#9a9ab0; margin:26px 0 10px; text-transform:uppercase; letter-spacing:.05em; }
  .inc { background:#1a1a2e; border-left:4px solid #e94560; border-radius:6px; padding:10px 14px; margin-bottom:8px; }
  .inc.warning { border-left-color:#e0a020; } .inc.info { border-left-color:#3a7bd5; }
  .inc .h { font-weight:600; } .inc .ex { color:#b8b8d0; font-size:.85rem; margin-top:3px; }
  .muted { color:#6a6a85; }
  .src { font-size:.72rem; }
</style></head>
<body>
<header>
  <span style="font-size:1.5rem">\xf0\x9f\x9b\xb0\xef\xb8\x8f</span>
  <h1>Observability Incident-Response Agents</h1>
  <span id="ready" class="badge">...</span>
  <span id="count" class="badge">...</span>
  <span class="muted" style="margin-left:auto;font-size:.78rem" id="ts"></span>
</header>
<main>
  <div class="grid" id="agents"></div>
  <h2>Open incidents</h2>
  <div id="incidents"><span class="muted">none</span></div>
  <h2>Recently resolved</h2>
  <div id="resolved"><span class="muted">none</span></div>
</main>
<script>
const KIND = {metrics:'\\u{1F4C8}', logs:'\\u{1F4DC}', traces:'\\u{1F517}', coordinator:'\\u{1F9ED}'};
const STATE = {starting:'\\u{1F7E1}', polling:'\\u{1F50D}', checking:'\\u{1F50E}', reasoning:'\\u{1F9E0}',
  reporting:'\\u{1F6A8}', suppressed:'\\u{1F92B}', idle:'\\u{1F4A4}', correlating:'\\u{1F9E9}',
  incident:'\\u{1F525}', decided:'\\u{2705}', error:'\\u{274C}'};
const SEV = {critical:'\\u{1F534}', warning:'\\u{1F7E0}', info:'\\u{1F535}'};
const ACTIVE = new Set(['polling','reasoning','reporting','correlating','incident','decided']);
const ago = t => Math.max(0, Math.round(Date.now()/1000 - t)) + 's';
function esc(s){ return (s||'').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

async function tick(){
  let d; try { d = await (await fetch('/status')).json(); } catch(e){ return; }
  const r = document.getElementById('ready');
  r.textContent = d.ready ? 'ready' : 'starting'; r.className = 'badge ' + (d.ready?'ok':'no');
  document.getElementById('count').textContent = (d.incidents.length) + ' incident(s)';
  document.getElementById('ts').textContent = 'updated ' + new Date().toLocaleTimeString();

  document.getElementById('agents').innerHTML = d.agents.map(a => {
    const c = Object.entries(a.counters||{}).map(([k,v]) =>
      `<span class="chip">${esc(k)}: ${v}</span>`).join('');
    return `<div class="card ${ACTIVE.has(a.state)?'act':''}">
      <div class="top"><span class="kind">${KIND[a.kind]||'\\u{1F916}'}</span>${esc(a.label)}</div>
      <div class="state">${STATE[a.state]||'\\u{2753}'} ${esc(a.state)}</div>
      <div class="detail">${esc(a.detail)}</div>
      <div class="counters">${c}<span class="chip">up ${ago(a.since)}</span></div>
    </div>`; }).join('') || '<span class="muted">no agents</span>';

  document.getElementById('incidents').innerHTML = d.incidents.map(i =>
    `<div class="inc ${i.severity}">
      <div class="h">${SEV[i.severity]||''} ${esc(i.incident_id)} &middot; ${esc(i.recommended_action)}
        <span class="muted src">(${(i.contributing_alerts||[]).join(', ')} \\u00b7 ${esc(i.decision_source)})</span></div>
      <div class="ex">${esc(i.explanation||i.summary)}</div>
    </div>`).join('') || '<span class="muted">none</span>';

  document.getElementById('resolved').innerHTML = (d.resolved||[]).slice(-8).reverse().map(i =>
    `<div class="inc" style="border-left-color:#3a7d5a;opacity:.7">
      <div class="h">\\u2705 ${esc(i.incident_id)} <span class="muted src">resolved (was ${esc(i.severity)}/${esc(i.recommended_action)})</span></div>
    </div>`).join('') || '<span class="muted">none</span>';
}
tick(); setInterval(tick, 1000);
</script>
</body></html>"""


def start_health_server(
    host: str,
    port: int,
    state: HealthState,
) -> Callable[[], None]:
    """Start the health server in a daemon thread; return a shutdown callable."""
    server = ThreadingHTTPServer((host, port), _make_handler(state))
    thread = threading.Thread(target=server.serve_forever, name="health", daemon=True)
    thread.start()
    _log.info("health server listening on %s:%s", host, port)

    def shutdown() -> None:
        server.shutdown()
        server.server_close()

    return shutdown
