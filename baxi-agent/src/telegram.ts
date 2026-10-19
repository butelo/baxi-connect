/** Thin Telegram Bot API helpers. */

const API = "https://api.telegram.org/bot";

/** Telegram caps messages at 4096 characters; split on paragraph/line boundaries. */
function chunk(text: string, size = 3900): string[] {
  if (text.length <= size) return [text];
  const parts: string[] = [];
  let rest = text;
  while (rest.length > size) {
    let cut = rest.lastIndexOf("\n\n", size);
    if (cut < size * 0.5) cut = rest.lastIndexOf("\n", size);
    if (cut < size * 0.5) cut = rest.lastIndexOf(" ", size);
    if (cut <= 0) cut = size;
    parts.push(rest.slice(0, cut));
    rest = rest.slice(cut).trimStart();
  }
  if (rest) parts.push(rest);
  return parts;
}

export async function sendMessage(token: string, chatId: number, text: string): Promise<void> {
  for (const part of chunk(text || "(empty response)")) {
    const res = await fetch(`${API}${token}/sendMessage`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        chat_id: chatId,
        text: part,
        disable_web_page_preview: true,
      }),
    });
    if (!res.ok) {
      // Never let a Telegram failure mask the real error in logs.
      console.error("telegram sendMessage failed", res.status, await res.text());
    }
  }
}

/** Reaction on a specific message, used as a lightweight "seen / done" receipt. */
export async function setReaction(
  token: string,
  chatId: number,
  messageId: number,
  emoji: string,
): Promise<void> {
  const res = await fetch(`${API}${token}/setMessageReaction`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      chat_id: chatId,
      message_id: messageId,
      reaction: emoji ? [{ type: "emoji", emoji }] : [],
    }),
  });
  if (!res.ok) {
    console.error("telegram setMessageReaction failed", res.status, await res.text());
  }
}

export async function sendChatAction(
  token: string,
  chatId: number,
  action: "typing" | "upload_document" = "typing",
): Promise<void> {
  await fetch(`${API}${token}/sendChatAction`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, action }),
  }).catch(() => undefined);
}

/** Register the webhook. Call once after deploy. */
export async function setWebhook(
  token: string,
  url: string,
  secret: string,
): Promise<unknown> {
  const res = await fetch(`${API}${token}/setWebhook`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      url,
      secret_token: secret,
      allowed_updates: ["message"],
      drop_pending_updates: true,
    }),
  });
  return res.json();
}
