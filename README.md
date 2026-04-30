# HLL OBS Kill Overlay for Railway

A Railway-ready OBS browser overlay for Hell Let Loose servers. It connects to the HLL RCON port, polls server logs, counts match kills by team/faction, and serves a transparent overlay page for OBS.

## Features

- Counts `KILL:` events by the killer's faction/team.
- Ignores `TEAM KILL:` by default.
- Auto-resets when a new `MATCH START` log appears.
- Manual reset endpoint.
- Transparent OBS overlay at `/overlay`.
- JSON status endpoint at `/api/state`.
- No database required; state is kept in memory.

## Railway Setup

1. Upload/deploy this folder to GitHub.
2. Create a new Railway project from the GitHub repo.
3. Add these Railway Variables:

```env
RCON_HOST=your.server.ip.or.hostname
RCON_PORT=your_rcon_port
RCON_PASSWORD=your_rcon_password
POLL_SECONDS=5
LOG_LOOKBACK_MINUTES=5
OVERLAY_TITLE=DMT Kill Count
ALLIES_LABEL=Allies
AXIS_LABEL=Axis
COUNT_TEAM_KILLS=false
ACCESS_TOKEN=change-this-to-any-secret
```

4. Deploy.
5. Add this URL as an OBS Browser Source:

```text
https://YOUR-RAILWAY-APP.up.railway.app/overlay?token=change-this-to-any-secret
```

Recommended OBS Browser Source size: `1920x250` or `800x250`.

## Endpoints

- `/overlay?token=...` — OBS overlay.
- `/api/state?token=...` — current kill counts as JSON.
- `/api/reset?token=...` — manual reset.
- `/health` — Railway health/status.

## Notes

The overlay uses raw HLL RCON TCP with the XOR key sent by the server. The RCON command used is `ShowLog <minutes>`. HLL logs only persist since the last game-server restart, so if Railway restarts mid-match the app rebuilds counts from whatever logs are still available inside the configured lookback window.
