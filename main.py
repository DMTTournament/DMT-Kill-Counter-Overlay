import asyncio
import os
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Set, Tuple
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse


RCON_HOST = os.getenv("RCON_HOST", "")
RCON_PORT = int(os.getenv("RCON_PORT", "0") or 0)
RCON_PASSWORD = os.getenv("RCON_PASSWORD", "")
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "5"))
LOG_LOOKBACK_MINUTES = int(os.getenv("LOG_LOOKBACK_MINUTES", "5"))
OVERLAY_TITLE = os.getenv("OVERLAY_TITLE", "HLL Kill Count")
ALLIES_LABEL = os.getenv("ALLIES_LABEL", "Allies")
AXIS_LABEL = os.getenv("AXIS_LABEL", "Axis")
COUNT_TEAM_KILLS = os.getenv("COUNT_TEAM_KILLS", "false").lower() in ("1", "true", "yes", "y")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "")

LOG_LINE_RE = re.compile(r"^\[(?P<age>.+?) \((?P<ts>\d+)\)\] (?P<body>.*)$")
KILL_RE = re.compile(
    r"^(?P<kind>TEAM KILL|KILL):\s+"
    r"(?P<attacker>.*?)\((?P<attacker_team>Allies|Axis)/(?P<attacker_id>[^)]*)\)\s+->\s+"
    r"(?P<victim>.*?)\((?P<victim_team>Allies|Axis)/(?P<victim_id>[^)]*)\)\s+with\s+(?P<weapon>.*)$",
    re.IGNORECASE,
)
MATCH_START_RE = re.compile(r"^MATCH START\s+(?P<map>.+)$", re.IGNORECASE)
MATCH_END_RE = re.compile(r"^MATCH ENDED\s+(?P<details>.+)$", re.IGNORECASE)


class RconError(RuntimeError):
    pass


class HLLRconClient:
    """Minimal HLL RCON client using the server-provided XOR key."""

    def __init__(self, host: str, port: int, password: str, timeout: float = 8.0):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout

    @staticmethod
    def _xor(data: bytes, key: bytes) -> bytes:
        return bytes(byte ^ key[i % len(key)] for i, byte in enumerate(data))

    def command_sync(self, command: str) -> str:
        if not self.host or not self.port or not self.password:
            raise RconError("RCON_HOST, RCON_PORT, and RCON_PASSWORD must be configured.")

        with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
            sock.settimeout(self.timeout)
            key = sock.recv(4096)
            if not key:
                raise RconError("RCON server did not send an encryption key.")

            def send(cmd: str) -> str:
                encrypted = self._xor(cmd.encode("utf-8"), key)
                sock.sendall(encrypted)
                chunks = []
                while True:
                    try:
                        part = sock.recv(65535)
                    except socket.timeout:
                        break
                    if not part:
                        break
                    chunks.append(part)
                    # HLL RCON responses normally arrive as a single packet, but logs can be larger.
                    if len(part) < 65535:
                        break
                if not chunks:
                    return ""
                return self._xor(b"".join(chunks), key).decode("utf-8", errors="replace")

            login = send(f"Login {self.password}")
            if "SUCCESS" not in login.upper():
                raise RconError(f"RCON login failed: {login!r}")
            return send(command)

    async def command(self, command: str) -> str:
        return await asyncio.to_thread(self.command_sync, command)


@dataclass
class MatchState:
    counts: Dict[str, int] = field(default_factory=lambda: {"Allies": 0, "Axis": 0})
    seen_events: Set[str] = field(default_factory=set)
    last_match_start_key: Optional[str] = None
    map_name: str = "Unknown"
    last_poll_ok: bool = False
    last_error: str = ""
    last_update_epoch: float = 0
    total_events: int = 0

    def reset(self, map_name: str = "Unknown", match_key: Optional[str] = None):
        self.counts = {"Allies": 0, "Axis": 0}
        self.seen_events = set()
        self.last_match_start_key = match_key
        self.map_name = map_name
        self.total_events = 0
        self.last_update_epoch = time.time()


state = MatchState()
state_lock = asyncio.Lock()
client = HLLRconClient(RCON_HOST, RCON_PORT, RCON_PASSWORD)
app = FastAPI(title="HLL OBS Kill Overlay")


