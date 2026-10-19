#!/usr/bin/env bash
# Call any Telegram Bot API method without pasting your token into a chat.
#
#   ./tools/tg.sh                    # getWebhookInfo
#   ./tools/tg.sh getMe
#   ./tools/tg.sh deleteWebhook
#
set -euo pipefail

read -rsp "Bot token (hidden): " T
echo
T="${T//[[:space:]]/}"
T="${T#bot}"
[[ -z "${T}" ]] && { echo "empty token" >&2; exit 1; }

METHOD="${1:-getWebhookInfo}"
curl -s "https://api.telegram.org/bot${T}/${METHOD}" | python3 -m json.tool
unset T
