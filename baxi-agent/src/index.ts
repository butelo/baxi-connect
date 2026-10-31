import type { Env } from "./env";
import { Assistant } from "./assistant";
import { sendMessage } from "./telegram";

export { Assistant };

interface TelegramMessage {
  text?: string;
  message_id?: number;
  chat?: { id?: number };
}

interface TelegramUpdate {
  message?: TelegramMessage;
  edited_message?: TelegramMessage;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/") {
      return Response.json({ ok: true, service: "baxi-agent" });
    }

    if (request.method === "POST" && url.pathname === "/telegram") {
      const secret = request.headers.get("x-telegram-bot-api-secret-token");
      if (!env.TELEGRAM_SECRET || secret !== env.TELEGRAM_SECRET) {
        return new Response("forbidden", { status: 403 });
      }

      let update: TelegramUpdate;
      try {
        update = (await request.json()) as TelegramUpdate;
      } catch {
        return new Response("bad request", { status: 400 });
      }

      const message = update.message ?? update.edited_message;
      const text = message?.text;
      const chatId = message?.chat?.id;
      const messageId = message?.message_id;
      if (!text || typeof chatId !== "number") {
        return Response.json({ ok: true }); // stickers, joins, etc.
      }

      // Anyone can find a public bot, so only listed chats may touch the heating.
      const allowed = (env.ALLOWED_CHAT_IDS ?? "")
        .split(",")
        .map((id) => id.trim())
        .filter(Boolean);
      if (allowed.length === 0) {
        await sendMessage(
          env.TELEGRAM_BOT_TOKEN,
          chatId,
          `Not configured yet. Add this to the Worker:\n\nALLOWED_CHAT_IDS=${chatId}\n\n` +
            `or run: ./tools/set_allowed.sh ${chatId}`,
        );
        return Response.json({ ok: true });
      }
      if (!allowed.includes(String(chatId))) {
        // Silence reads as "the bot is broken", so answer once with the only
        // thing the owner needs in order to let this person in. No model call,
        // no heating access: the door stays shut until the id is configured.
        await sendMessage(
          env.TELEGRAM_BOT_TOKEN,
          chatId,
          `Este bot es privado: todavía no puedes controlar la calefacción.\n\n` +
            `Tu chat id es ${chatId}\n` +
            `Pásaselo a quien lo administre y te añadirá.\n\n` +
            `Your chat id is ${chatId}`,
        );
        return Response.json({ ok: true });
      }

      // One Durable Object per Telegram chat: its own Pi session and token cache.
      const stub = env.Assistant.get(env.Assistant.idFromName(`tg:${chatId}`));
      await stub.enqueue({ chatId, text, messageId });
      return Response.json({ ok: true });
    }

    return new Response("not found", { status: 404 });
  },
} satisfies ExportedHandler<Env>;