def require_token(request: Request):
    if ACCESS_TOKEN and request.query_params.get("token") != ACCESS_TOKEN:
        raise HTTPException(status_code=401, detail="Missing or invalid token")


def normalize_log_text(text: str) -> str:
    text = (text or "").strip("\n")
    if not text or text.upper() == "EMPTY":
        return ""
    # Escape accidental embedded newlines inside MESSAGE/KICK/BAN logs.
    return re.sub(r"\n(?!\[.+? \(\d+\)\])", r"\\n", text)


def parse_log_line(line: str) -> Optional[Tuple[str, int, str]]:
    m = LOG_LINE_RE.match(line.strip())
    if not m:
        return None
    return f"{m.group('ts')}|{m.group('body')}", int(m.group("ts")), m.group("body")


def team_payload():
    return {
        "title": OVERLAY_TITLE,
        "map": state.map_name,
        "counts": {
            "Allies": state.counts.get("Allies", 0),
            "Axis": state.counts.get("Axis", 0),
        },
        "labels": {
            "Allies": ALLIES_LABEL,
            "Axis": AXIS_LABEL,
        },
        "last_poll_ok": state.last_poll_ok,
        "last_error": state.last_error,
        "last_update_epoch": state.last_update_epoch,
        "total_events": state.total_events,
        "count_team_kills": COUNT_TEAM_KILLS,
    }


async def process_logs(raw_logs: str):
    clean = normalize_log_text(raw_logs)
    if not clean:
        async with state_lock:
            state.last_poll_ok = True
            state.last_error = ""
            state.last_update_epoch = time.time()
        return

    events = []
    for line in clean.splitlines():
        parsed = parse_log_line(line)
        if parsed:
            events.append(parsed)

    events.sort(key=lambda item: item[1])

    async with state_lock:
        for event_key, _ts, body in events:
            start = MATCH_START_RE.match(body)
            if start:
                if event_key != state.last_match_start_key:
                    state.reset(map_name=start.group("map").strip(), match_key=event_key)
                continue

            end = MATCH_END_RE.match(body)
            if end:
                continue

            kill = KILL_RE.match(body)
            if not kill:
                continue

            kind = kill.group("kind").upper()
            if kind == "TEAM KILL" and not COUNT_TEAM_KILLS:
                continue

            if event_key in state.seen_events:
                continue

            attacker_team = kill.group("attacker_team").capitalize()
            if attacker_team in ("Allies", "Axis"):
                state.counts[attacker_team] = state.counts.get(attacker_team, 0) + 1
                state.seen_events.add(event_key)
                state.total_events += 1

        state.last_poll_ok = True
        state.last_error = ""
        state.last_update_epoch = time.time()


async def poll_loop():
    while True:
        try:
            logs = await client.command(f"ShowLog {LOG_LOOKBACK_MINUTES}")
            await process_logs(logs)
        except Exception as exc:
            async with state_lock:
                state.last_poll_ok = False
                state.last_error = str(exc)
                state.last_update_epoch = time.time()
        await asyncio.sleep(POLL_SECONDS)


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(poll_loop())


@app.get("/health")
async def health():
    return {"ok": True, "rcon_configured": bool(RCON_HOST and RCON_PORT and RCON_PASSWORD)}


@app.get("/api/state")
async def api_state(request: Request):
    require_token(request)
    async with state_lock:
        return JSONResponse(team_payload())


@app.get("/api/reset")
async def api_reset(request: Request):
    require_token(request)
    async with state_lock:
        state.reset(map_name=state.map_name)
        state.last_error = "Manual reset"
    return {"ok": True, "state": team_payload()}


