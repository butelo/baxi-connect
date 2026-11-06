#!/usr/bin/env python3
"""MCP (Model Context Protocol) server exposing a BAXI / BDR thermostat to an AI agent.

Transport: stdio, newline-delimited JSON-RPC 2.0 (the format Claude Desktop,
Cursor, and most MCP clients speak).

Register it, e.g. in claude_desktop_config.json:

    {
      "mcpServers": {
        "baxi": {
          "command": "python3",
          "args": ["/absolute/path/to/baxi_mcp.py"]
        }
      }
    }

Run `python3 baxi.py login` once first so a refresh token exists.
Set BAXI_TOKEN_FILE if you keep the token somewhere else.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any, Dict, Optional

from baxi_api import (
    AuthError,
    BaxiCloud,
    BaxiError,
    DEFAULT_ON_TEMPERATURE,
    DEFAULT_TOKEN_FILE,
)

SERVER_INFO = {"name": "baxi-thermostat", "version": "1.0.0"}
PROTOCOL_VERSION = "2024-11-05"

TOKEN_FILE = os.environ.get("BAXI_TOKEN_FILE", str(DEFAULT_TOKEN_FILE))
BRAND = os.environ.get("BAXI_BRAND", "baxi")


def cloud() -> BaxiCloud:
    c = BaxiCloud(brand=BRAND, token_file=TOKEN_FILE)
    if not c.load_token():
        raise AuthError(
            "No stored credentials. Run `python3 baxi.py login` once on this machine."
        )
    return c


# --- tool implementations -----------------------------------------------------


def _zone_or_raise(c: BaxiCloud, zone_index: int) -> Dict[str, Any]:
    entry = c.find_zone(zone_index)
    return entry


def _check_temperature(z: Dict[str, Any], temp: float) -> None:
    lo, hi = z.get("setPointMin"), z.get("setPointMax")
    if lo is not None and hi is not None and not (lo <= temp <= hi):
        raise BaxiError(f"Temperature must be between {lo} and {hi} C for this zone")


def tool_status(args: Dict[str, Any]) -> str:
    c = cloud()
    data = c.dashboard()
    out = []
    for app in data.get("appliances", []):
        out.append(
            {
                "applianceType": app.get("applianceType"),
                "online": app.get("applianceOnline"),
                "operatingMode": app.get("operatingMode"),
                "waterPressure": app.get("waterPressure"),
                "outdoorTemperature": app.get("outdoorTemperature"),
                "zones": app.get("climateZones", []),
                "hotWater": app.get("hotWaterZones", []),
            }
        )
    return json.dumps({"appliances": out}, indent=2, ensure_ascii=False)


def tool_zones(args: Dict[str, Any]) -> str:
    c = cloud()
    zones = []
    for i, entry in enumerate(c.zones()):
        z = entry["zone"]
        zones.append(
            {
                "index": i,
                "climateZoneId": z.get("climateZoneId"),
                "name": z.get("name"),
                "mode": z.get("zoneMode"),
                "roomTemperature": z.get("roomTemperature"),
                "setPoint": z.get("setPoint"),
                "min": z.get("setPointMin"),
                "max": z.get("setPointMax"),
            }
        )
    return json.dumps(zones, indent=2, ensure_ascii=False)


def tool_set_temperature(args: Dict[str, Any]) -> str:
    temp = float(args["temperature"])
    mode = args.get("mode", "override")
    zone_index = int(args.get("zone", 0))
    c = cloud()
    entry = _zone_or_raise(c, zone_index)
    z = entry["zone"]
    _check_temperature(z, temp)
    zone_id = z["climateZoneId"]
    if mode == "manual":
        c.set_manual(zone_id, temp)
        action = f"manual hold -> {temp} C (program paused)"
    else:
        c.set_override(zone_id, temp)
        action = f"temporary override -> {temp} C (until the next program change, program stays on)"
    return f"OK: {action} in zone '{z.get('name')}' ({z.get('climateZoneId')})."


def tool_turn_on(args: Dict[str, Any]) -> str:
    temp = float(args.get("temperature", DEFAULT_ON_TEMPERATURE))
    zone_index = int(args.get("zone", 0))
    c = cloud()
    entry = _zone_or_raise(c, zone_index)
    z = entry["zone"]
    _check_temperature(z, temp)
    program = int(args.get("program") or z.get("activeHeatingClimateTimeProgramNumber") or 1)
    c.turn_on(z["climateZoneId"], temp, program)
    return (
        f"OK: heating on in zone '{z.get('name')}' -> {temp} C until the next program "
        f"change (program {program} stays on)."
    )


def tool_set_mode(args: Dict[str, Any]) -> str:
    mode = args["mode"]
    zone_index = int(args.get("zone", 0))
    c = cloud()
    entry = _zone_or_raise(c, zone_index)
    z = entry["zone"]
    zone_id = z["climateZoneId"]
    if mode == "off":
        c.set_off(zone_id)
        return f"OK: zone '{z.get('name')}' is off (anti-frost)."
    if mode in ("auto", "schedule"):
        program = int(args.get("program") or z.get("activeHeatingClimateTimeProgramNumber") or 1)
        c.set_schedule(zone_id, program)
        return f"OK: zone '{z.get('name')}' follows weekly program {program}."
    if mode == "manual":
        if args.get("temperature") is None:
            raise BaxiError("mode=manual needs a temperature")
        c.set_manual(zone_id, float(args["temperature"]))
        return f"OK: zone '{z.get('name')}' holds {args['temperature']} C."
    raise BaxiError(f"Unknown mode '{mode}'. Use auto, manual, or off.")


TOOLS = [
    {
        "name": "baxi_status",
        "description": (
            "Read the current state of the Baxi heating system: room temperature, "
            "current setpoint, zone mode, water pressure, outdoor temperature, hot water."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": tool_status,
    },
    {
        "name": "baxi_list_zones",
        "description": "List heating zones with their index, id, name, allowed temperature range and current mode.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": tool_zones,
    },
    {
        "name": "baxi_set_temperature",
        "description": (
            "Set the target temperature of a heating zone. Default mode 'override' changes the "
            "temperature until the next programmed step, keeping the weekly program active; "
            "use 'manual' only to hold the temperature indefinitely (pauses the program)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "temperature": {"type": "number", "description": "Target in Celsius"},
                "mode": {"type": "string", "enum": ["manual", "override"], "default": "override"},
                "zone": {"type": "integer", "default": 0, "description": "Zone index"},
            },
            "required": ["temperature"],
            "additionalProperties": False,
        },
        "handler": tool_set_temperature,
    },
    {
        "name": "baxi_turn_on",
        "description": (
            "Turn the heating on when it is off or idle. Uses 22.5 C by default and keeps the "
            "weekly program active: the setpoint is a temporary override, so the program takes "
            "over again at the next switch point. Use this when the user says 'turn on the "
            "heating' / 'enciende la calefaccion' without naming a temperature."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "temperature": {
                    "type": "number",
                    "description": f"Override temperature in Celsius (default {DEFAULT_ON_TEMPERATURE})",
                },
                "zone": {"type": "integer", "default": 0, "description": "Zone index"},
                "program": {
                    "type": "integer",
                    "description": "Weekly program to re-enable (default: the zone's active program)",
                },
            },
            "additionalProperties": False,
        },
        "handler": tool_turn_on,
    },
    {
        "name": "baxi_set_mode",
        "description": (
            "Change a zone mode: 'auto' to follow the weekly program, 'off' for anti-frost, "
            "'manual' to hold a fixed temperature (requires temperature)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["auto", "manual", "off"]},
                "temperature": {"type": "number"},
                "zone": {"type": "integer", "default": 0},
                "program": {"type": "integer", "description": "Heating program id for auto mode"},
            },
            "required": ["mode"],
            "additionalProperties": False,
        },
        "handler": tool_set_mode,
    },
]


# --- JSON-RPC plumbing --------------------------------------------------------


def _send(payload: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _result(msg_id, result: Any) -> None:
    _send({"jsonrpc": "2.0", "id": msg_id, "result": result})


def _error(msg_id, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})


def handle(msg: Dict[str, Any]) -> None:
    method = msg.get("method")
    msg_id = msg.get("id")

    if method == "initialize":
        _result(
            msg_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            },
        )
    elif method == "tools/list":
        _result(
            msg_id,
            {
                "tools": [
                    {k: t[k] for k in ("name", "description", "inputSchema")}
                    for t in TOOLS
                ]
            },
        )
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = next((t for t in TOOLS if t["name"] == name), None)
        if tool is None:
            _result(
                msg_id,
                {
                    "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
                    "isError": True,
                },
            )
            return
        try:
            text = tool["handler"](args)
            _result(msg_id, {"content": [{"type": "text", "text": text}], "isError": False})
        except (BaxiError, AuthError, KeyError, ValueError, TypeError) as e:
            _result(
                msg_id,
                {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True},
            )
    elif method in ("notifications/initialized", "notifications/cancelled"):
        return  # notifications get no reply
    elif method == "ping":
        _result(msg_id, {})
    elif msg_id is not None:
        _error(msg_id, -32601, f"Method not found: {method}")


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        try:
            handle(msg)
        except Exception:  # never let one bad call kill the server
            traceback.print_exc(file=sys.stderr)
            if msg.get("id") is not None:
                _error(msg["id"], -32603, "Internal error")
    return 0


if __name__ == "__main__":
    sys.exit(main())
