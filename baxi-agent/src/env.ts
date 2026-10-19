import type { Assistant } from "./assistant";

export interface Env {
  /** Workers AI binding (also routes vendor models through AI Gateway). */
  AI: Ai;
  /** The Durable Object hosting the Pi harness. */
  Assistant: DurableObjectNamespace<Assistant>;
  TELEGRAM_BOT_TOKEN: string;
  /** Random string Telegram sends back in X-Telegram-Bot-Api-Secret-Token. */
  TELEGRAM_SECRET: string;
  /** Refresh token produced by `python3 baxi.py login` on your laptop. */
  BAXI_REFRESH_TOKEN: string;
  /** Comma-separated Telegram chat ids allowed to control the heating. */
  ALLOWED_CHAT_IDS?: string;
  /** Optional model override, e.g. "@cf/zai-org/glm-4.7-flash". */
  MODEL?: string;
}
