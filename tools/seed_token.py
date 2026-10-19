#!/usr/bin/env python3
"""Seed the Worker's local dev vars from the refresh token you already have.

    python3 tools/seed_token.py            # print the refresh token
    python3 tools/seed_token.py --write    # create baxi-agent/.dev.vars (asks for the bot token)
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKEN_FILE = ROOT / ".baxi_token.json"
DEV_VARS = ROOT / "baxi-agent" / ".dev.vars"
DEFAULT_MODEL = "@cf/zai-org/glm-4.7-flash"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true", help="write baxi-agent/.dev.vars")
    ap.add_argument("--bot-token", default=os.environ.get("TELEGRAM_BOT_TOKEN"))
    ap.add_argument("--model", default=DEFAULT_MODEL)
    args = ap.parse_args()

    if not TOKEN_FILE.exists():
        print("No .baxi_token.json found. Run `python3 baxi.py login` first.", file=sys.stderr)
        return 1

    token = json.loads(TOKEN_FILE.read_text()).get("refresh_token")
    if not token:
        print("The token file has no refresh_token. Log in again.", file=sys.stderr)
        return 1

    if not args.write:
        print(token)
        return 0

    bot = args.bot_token or input("Telegram bot token (from @BotFather): ").strip()
    secret = secrets.token_urlsafe(32)
    DEV_VARS.write_text(
        f"TELEGRAM_BOT_TOKEN={bot}\n"
        f"TELEGRAM_SECRET={secret}\n"
        f"BAXI_REFRESH_TOKEN={token}\n"
        f"ALLOWED_CHAT_IDS=\n"
        f"MODEL={args.model}\n"
    )
    try:
        os.chmod(DEV_VARS, 0o600)
    except OSError:
        pass

    print(f"Wrote {DEV_VARS}")
    print("\nNext:")
    print("  cd baxi-agent && npm run dev        # local test")
    print("  npx wrangler deploy                 # production")
    print("\nThen push the same three secrets to production:")
    print(f"  npx wrangler secret put TELEGRAM_BOT_TOKEN")
    print(f"  npx wrangler secret put TELEGRAM_SECRET   # {secret}")
    print(f"  npx wrangler secret put BAXI_REFRESH_TOKEN")
    print("  npx wrangler secret put ALLOWED_CHAT_IDS   # your chat id, from the bot's first reply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
