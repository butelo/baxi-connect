/**
 * Minimal client for the BDR Thermea cloud (the API behind the BAXI Connect app).
 *
 * Runs in a Durable Object, so the OAuth token lives in the object's SQLite
 * storage and survives eviction. Only the refresh grant is used here: the
 * interactive login (email + password, Azure AD B2C) is done once on your
 * laptop by `baxi.py login`, and its refresh token is seeded as a secret.
 *
 * Note: B2C rotates the refresh token on every refresh, so the new one must be
 * written back or the next call fails.
 */

const TOKEN_URL =
  "https://remehalogin.bdrthermea.net/bdrb2cprod.onmicrosoft.com/oauth2/v2.0/token?p=B2C_1A_RPSignUpSignInNewRoomV3.1";
const CLIENT_ID = "6ce007c6-0628-419e-88f4-bee2e6418eec";
const API_BASE = "https://api.bdrthermea.net/Mobile/api";
const API_KEY = "df605c5470d846fc91e848b1cc653ddf";
const TOKEN_KEY = "baxi:token";

/** Where the (single, shared) BDR token lives. */
export { TOKEN_KEY };

export interface StoredToken {
  access: string;
  refresh: string;
  exp: number; // unix seconds
}

/** Anything that can hand out a bearer token: the durable vault, in practice. */
export interface TokenSource {
  accessToken(): Promise<string>;
}

/** Token endpoint refusal, with the status kept so callers can tell 400 from a blip. */
export class TokenRefreshError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "TokenRefreshError";
  }
}

/**
 * Exchange a refresh token for a new access token. B2C rotates the refresh
 * token on every use, so the caller must persist the result.
 */
export async function refreshTokens(refresh: string): Promise<StoredToken> {
  const res = await fetch(TOKEN_URL, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "refresh_token",
      refresh_token: refresh,
      client_id: CLIENT_ID,
    }),
  });

  if (!res.ok) {
    const body = await res.text();
    throw new TokenRefreshError(
      `Baxi token refresh failed (${res.status}). Re-seed BAXI_REFRESH_TOKEN with a fresh ` +
        `\`python3 baxi.py login\`. Details: ${body.slice(0, 200)}`,
      res.status,
    );
  }

  const json = (await res.json()) as {
    access_token: string;
    refresh_token?: string;
    expires_in?: number;
  };

  return {
    access: json.access_token,
    refresh: json.refresh_token ?? refresh,
    exp: Math.floor(Date.now() / 1000) + (json.expires_in ?? 3600),
  };
}

export interface Zone {
  climateZoneId: string;
  name?: string;
  zoneMode?: string;
  roomTemperature?: number;
  setPoint?: number;
  setPointMin?: number;
  setPointMax?: number;
  nextSetpoint?: number;
  nextSwitchTime?: string;
  currentScheduleSetPoint?: number;
  activeHeatingClimateTimeProgramNumber?: number;
}

export interface Appliance {
  applianceId: string;
  applianceType?: string;
  applianceOnline?: boolean;
  applianceConnectionStatus?: string;
  operatingMode?: string;
  waterPressure?: number;
  waterPressureOK?: boolean;
  outdoorTemperature?: number;
  climateZones?: Zone[];
  hotWaterZones?: Array<Record<string, unknown>>;
}

export interface Dashboard {
  appliances: Appliance[];
}

export interface Activity {
  activityNumber: number;
  type: string;
  name: string;
  temperature: number;
  setPointMin: number;
  setPointMax: number;
  increment: number;
}

export interface SwitchPoint {
  /** 0 = Sunday … 6 = Saturday. */
  day: number;
  /** "HH:MM", 24h. */
  time: string;
  /** Which activity applies from this point on. */
  activity: number;
  customValue: number | null;
}

export interface TimeProgram {
  timeProgramNumber: number;
  type: string;
  switchPoints: SwitchPoint[];
}

export class BaxiClient {
  constructor(private readonly tokens: TokenSource) {}

  private async api<T>(method: string, path: string, body?: unknown): Promise<T> {
    const token = await this.tokens.accessToken();
    const headers: Record<string, string> = {
      authorization: `Bearer ${token}`,
      "Ocp-Apim-Subscription-Key": API_KEY,
      accept: "application/json",
    };
    if (body !== undefined) headers["content-type"] = "application/json";

    const res = await fetch(API_BASE + path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await res.text();
    if (!res.ok) {
      throw new Error(`${method} ${path} -> HTTP ${res.status}: ${text.slice(0, 300)}`);
    }
    return (text ? JSON.parse(text) : null) as T;
  }

  dashboard(): Promise<Dashboard> {
    return this.api<Dashboard>("GET", "/homes/dashboard");
  }

  /** Hold a temperature indefinitely (leaves the weekly program). */
  setManual(zoneId: string, temperature: number): Promise<unknown> {
    return this.api("POST", `/climate-zones/${zoneId}/modes/manual`, {
      roomTemperatureSetPoint: temperature,
    });
  }

  /** Change until the next scheduled step. */
  setOverride(zoneId: string, temperature: number): Promise<unknown> {
    return this.api("POST", `/climate-zones/${zoneId}/modes/temporary-override`, {
      roomTemperatureSetPoint: temperature,
    });
  }

  setSchedule(zoneId: string, programId = 1): Promise<unknown> {
    return this.api("POST", `/climate-zones/${zoneId}/modes/schedule`, {
      heatingProgramId: programId,
    });
  }

  setOff(zoneId: string): Promise<unknown> {
    return this.api("POST", `/climate-zones/${zoneId}/modes/anti-frost`);
  }

  setHotWater(zoneId: string, comfort?: number, reduced?: number): Promise<unknown> {
    const body: Record<string, number> = {};
    if (comfort !== undefined) body.comfortSetpoint = comfort;
    if (reduced !== undefined) body.reducedSetpoint = reduced;
    return this.api("POST", `/hot-water-zones/${zoneId}/setpoints`, body);
  }

  // Weekly program. Readable and writable; the activity *temperatures* are not
  // (they are configured on the device), only which activity applies when.

  getActivities(zoneId: string): Promise<Activity[]> {
    return this.api<Activity[]>("GET", `/climate-zones/${zoneId}/activities`);
  }

  getTimeProgram(zoneId: string, program: number): Promise<TimeProgram> {
    return this.api<TimeProgram>(
      "GET",
      `/climate-zones/${zoneId}/time-programs/heating/${program}`,
    );
  }

  putTimeProgram(zoneId: string, program: number, body: TimeProgram): Promise<unknown> {
    return this.api("PUT", `/climate-zones/${zoneId}/time-programs/heating/${program}`, body);
  }
}

/** Flatten all climate zones with a stable index, for tool output. */
export function flattenZones(d: Dashboard) {
  const out: Array<{ index: number; zone: Zone; appliance: Appliance }> = [];
  for (const appliance of d.appliances ?? []) {
    for (const zone of appliance.climateZones ?? []) {
      out.push({ index: out.length, zone, appliance });
    }
  }
  return out;
}
