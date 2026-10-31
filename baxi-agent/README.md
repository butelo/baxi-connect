# baxi-agent — Telegram bot on Pi Durable + Cloudflare

A durable agent that controls the flat's BAXI heating from Telegram, running entirely on
Cloudflare: one Worker, one Durable Object, no servers.

```
Telegram ──► Worker /telegram ──► Durable Object (Pi Durable harness)
                                        │  transcript, inbox, token cache (SQLite)
                                        └─► api.bdrthermea.net ──► thermostat ──► boiler
```

* **Pi Durable** (`PiHarness` from the Agents SDK) runs the agent loop — model calls,
  tool calls, retries, crash recovery. An evicted object wakes up and finishes the turn.
* **The Durable Object** holds one Pi session per Telegram chat and the OAuth token.
* **Tools** (`baxi_status`, `baxi_set_temperature`, `baxi_set_mode`) call the same BDR
  cloud API the official app uses.

## Auth: a refresh token, not your password

The B2C login needs cookies and a browser-shaped redirect dance. Doing that in a Worker is
possible but fragile, so it is done **once on your laptop** by `../baxi.py login`; the
Worker only ever performs the refresh grant:

1. `BAXI_REFRESH_TOKEN` secret seeds the object on first use.
2. The token is stored in the object's SQLite storage and reused until ~60 s before expiry.
3. B2C **rotates** the refresh token on every refresh; the new one is written back. If you
   ever see `invalid_grant`, re-run `python3 ../baxi.py login` and re-seed.

## Setup

**1. Create the bot** — talk to [@BotFather](https://t.me/BotFather), `/newbot`, keep the token.

**2. Seed local vars**

```bash
cd ..                                   # repo root, where .baxi_token.json lives
python3 tools/seed_token.py --write     # asks for the bot token, writes baxi-agent/.dev.vars
```

**3. Try it locally** (optional)

```bash
cd baxi-agent
npm install
npm run dev
```

**4. Deploy**

```bash
npx wrangler login      # once
npx wrangler deploy
```

**5. Push the secrets and register the webhook**

```bash
cd .. && WORKER_URL=https://baxi-agent.<your-subdomain>.workers.dev/telegram ./tools/setup_bot.sh
```

It validates the token with `getMe`, pushes `TELEGRAM_BOT_TOKEN` and `TELEGRAM_SECRET`,
and calls `setWebhook` with the secret header — then prints `getWebhookInfo` so you can see
it worked.

Still to push by hand, because they are not Telegram's:

```bash
npx wrangler secret put BAXI_REFRESH_TOKEN   # from tools/seed_token.py
```

**6. Claim your chat** — message the bot. If no chat is allowed yet it answers with its own
`chat_id`; then, from the repo root:

```bash
./tools/set_allowed.sh 123456789        # the id the bot just gave you
```

**7. Add a second phone** (a partner, a flatmate) — pass every id you want to keep:

```bash
./tools/set_allowed.sh 123456789 987654321
```

The script always sets the whole list (Cloudflare secrets are write-only, so there is
nothing to read back), updates `baxi-agent/.dev.vars` for `npm run dev`, and pushes the
secret. The id itself is one message away: anyone not on the list gets a short reply with
their own `chat_id` and nothing else, and `/id` repeats it for allowed chats.

Everyone else can find the bot, but cannot touch the heating. This matters: a public bot
without an allowlist lets strangers heat your flat.

## Talking to it

> ¿a cuánto está el salón? · ponlo a 21 · sube dos grados · apágalo · vuelve al programa

The agent reads `baxi_status` before writing, so the numbers it quotes are real.

## Model

`MODEL` defaults to `@cf/zai-org/glm-4.7-flash` (Workers AI). Any Workers AI model works;
vendor models are reachable through **AI Gateway** (enable the provider in the dashboard,
then use ids like `openai/gpt-5`). `createAI` only ever talks through the `AI` binding, so
there is no API key to store — that's the whole point of the integration.

Swap it without touching code:

```bash
npx wrangler secret put MODEL
```

## What is verified, and what is not

Verified: typechecks clean, bundles at 909 KB gzip, the Durable Object and AI bindings
attach, and the TypeScript BDR client was run against the live cloud (dashboard read,
token refresh, rotation persisted to storage).

Not verified (needs your account): an actual Telegram round trip and an actual model call.
Those need the bot token, the Cloudflare deploy, and your provider choice.

## Caveats

* `PiHarness` is **beta** — the API may change between releases; pin the versions.
* The BDR API is unofficial and can change (they already retired one backend).
* If the Worker is evicted mid-run, Pi resumes the work; the Telegram reply itself is sent
  after the turn completes, so a rare crash can lose one reply.
* One Durable Object per chat, SQLite-backed: a handful of rows, well inside the free tier.

## Files

| File | |
|---|---|
| `src/index.ts` | Worker: Telegram webhook, secret check, chat allowlist, routing |
| `src/assistant.ts` | Durable Object: Pi harness, system prompt, thermostat tools |
| `src/baxi.ts` | BDR cloud client (refresh grant + API), token cached in DO storage |
| `src/telegram.ts` | `sendMessage`, `sendChatAction`, message chunking |
| `wrangler.jsonc` | Worker, DO (SQLite class), AI binding |
