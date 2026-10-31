import { Agent } from "agents";
import { Type } from "@earendil-works/pi-ai";
import { createModels } from "@earendil-works/pi-ai/models";
import { createRegistry, Harness, type ToolRegistration } from "@earendil-works/pi-durable";
import { PiHarness } from "agents/harness/pi";
import { createAI } from "agents/models/pi-ai";

import {
  BaxiClient,
  flattenZones,
  refreshTokens,
  TOKEN_KEY,
  TokenRefreshError,
  type StoredToken,
  type TokenSource,
} from "./baxi";
import type { Env } from "./env";
import { sendChatAction, sendMessage, setReaction } from "./telegram";

const DEFAULT_MODEL = "@cf/zai-org/glm-4.7-flash";

const HELP = `I control the BAXI heating in your flat.

Try:
• ¿a cuánto está el salón?  /  what's the living room at?
• ponlo a 21  /  set it to 21
• sube dos grados
• apágalo  /  turn it off
• vuelve al programa  /  back to schedule

Conversation:
• /new — forget this conversation and start fresh
• /new <note> — start fresh but keep a one-line note
• /context — how much history I'm carrying
• /id — your Telegram chat id (needed to allow another phone)

I read before I write, so I'll tell you the real temperature and setpoint.`;

const SYSTEM_PROMPT = `You control a BAXI Connect heating system in the user's home, through the BDR Thermea cloud.

Rules:
- Temperatures are Celsius. Comfort is roughly 19-22 C; never aim below 5 (frost) or above 30.
- Always call baxi_status before changing anything: you need the real zoneId and the current room temperature.
- Change temperature with baxi_set_temperature. mode="override" lasts until the next program step; mode="manual" holds indefinitely. Use "override" for a quick nudge ("sube un poco"), "manual" when the user wants it held ("hasta que vuelva", "keep it at 20").
- baxi_set_mode with "auto" returns to the weekly program, "off" is anti-frost.
- When asked about the schedule, relay the baxi_schedule text block verbatim. Do not build tables, do not merge days, do not infer hours the tool did not list (gaps between switch points are not yours to fill).
- You CAN read and edit the weekly programs: call baxi_schedule first, then baxi_set_switchpoint to move a day/time to another activity. Activity temperatures (Home 15, Sleep 20, Evening 6, Work 19, Comfort 21) are fixed on the device and cannot be changed through the API — say so if asked. For an arbitrary temperature at a moment, use baxi_set_temperature instead, which overrides the program.
- Reply in the language the user writes (Spanish or English), in one or two short sentences, stating the resulting setpoint.
- If a tool fails, say so plainly with the reason. Never claim a change you did not make.`;

interface TelegramJob {
  chatId: number;
  text: string;
  messageId?: number;
}

const SetTemperatureParams = Type.Object({
  temperature: Type.Number({ description: "Target temperature in Celsius" }),
  mode: Type.Optional(
    Type.Union([Type.Literal("manual"), Type.Literal("override")], {
      description: "override = until next program step, manual = hold indefinitely",
    }),
  ),
  zoneId: Type.Optional(Type.String({ description: "Zone id from baxi_status" })),
});

const SwitchPointParams = Type.Object({
  day: Type.Number({ description: "0 = Sunday, 1 = Monday, ... 6 = Saturday" }),
  time: Type.String({ description: "HH:MM in 24h, e.g. 07:30" }),
  activity: Type.Number({
    description: "Activity number from baxi_schedule: 1=Home, 2=Sleep, 3=Evening, 4=Work, 5=Comfort",
  }),
  program: Type.Optional(Type.Number({ description: "Weekly program 1-3 (default 1)" })),
});

const SetModeParams = Type.Object({
  mode: Type.Union([Type.Literal("auto"), Type.Literal("off")]),
  zoneId: Type.Optional(Type.String({ description: "Zone id from baxi_status" })),
  program: Type.Optional(
    Type.Number({ description: "Which weekly program to activate in auto mode (1-3, default 1)" }),
  ),
});

/**
 * One object holds the BDR token for the whole Worker. B2C rotates the refresh
 * token on every use, so two objects refreshing from the same seed would
 * invalidate each other's chain — every chat goes through this one instead.
 */
const TOKEN_VAULT = "baxi:token";

/** Reads the token from the vault object; keeps this chat's old copy as a bridge. */
class VaultTokenSource implements TokenSource {
  constructor(
    private readonly namespace: DurableObjectNamespace<Assistant>,
    private readonly seed: string | undefined,
    private readonly storage: DurableObjectStorage,
  ) {}

