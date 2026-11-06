#!/usr/bin/env python3
"""Command line control for a BAXI / BDR Thermea thermostat (no official app needed).

    python3 baxi.py login                 # once, stores a refresh token locally
    python3 baxi.py status                # current temperatures, setpoint, mode
    python3 baxi.py set 21.5              # 21.5 C until the next program step
    python3 baxi.py set 21.5 --mode manual  # hold 21.5 C, ignoring the program
    python3 baxi.py off                   # anti-frost
    python3 baxi.py auto                  # back to the weekly program
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys

from baxi_api import AuthError, BaxiCloud, BaxiError, DEFAULT_TOKEN_FILE


def _client(args) -> BaxiCloud:
    return BaxiCloud(brand=args.brand, token_file=args.token_file, lang=args.lang)


def cmd_login(args) -> int:
    email = args.email or os.environ.get("BAXI_EMAIL") or input("Baxi account email: ")
    password = (
        args.password
        or os.environ.get("BAXI_PASSWORD")
        or getpass.getpass("Password (not echoed, not stored): ")
    )
    cloud = _client(args)
    cloud.login(email, password)
    cloud.save_token()
    print(f"Logged in. Token saved to {cloud.token_file}")
    try:
        _print_status(cloud)
    except BaxiError as e:
        print(f"\nLogin worked, but reading the dashboard failed: {e}")
    return 0


def _fmt_zone(z: dict, idx: int) -> str:
    mode = z.get("zoneMode", "?")
    cur = z.get("roomTemperature")
    tgt = z.get("setPoint")
    lo, hi = z.get("setPointMin"), z.get("setPointMax")
    lines = [
        f"[{idx}] {z.get('name', 'zone')}  ({z.get('zoneType', 'CH')})",
        f"    mode          : {mode}",
        f"    room          : {cur}\u00b0C",
        f"    setpoint      : {tgt}\u00b0C   (allowed {lo}-{hi})",
    ]
    if z.get("nextSetpoint") is not None:
        lines.append(f"    next change   : {z.get('nextSetpoint')}\u00b0C at {z.get('nextSwitchTime')}")
    if z.get("currentScheduleSetPoint") is not None:
        lines.append(f"    program says  : {z.get('currentScheduleSetPoint')}\u00b0C")
    if z.get("firePlaceModeActive"):
        lines.append("    fireplace mode: ON")
    return "\n".join(lines)


def _print_status(cloud: BaxiCloud) -> None:
    data = cloud.dashboard()
    for app in data.get("appliances", []):
        print(
            f"Appliance {app.get('applianceType')} | online={app.get('applianceOnline')} "
            f"| {app.get('applianceConnectionStatus')} | mode={app.get('operatingMode')}"
        )
        if app.get("waterPressure") is not None:
            pressure = app["waterPressure"]
            ok = "" if app.get("waterPressureOK", True) else "  <-- CHECK"
            print(f"  water pressure: {pressure} bar{ok}")
        if app.get("outdoorTemperature") is not None:
            print(f"  outdoor temp  : {app['outdoorTemperature']}\u00b0C")
        for i, z in enumerate(app.get("climateZones", [])):
            print(_fmt_zone(z, i))
        for hw in app.get("hotWaterZones", []):
            print(
                f"  DHW {hw.get('name', '')}: {hw.get('dhwTemperature')}\u00b0C "
                f"(target {hw.get('targetSetpoint')}\u00b0C, mode {hw.get('dhwZoneMode')})"
            )


def cmd_status(args) -> int:
    cloud = _client(args)
    if args.json:
        print(json.dumps(cloud.dashboard(), indent=2, ensure_ascii=False))
        return 0
    _print_status(cloud)
    return 0


def cmd_set(args) -> int:
    cloud = _client(args)
    zone = cloud.find_zone(args.zone)
    zone_id = zone["zone"]["climateZoneId"]
    zmin = zone["zone"].get("setPointMin")
    zmax = zone["zone"].get("setPointMax")
    if zmin is not None and zmax is not None and not (zmin <= args.temperature <= zmax):
        print(
            f"Refusing {args.temperature}\u00b0C: this zone allows {zmin}-{zmax}\u00b0C",
            file=sys.stderr,
        )
        return 2
    if args.mode == "manual":
        cloud.set_manual(zone_id, args.temperature)
        print(f"Manual mode: holding {args.temperature}\u00b0C, the program is paused.")
    else:
        cloud.set_override(zone_id, args.temperature)
        print(
            f"Temporary override set to {args.temperature}\u00b0C until the next "
            f"program change (program stays on)."
        )
    return 0


def cmd_off(args) -> int:
    cloud = _client(args)
    zone = cloud.find_zone(args.zone)
    cloud.set_off(zone["zone"]["climateZoneId"])
    print("Zone set to off / anti-frost.")
    return 0


def cmd_auto(args) -> int:
    cloud = _client(args)
    zone = cloud.find_zone(args.zone)
    z = zone["zone"]
    program = args.program or z.get("activeHeatingClimateTimeProgramNumber") or 1
    cloud.set_schedule(z["climateZoneId"], program)
    print(f"Back to the weekly program (program {program}).")
    return 0


def cmd_program(args) -> int:
    cloud = _client(args)
    zone = cloud.find_zone(args.zone)
    cloud.activate_program(zone["zone"]["climateZoneId"], args.program_id)
    print(f"Activated heating program {args.program_id}.")
    return 0


def cmd_dhw(args) -> int:
    cloud = _client(args)
    data = cloud.dashboard()
    zones = [hw for a in data.get("appliances", []) for hw in a.get("hotWaterZones", [])]
    if not zones:
        raise BaxiError("No hot water zone in this account")
    cloud.set_hot_water(zones[args.zone]["hotWaterZoneId"], args.comfort, args.reduced)
    print("Hot water setpoints updated.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--brand", default="baxi", help="baxi (default), remeha, dedietrich")
    common.add_argument("--lang", default="es")
    common.add_argument("--token-file", default=str(DEFAULT_TOKEN_FILE))

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    lp = sub.add_parser("login", parents=[common], help="authenticate and cache a token")
    lp.add_argument("--email")
    lp.add_argument("--password")
    lp.set_defaults(func=cmd_login)

    sp = sub.add_parser("status", parents=[common], help="show current values")
    sp.add_argument("--json", action="store_true", help="raw dashboard JSON")
    sp.set_defaults(func=cmd_status)

    st = sub.add_parser("set", parents=[common], help="set a temperature")
    st.add_argument("temperature", type=float)
    st.add_argument("--zone", type=int, default=0)
    st.add_argument(
        "--mode",
        choices=["manual", "override"],
        default="override",
        help="override (default): until the next program step, keeping the program on; "
        "manual: hold the temperature, pausing the program",
    )
    st.set_defaults(func=cmd_set)

    off = sub.add_parser("off", parents=[common], help="turn the zone off (anti-frost)")
    off.add_argument("--zone", type=int, default=0)
    off.set_defaults(func=cmd_off)

    auto = sub.add_parser("auto", parents=[common], help="return to the weekly program")
    auto.add_argument("--zone", type=int, default=0)
    auto.add_argument("--program", type=int)
    auto.set_defaults(func=cmd_auto)

    prog = sub.add_parser("program", parents=[common], help="activate a heating program")
    prog.add_argument("program_id", type=int)
    prog.add_argument("--zone", type=int, default=0)
    prog.set_defaults(func=cmd_program)

    dhw = sub.add_parser("dhw", parents=[common], help="set hot water setpoints")
    dhw.add_argument("--zone", type=int, default=0)
    dhw.add_argument("--comfort", type=float)
    dhw.add_argument("--reduced", type=float)
    dhw.set_defaults(func=cmd_dhw)
    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except AuthError as e:
        print(f"Auth error: {e}", file=sys.stderr)
        return 1
    except BaxiError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
