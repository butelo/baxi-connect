#!/usr/bin/env bash
# Rotate the Telegram bot token without pasting it into any chat.
#
#   1. BotFather -> /revoke -> copy the new token
#   2. run: tools/rotate_bot_token.sh
#   3. paste it at the hidden prompt (nothing is echoed, nothing is logged)
#
# The webhook does not need re-registering: it belongs to the bot, not the token.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

read -rsp "New bot token (hidden): " TOKEN
echo
if [[ -z "${TOKEN}" ]]; then
  echo "Empty token, aborting." >&2
  exit 1
fi

printf '%s' "$TOKEN" | "$ROOT/tools/wr.sh" secret put TELEGRAM_BOT_TOKEN
unset TOKEN
echo "Token rotated. Send the bot a message to confirm."
