#!/usr/bin/env bash
# Run wrangler with credentials stored inside the project, so no shell
# environment has to be set up by hand.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export XDG_CONFIG_HOME="$ROOT/baxi-agent/.wrangler-home"
export WRANGLER_LOG_PATH="$XDG_CONFIG_HOME/logs"
export WRANGLER_REGISTRY_PATH="$XDG_CONFIG_HOME/registry"
mkdir -p "$WRANGLER_LOG_PATH" "$WRANGLER_REGISTRY_PATH"

cd "$ROOT/baxi-agent"
exec npx --no-install wrangler "$@"