@app.get("/overlay", response_class=HTMLResponse)
async def overlay(request: Request):
    require_token(request)
    token = quote(request.query_params.get("token", ""))
    html = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{OVERLAY_TITLE}</title>
  <style>
    :root {{
      --bg: rgba(8, 10, 12, 0.72);
      --panel: rgba(18, 22, 26, 0.88);
      --text: #f2efe6;
      --muted: #b9b2a3;
      --accent: #d8b15f;
      --allies: #3d7edb;
      --axis: #c24d3c;
    }}
    html, body {{
      margin: 0;
      padding: 0;
      background: transparent;
      font-family: Impact, Haettenschweiler, 'Arial Narrow Bold', sans-serif;
      color: var(--text);
      overflow: hidden;
    }}
    .wrap {{
      box-sizing: border-box;
      width: 100vw;
      padding: 18px 24px;
      display: flex;
      justify-content: center;
      align-items: center;
    }}
    .scoreboard {{
      min-width: 760px;
      max-width: 1100px;
      background: var(--bg);
      border: 2px solid rgba(216, 177, 95, 0.8);
      box-shadow: 0 0 24px rgba(0,0,0,0.65);
      display: grid;
      grid-template-columns: 1fr auto 1fr;
      align-items: center;
      gap: 20px;
      padding: 16px 26px;
      border-radius: 8px;
      text-transform: uppercase;
    }}
    .team {{
      background: var(--panel);
      border-radius: 6px;
      padding: 12px 18px;
      display: grid;
      grid-template-columns: 1fr auto;
      align-items: center;
      gap: 18px;
      border-left: 8px solid var(--accent);
    }}
    .team.allies {{ border-left-color: var(--allies); }}
    .team.axis {{ border-left-color: var(--axis); }}
    .label {{
      font-size: 34px;
      letter-spacing: 1px;
      text-shadow: 2px 2px 2px #000;
      white-space: nowrap;
    }}
    .kills {{
      font-size: 58px;
      line-height: 1;
      color: var(--accent);
      text-shadow: 3px 3px 2px #000;
      min-width: 86px;
      text-align: right;
    }}
    .center {{
      text-align: center;
      min-width: 190px;
    }}
    .title {{
      font-size: 30px;
      letter-spacing: 1px;
      color: var(--accent);
      text-shadow: 2px 2px 2px #000;
    }}
    .map {{
      margin-top: 5px;
      font-family: Arial, sans-serif;
      font-weight: 700;
      font-size: 13px;
      letter-spacing: 1px;
      color: var(--muted);
    }}
    .status {{
      margin-top: 6px;
      font-family: Arial, sans-serif;
      font-size: 11px;
      color: var(--muted);
      text-transform: none;
    }}
    .bad {{ color: #ff8080; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="scoreboard">
      <div class="team allies">
        <div id="alliesLabel" class="label">{ALLIES_LABEL}</div>
        <div id="alliesKills" class="kills">0</div>
      </div>
      <div class="center">
        <div id="title" class="title">{OVERLAY_TITLE}</div>
        <div id="map" class="map">Loading...</div>
        <div id="status" class="status">Connecting...</div>
      </div>
      <div class="team axis">
        <div id="axisLabel" class="label">{AXIS_LABEL}</div>
        <div id="axisKills" class="kills">0</div>
      </div>
    </div>
  </div>
  <script>
    async function refresh() {{
      try {{
        const res = await fetch('/api/state?token={token}', {{ cache: 'no-store' }});
        const data = await res.json();
        document.getElementById('title').textContent = data.title || 'HLL Kill Count';
        document.getElementById('alliesLabel').textContent = data.labels.Allies || 'Allies';
        document.getElementById('axisLabel').textContent = data.labels.Axis || 'Axis';
        document.getElementById('alliesKills').textContent = data.counts.Allies ?? 0;
        document.getElementById('axisKills').textContent = data.counts.Axis ?? 0;
        document.getElementById('map').textContent = data.map && data.map !== 'Unknown' ? data.map : 'Current Match';
        const status = document.getElementById('status');
        if (data.last_poll_ok) {{
          status.textContent = 'Live';
          status.className = 'status';
        }} else {{
          status.textContent = data.last_error ? 'RCON Error' : 'Waiting for logs';
          status.className = 'status bad';
        }}
      }} catch (err) {{
        const status = document.getElementById('status');
        status.textContent = 'Overlay connection error';
        status.className = 'status bad';
      }}
    }}
    refresh();
    setInterval(refresh, 2000);
  </script>
</body>
</html>
    """
    return HTMLResponse(html)


@app.get("/")
async def root(request: Request):
    base = str(request.base_url).rstrip("/")
    token_part = f"?token={ACCESS_TOKEN}" if ACCESS_TOKEN else ""
    return PlainTextResponse(
        "HLL OBS Kill Overlay\n"
        f"Overlay: {base}/overlay{token_part}\n"
        f"State:   {base}/api/state{token_part}\n"
        f"Reset:   {base}/api/reset{token_part}\n"
    )
