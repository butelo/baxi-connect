# Baxi Connect — control it without the official app

Reverse-engineered control of a **BAXI Connect** room unit (BDR Thermea platform: Baxi,
Remeha, De Dietrich, Brötje…) with no vendor app involved. Three pieces, usable separately:

| Piece | What it is |
|---|---|
| `baxi.py` | command line: read state, set temperature, modes, weekly program |
| `baxi_mcp.py` | MCP server, so any AI agent can call those as tools |
| `baxi-agent/` | Cloudflare Worker + Durable Object running a **Pi Durable** agent behind a Telegram bot |

> ⚠️ Unofficial. These are private endpoints the official apps use. They are not documented
> by BDR Thermea and can change or disappear without notice (the previous backend already
> did). This controls heating in a real home — don't wire it to anything unattended without
> limits.

## Background: why there is no local API

I scanned the room unit on a normal home LAN:

- **TCP 1–65535: nothing open.** No web page, no socket.
- UDP 6666/6667 (Tuya), 5683 (CoAP), 5353 (mDNS), 1900 (SSDP): no response.
- ICMP filtered, no mDNS advertisement.

The unit holds an outbound connection to the vendor cloud and nothing else. So control means
talking to that cloud:

```
app ──OAuth2 (Azure AD B2C)──► remehalogin.bdrthermea.net
    ──Bearer token──────────► https://api.bdrthermea.net/Mobile/api ──► thermostat
```

The API gateway (`bdriotprdapim` in the DNS chain — Azure API Management, West Europe)
checks two independent things: an `Ocp-Apim-Subscription-Key` that identifies *the app*
(public, baked into the APK) and a Bearer token that identifies *you*.

## Quick start

Python ≥ 3.8, standard library only.

```bash
python3 baxi.py login      # your Baxi/Remeha/De Dietrich app email + password
python3 baxi.py status
python3 baxi.py set 21.5                 # hold a temperature
python3 baxi.py set 21.5 --mode override # until the next program step
python3 baxi.py auto                     # back to the weekly program
python3 baxi.py off                      # anti-frost
python3 baxi.py status --json
```

The password is used once, over TLS, and never stored. Only the OAuth refresh token is
written, to `.baxi_token.json` (mode 600, gitignored).

### Give it to an AI agent (local MCP)

```json
{
  "mcpServers": {
    "baxi": { "command": "python3", "args": ["/absolute/path/to/baxi_mcp.py"] }
  }
}
```

Tools: `baxi_status`, `baxi_list_zones`, `baxi_set_temperature`, `baxi_set_mode`.

### Or run it as a Telegram bot on Cloudflare

A Worker receives the webhook; a Durable Object runs the **Pi Durable** harness with the
thermostat as tools, storing the transcript and the rotating OAuth token in SQLite.

```
Telegram ──► Worker /telegram ──► Durable Object (Pi session + token) ──► BDR cloud ──► boiler
```

Full instructions in [baxi-agent/README.md](baxi-agent/README.md). Short version:

```bash
python3 tools/seed_token.py --write                 # asks for the BotFather token
cd baxi-agent && npm install && npx wrangler deploy
cd .. && WORKER_URL=https://<worker>.<subdomain>.workers.dev/telegram ./tools/setup_bot.sh
```

Then message the bot: it tells you its own `chat_id`, which you allow with
`./tools/set_allowed.sh <chat-id>` — pass more ids to let family phones in too.
**Do this** — a public bot without an allowlist lets strangers heat your home.

## API notes

Everything lives under `https://api.bdrthermea.net/Mobile/api`, with the subscription key
header and a Bearer token.

| Method | Path | Purpose |
|---|---|---|
| GET | `/homes/dashboard` | all state: zones, setpoint, mode, pressure, DHW |
| POST | `/climate-zones/{id}/modes/manual` | hold a temperature |
| POST | `/climate-zones/{id}/modes/temporary-override` | until next program step |
| POST | `/climate-zones/{id}/modes/schedule` | follow a program (`heatingProgramId`) |
| POST | `/climate-zones/{id}/modes/anti-frost` | off |
| GET | `/climate-zones/{id}/time-programs/heating/{1..3}` | weekly program |
| PUT | same path | write switch points |
| GET | `/climate-zones/{id}/activities` | activity presets |

Two limits worth knowing, both verified empirically:

- **Activity temperatures are read-only.** `PUT …/activities/{n}` returns 404. The program
  decides *which* activity applies *when*; the temperature behind each activity is set on
  the device.
- **`customValue` in a switch point is rejected** (400), so a schedule can't carry an
  arbitrary temperature — only an activity.

Activity names are user-defined and can be misleading (one install had "Evening" = 6 °C).
Always quote the temperature, never the name.

## Auth details

B2C rotates the refresh token on every refresh. Persist the new one or the next call fails.
If you ever see `invalid_grant`, run `python3 baxi.py login` again and re-seed.

## Repo layout

```
baxi.py              CLI
baxi_api.py          OAuth2 + API client (stdlib only)
baxi_mcp.py          MCP server (stdio JSON-RPC)
baxi-agent/          Cloudflare Worker + Durable Object + Pi harness
  src/index.ts       webhook, secret check, chat allowlist
  src/assistant.ts   Pi harness, system prompt, thermostat tools
  src/baxi.ts        token refresh + API, stored in the DO
tools/               seed_token.py, wrangler wrapper, set_allowed.sh, Telegram helpers
```

## Security

- Never commit `.baxi_token.json`, `.dev.vars`, or `.wrangler-home/` — all gitignored.
- The refresh token grants full control of the heating; treat it like a password.
- The Telegram bot is publicly discoverable: `ALLOWED_CHAT_IDS` is not optional.
- Cost on Cloudflare: Workers + Durable Objects free tier, and Workers AI free neurons
  (10,000/day) — a thermostat conversation uses roughly 30–60 neurons per turn.

## Credits

Built on the community's earlier reverse engineering:
[awhitwam/bdrthermea](https://github.com/awhitwam/bdrthermea) and
[freitdav/BDRthermostatHA](https://github.com/freitdav/BDRthermostatHA). The agent runtime is
[Pi Durable](https://pi.dev/) via Cloudflare's [`PiHarness`](https://developers.cloudflare.com/agents/harnesses/pi/).