  async accessToken(): Promise<string> {
    // Before the vault existed each chat kept its own token. Hand the live one
    // over once, so deploying this does not invalidate the current session.
    const legacy = await this.storage.get<StoredToken>(TOKEN_KEY);
    const vault = this.namespace.get(this.namespace.idFromName(TOKEN_VAULT));
    return vault.getAccessToken(this.seed, legacy);
  }
}

export class Assistant extends Agent<Env> {
  readonly ai = createAI({ binding: this.env.AI });
  readonly registry = createRegistry();
  readonly baxi = new BaxiClient(
    new VaultTokenSource(this.env.Assistant, this.env.BAXI_REFRESH_TOKEN, this.ctx.storage),
  );

  readonly harness = new PiHarness({
    harness: ({ storage, context }) => {
      const models = createModels();
      models.setProvider(this.ai.provider);
      this.registry.install({
        name: "baxi",
        sections: [{ key: "preamble", render: () => SYSTEM_PROMPT, tag: false }],
        tools: this.tools(),
      });
      return Harness.open(storage, { models, registry: this.registry }, context);
    },
    defaults: {
      model: this.ai(this.env.MODEL ?? DEFAULT_MODEL),
      thinkingLevel: "low",
    },
  });

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.lifecycle.use(this.harness);
  }

  private refreshInFlight?: Promise<StoredToken>;

  /**
   * The single owner of the BDR token. Called by every chat's object, so the
   * refresh happens once and the rotated token is persisted here, not per chat.
   * `legacy` is a pre-vault token from the calling chat's own storage.
   */
  async getAccessToken(seed?: string, legacy?: StoredToken): Promise<string> {
    const now = Math.floor(Date.now() / 1000);
    const saved = await this.ctx.storage.get<StoredToken>(TOKEN_KEY);
    if (saved?.access && saved.exp > now + 60) return saved.access;

    // Newest first: the vault's own token, then whatever the calling chat kept
    // before the vault existed, then the deploy-time seed.
    const sources = [
      ...new Set([saved?.refresh, legacy?.refresh, seed].filter((t): t is string => Boolean(t))),
    ];
    if (sources.length === 0) {
      throw new Error(
        "No Baxi credentials. Seed the BAXI_REFRESH_TOKEN secret (see README) and redeploy.",
      );
    }

    // Concurrent callers share one refresh: rotating twice would burn a token.
    this.refreshInFlight ??= this.refreshFrom(sources).finally(() => {
      this.refreshInFlight = undefined;
    });
    const next = await this.refreshInFlight;
    await this.ctx.storage.put(TOKEN_KEY, next);
    return next.access;
  }

  /**
   * Try each credential source in turn, but only move on when B2C says the
   * token is revoked (400 invalid_grant) — that is what a `baxi.py login` on
   * the laptop looks like from here, and the re-seeded secret is newer than
   * whatever this object stored. Anything else must surface untouched.
   */
  private async refreshFrom(sources: string[]): Promise<StoredToken> {
    let lastError: unknown;
    for (const refresh of sources) {
      try {
        return await refreshTokens(refresh);
      } catch (error) {
        if (!(error instanceof TokenRefreshError) || error.status !== 400) throw error;
        lastError = error;
      }
    }
    throw lastError;
  }

  /** Called by the webhook. Returns immediately; the turn runs in the background. */
  async enqueue(job: TelegramJob): Promise<{ ok: true }> {
    this.ctx.waitUntil(this.respond(job));
    return { ok: true };
  }

  private async respond(job: TelegramJob): Promise<void> {
    const { chatId, text, messageId } = job;
    const token = this.env.TELEGRAM_BOT_TOKEN;
    // Instant "seen" receipt: the model may take a while, and silence reads as broken.
    if (messageId) await setReaction(token, chatId, messageId, "\ud83d\udc40");
    try {
      if (text.trim() === "/start" || text.trim() === "/help") {
        await sendMessage(token, chatId, HELP);
        return;
      }

      // Start a new context. Everything before the reset leaves the model's view;
      // pi keeps the old entries on disk but stops sending them.
      const resetMatch = text.match(/^\/(?:new|reset)\b\s*([\s\S]*)$/i);
      if (resetMatch) {
        const handoff = resetMatch[1].trim();
        await this.harness.session().reset(handoff || undefined);
        await sendMessage(
          token,
          chatId,
          handoff
            ? `Nueva conversación. Recordaré esto:\n${handoff}`
            : "Nueva conversación. Contexto anterior descartado.",
        );
        return;
      }

      if (text.trim() === "/id") {
        await sendMessage(token, chatId, `chat id: ${chatId}`);
        return;
      }

      if (text.trim() === "/context") {
        const entries = await this.harness.messages();
        await sendMessage(
          token,
          chatId,
          `${entries.length} entries in this conversation since the last reset.`,
        );
        return;
      }

      await this.runTurn(token, chatId, text);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      console.error("agent turn failed:", message);
      await sendMessage(token, chatId, `⚠️ ${message}`);
    } finally {
      if (messageId) await setReaction(token, chatId, messageId, "\u2705");
    }
  }

  /**
   * Run one model turn, keeping the Telegram "typing…" indicator alive.
   *
   * The indicator Telegram shows lasts ~5 s, so a single sendChatAction at the
   * start goes stale on any slower turn. Pi also serialises prompts per
   * session: a message sent while a turn is running is queued as a follow-up,
   * so the wait can be the previous turn plus this one. A heartbeat every 4 s
   * makes that visible instead of looking dead.
   */
  private async runTurn(token: string, chatId: number, text: string): Promise<void> {
    const started = Date.now();
    let finished = false;
    const heartbeat = (async () => {
      while (!finished) {
        await sendChatAction(token, chatId, "typing");
        await new Promise((resolve) => setTimeout(resolve, 4000));
      }
    })();

    try {
      const busyAtStart = await this.harness.session().busy();
      const result = await this.harness.prompt(text);
      await sendMessage(token, chatId, result.text?.trim() || "No answer from the model.");
      console.log(
        `turn ok in ${Date.now() - started}ms (queuedBehindPreviousTurn=${busyAtStart})`,
      );
    } finally {
      finished = true;
    }
  }

  private async resolveZone(zoneId?: string) {
    const zones = flattenZones(await this.baxi.dashboard());
    if (zones.length === 0) {
      throw new Error("No climate zone in this account — the thermostat is not paired to this cloud.");
    }
    if (!zoneId) return zones[0];
    const match = zones.find((z) => z.zone.climateZoneId === zoneId);
    if (!match) {
      throw new Error(
        `Unknown zoneId. Known zones: ${zones.map((z) => z.zone.climateZoneId).join(", ")}`,
      );
    }
    return match;
  }

  private tools(): ToolRegistration[] {
    const status: ToolRegistration = {
      name: "baxi_status",
      description:
        "Read the live heating state: room temperature, setpoint, mode, allowed range, " +
        "water pressure, outdoor temperature and hot water. Call this before changing anything.",
      parameters: Type.Object({}),
      replay: "safe",
      execute: async () => {
        const dashboard = await this.baxi.dashboard();
        const zones = flattenZones(dashboard);
        const payload = {
          zones: zones.map(({ zone }) => ({
            zoneId: zone.climateZoneId,
            name: zone.name?.trim() || "(unnamed)",
            mode: zone.zoneMode,
            roomTemperature: zone.roomTemperature,
            setpoint: zone.setPoint,
            allowedRange: [zone.setPointMin, zone.setPointMax],
            nextSetpoint: zone.nextSetpoint,
            nextSwitchTime: zone.nextSwitchTime,
            programSetpoint: zone.currentScheduleSetPoint,
          })),
          appliances: dashboard.appliances.map((a) => ({
            type: a.applianceType,
            online: a.applianceOnline,
            operatingMode: a.operatingMode,
            waterPressureBar: a.waterPressure,
            outdoorTemperature: a.outdoorTemperature,
          })),
          hotWater: dashboard.appliances.flatMap((a) =>
            (a.hotWaterZones ?? []).map((hw) => ({
              zoneId: (hw as Record<string, unknown>).hotWaterZoneId,
              temperature: (hw as Record<string, unknown>).dhwTemperature,
              target: (hw as Record<string, unknown>).targetSetpoint,
            })),
          ),
        };
        return { output: [{ type: "text", text: JSON.stringify(payload, null, 2) }] };
      },
    };

    const setTemperature: ToolRegistration<typeof SetTemperatureParams> = {
      name: "baxi_set_temperature",
      description:
        "Set the target temperature of a heating zone. mode=override lasts until the next " +
        "programmed step; mode=manual holds it indefinitely.",
      parameters: SetTemperatureParams,
      replay: "safe",
      execute: async ({ temperature, mode = "override", zoneId }) => {
        const target = await this.resolveZone(zoneId);
        const lo = target.zone.setPointMin ?? 5;
        const hi = target.zone.setPointMax ?? 30;
        if (temperature < lo || temperature > hi) {
          throw new Error(`Temperature must be between ${lo} and ${hi} C for this zone.`);
        }
        const id = target.zone.climateZoneId;
        if (mode === "manual") await this.baxi.setManual(id, temperature);
        else await this.baxi.setOverride(id, temperature);

        const label = target.zone.name?.trim() || "zone";
        return {
          output: [
            {
              type: "text",
              text:
                `Set ${label} to ${temperature} C (${mode}). ` +
                `Room is currently ${target.zone.roomTemperature ?? "?"} C.`,
            },
          ],
        };
      },
    };

    const setMode: ToolRegistration<typeof SetModeParams> = {
      name: "baxi_set_mode",
      description:
        "Set a zone to auto (follow the weekly program) or off (anti-frost).",
      parameters: SetModeParams,
      replay: "safe",
      execute: async ({ mode, zoneId, program }) => {
        const target = await this.resolveZone(zoneId);
        const id = target.zone.climateZoneId;
        const label = target.zone.name?.trim() || "zone";
        if (mode === "off") {
          await this.baxi.setOff(id);
          return { output: [{ type: "text", text: `${label} is off (anti-frost).` }] };
        }
        const chosen = program ?? 1;
        await this.baxi.setSchedule(id, chosen);
        return {
          output: [{ type: "text", text: `${label} follows weekly program ${chosen} again.` }],
        };
      },
    };

    const schedule: ToolRegistration = {
      name: "baxi_schedule",
      description:
        "Read the weekly heating programs and the activity presets. Returns a ready-to-relay " +
        "text block listing every day separately, with each switch point already resolved to " +
        "activity name and temperature.",
      parameters: Type.Object({}),
      replay: "safe",
      execute: async () => {
        const target = await this.resolveZone();
        const id = target.zone.climateZoneId;
        const activities = await this.baxi.getActivities(id);
        const byNumber = new Map(activities.map((a) => [a.activityNumber, a]));
        const dayNames = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
        const active = target.zone.activeHeatingClimateTimeProgramNumber;

        const lines: string[] = [];
        lines.push(`Active program: ${active ?? "unknown"}`);
        lines.push(
          "Activities (temperatures are fixed on the device): " +
            activities.map((a) => `${a.activityNumber}=${a.name} ${a.temperature}C`).join(", "),
        );

        for (const n of [1, 2, 3]) {
          try {
            const { switchPoints } = await this.baxi.getTimeProgram(id, n);
            const perDay = new Map<number, string[]>();
            for (const sp of switchPoints) {
              const a = byNumber.get(sp.activity);
              const label = `${sp.time} ${a?.name ?? `activity ${sp.activity}`} (${a?.temperature ?? "?"}C)`;
              const list = perDay.get(sp.day) ?? [];
              list.push(label);
              perDay.set(sp.day, list);
            }
            lines.push("");
            lines.push(`Program ${n}${active === n ? " (ACTIVE)" : ""}:`);
            for (const [day, items] of [...perDay.entries()].sort((x, y) => x[0] - y[0])) {
              lines.push(`  ${dayNames[day] ?? day}: ${items.sort().join(" -> ")}`);
            }
          } catch {
            /* program number unused */
          }
        }

        return { output: [{ type: "text", text: lines.join("\n") }] };
      },
    };

    const setSwitchPoint: ToolRegistration<typeof SwitchPointParams> = {
      name: "baxi_set_switchpoint",
      description:
        "Set which activity starts at a given day and time in a weekly program, e.g. " +
        "day=1 (Mon) time=07:00 activity=5 (Comfort). Adds the point if missing, replaces " +
        "it if that day/time already exists. The rest of the program is preserved.",
      parameters: SwitchPointParams,
      replay: "unsafe",
      execute: async ({ day, time, activity, program = 1 }) => {
        if (!Number.isInteger(day) || day < 0 || day > 6) {
          throw new Error("day must be 0 (Sunday) through 6 (Saturday)");
        }
        if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(time)) {
          throw new Error("time must be HH:MM in 24h, e.g. 07:30");
        }
        const target = await this.resolveZone();
        const id = target.zone.climateZoneId;
        const activities = await this.baxi.getActivities(id);
        const chosen = activities.find((a) => a.activityNumber === activity);
        if (!chosen) {
          throw new Error(
            `Unknown activity ${activity}. Available: ` +
              activities.map((a) => `${a.activityNumber}=${a.name} (${a.temperature}C)`).join(", "),
          );
        }
        const body = await this.baxi.getTimeProgram(id, program);
        const existing = body.switchPoints.find((sp) => sp.day === day && sp.time === time);
        if (existing) existing.activity = activity;
        else body.switchPoints.push({ day, time, activity, customValue: null });
        body.switchPoints.sort((a, b) => (a.day - b.day) || a.time.localeCompare(b.time));
        await this.baxi.putTimeProgram(id, program, body);
        return {
          output: [
            {
              type: "text",
              text:
                `Program ${program}: ${existing ? "changed" : "added"} ${time} on day ${day} ` +
                `to "${chosen.name}" (${chosen.temperature} C).`,
            },
          ],
        };
      },
    };

    return [status, setTemperature, setMode, schedule, setSwitchPoint];
  }
}
