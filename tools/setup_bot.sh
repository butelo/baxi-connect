#!/usr/bin/env bash
# One-shot Telegram setup: validate the bot token, push it to the Worker,
# register the webhook, and print the result.
#
#   WORKER_URL=https://baxi-agent.<subdomain>.workers.dev/telegram ./tools/setup_bot.sh
#
# The token is read at a hidden prompt. The webhook secret is taken from
# baxi-agent/.dev.vars (created by tools/seed_token.py --write).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/baxi-agent/.dev.vars"

[[ -f "$ENV_FILE" ]] || {
  echo "Missing $ENV_FILE — run: python3 tools/seed_token.py --write" >&2
  exit 1
}

read_var() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2-; }
SECRET="$(read_var TELEGRAM_SECRET)"
[[ -n "$SECRET" ]] || { echo "TELEGRAM_SECRET not found in .dev.vars" >&2; exit 1; }

WORKER_URL="${WORKER_URL:-}"
if [[ -z "$WORKER_URL" ]]; then
  read -rp "Worker webhook URL (https://<worker>.<subdomain>.workers.dev/telegram): " WORKER_URL
fi
[[ -n "$WORKER_URL" ]] || { echo "No webhook URL" >&2; exit 1; }

read -rsp "Bot token (hidden): " RAW
echo

# Tolerate a paste with the "bot" prefix or stray whitespace.
TOKEN="${RAW//[[:space:]]/}"
TOKEN="${TOKEN#bot}"

if [[ ! "${TOKEN}" =~ ^[0-9]{6,}:[A-Za-z0-9_-]{30,}$ ]]; then
  echo "That does not look like a bot token (got ${#TOKEN} chars)." >&2
  echo "BotFather -> /mybots -> your bot -> API Token." >&2
  exit 1
fi

echo "--- getMe ---"
curl -s "https://api.telegram.org/bot${TOKEN}/getMe" | tee /tmp/tg_me.json | python3 -m json.tool
grep -Eq '"ok" *: *true' /tmp/tg_me.json || { echo "Telegram rejected that token." >&2; exit 1; }

echo "--- pushing secrets ---"
printf '%s' "${TOKEN}" | "${ROOT}/tools/wr.sh" secret put TELEGRAM_BOT_TOKEN
printf '%s' "${SECRET}" | "${ROOT}/tools/wr.sh" secret put TELEGRAM_SECRET

echo "--- setWebhook ---"
curl -s -X POST "https://api.telegram.org/bot${TOKEN}/setWebhook" \
  -H "content-type: application/json" \
  -d "{\"url\":\"${WORKER_URL}\",\"secret_token\":\"${SECRET}\",\"allowed_updates\":[\"message\"],\"drop_pending_updates\":true}" \
  | python3 -m json.tool

echo "--- getWebhookInfo ---"
curl -s "https://api.telegram.org/bot${TOKEN}/getWebhookInfo" | python3 -m json.tool

unset RAW TOKEN SECRET
echo "Done. Message the bot; it replies with your chat id, then set ALLOWED_CHAT_IDS."
