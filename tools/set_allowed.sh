#!/usr/bin/env bash
# Choose who may talk to the Telegram bot. Everyone else is refused.
#
#   ./tools/set_allowed.sh 123456789               # just you
#   ./tools/set_allowed.sh 123456789 987654321     # you and your wife
#   ./tools/set_allowed.sh --list                  # what is configured here
#
# Chat ids are the numbers the bot itself replies with: message it from an
# unlisted phone and it answers with that phone's chat id; /id repeats it.
#
# The value is written to baxi-agent/.dev.vars (so `npm run dev` matches) and
# pushed to the Worker as the ALLOWED_CHAT_IDS secret. Cloudflare secrets are
# write-only, so this always sets the whole list.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/baxi-agent/.dev.vars"

[[ -f "$ENV_FILE" ]] || {
  echo "Missing $ENV_FILE — run: python3 tools/seed_token.py --write" >&2
  exit 1
}

current() {
  grep -E '^ALLOWED_CHAT_IDS=' "$ENV_FILE" | head -1 | cut -d= -f2- || true
}

if [[ "${1:-}" == "--list" ]]; then
  value="$(current)"
  echo "baxi-agent/.dev.vars: ${value:-<empty>}"
  exit 0
fi

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <chat-id> [chat-id ...]   (see --list)" >&2
  exit 1
fi

IDS=()
for raw in "$@"; do
  id="${raw//[[:space:],]/}"
  id="${id#[Cc][Hh][Aa][Tt][Ii][Dd]:}"
  [[ "$id" =~ ^-?[0-9]{4,20}$ ]] || {
    echo "Not a chat id: '$raw' — expected a number like 123456789." >&2
    exit 1
  }
  IDS+=("$id")
done
VALUE="$(IFS=,; echo "${IDS[*]}")"

python3 - "$ENV_FILE" "$VALUE" <<'PY'
import sys

path, value = sys.argv[1], sys.argv[2]
key = "ALLOWED_CHAT_IDS"
lines = open(path, encoding="utf-8").read().splitlines(True)
out, seen = [], False
for line in lines:
    if line.startswith(key + "="):
        out.append(f"{key}={value}\n")
        seen = True
    else:
        out.append(line)
if not seen:
    out.append(f"{key}={value}\n")
open(path, "w", encoding="utf-8").write("".join(out))
PY

printf '%s' "$VALUE" | "$ROOT/tools/wr.sh" secret put ALLOWED_CHAT_IDS

echo "Allowed chats: ${VALUE}"
echo "Everyone else is refused."
