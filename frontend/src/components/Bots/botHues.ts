/** Per-Bot hue: botIconDesign.ts's drawn colour for a seat or a profile's seed, or, for a
 * non-seat profile, an assigned one from the two extra hues (amber, pink) once one lands, stored
 * under BOT_HUES_KEY so every window of the app agrees. Pure helpers only: a caller wires
 * read/write/list against its own transport (settings, BotsApi). */
import { createContext } from "react";
import type { BotRow, BotsApi } from "../../clients/messaging/botsApi";
import { BOT_HUES, designFor, type Emblem, type Hue, isBotIconId } from "./botIconDesign";

export const BOT_HUES_KEY = "bots.hues";
export const BOT_ICONS_KEY = "bots.icons";

/** Assigned hues, keyed by profile id (a canonical lower-case UUID). */
export type BotHues = Readonly<Record<string, Hue>>;
/** Chosen icon emblems, keyed by profile id (a canonical lower-case UUID). */
export type BotIcons = Readonly<Record<string, Emblem>>;
export type BotLookChoice = { hue?: Hue; icon?: Emblem | null };
export type BotLookResult = "saved" | "superseded";

export interface BotHueControls {
  hues: BotHues;
  icons: BotIcons;
  set?(profileId: string, choice: BotLookChoice): Promise<BotLookResult>;
}

export const BotHuesContext = createContext<BotHueControls>({ hues: {}, icons: {} });

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/** The stored value, parsed back: an entry survives only with a canonical lower-case UUID key and
 * a `BOT_HUES` value. Anything else about an entry (a foreign key, an unknown hue), the whole
 * value (not an object, an array), or unparsable text drops it, never throws: bad text is `{}`. */
export function parseBotHues(value: unknown): BotHues {
  if (typeof value !== "string") return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    return {};
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return {};
  const hues: Record<string, Hue> = {};
  for (const [key, hue] of Object.entries(parsed as Record<string, unknown>)) {
    if (
      UUID.test(key) &&
      typeof hue === "string" &&
      (BOT_HUES as readonly string[]).includes(hue)
    ) {
      hues[key] = hue as Hue;
    }
  }
  return hues;
}

/** Stored icon choices keyed by canonical profile UUID. Unknown icons and malformed input drop. */
export function parseBotIcons(value: unknown): BotIcons {
  if (typeof value !== "string") return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    return {};
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return {};
  const icons: Record<string, Emblem> = {};
  for (const [key, icon] of Object.entries(parsed as Record<string, unknown>)) {
    if (UUID.test(key) && isBotIconId(icon)) icons[key] = icon;
  }
  return icons;
}

/** A Bot's current hue: its stored, assigned one if present — even for a seat, so the owner's
 * pick sticks — else a seat's fixed hue, else the one its seed draws until an assignment lands. */
export function botHue(profileId: string, name: string, hues: BotHues): Hue {
  return hues[profileId] ?? designFor(profileId, name).hue;
}

/** The hue the fewest of `profiles`' distinct Bots show, ties broken in `BOT_HUES` order. */
export function leastUsedHue(
  profiles: readonly { profileId: string; name: string }[],
  hues: BotHues,
): Hue {
  const counted = new Set<string>();
  const counts = new Map<Hue, number>(BOT_HUES.map((hue) => [hue, 0]));
  for (const { profileId, name } of profiles) {
    if (counted.has(profileId)) continue;
    counted.add(profileId);
    const hue = botHue(profileId, name, hues);
    counts.set(hue, (counts.get(hue) ?? 0) + 1);
  }
  let best = BOT_HUES[0] as Hue;
  let fewest = Number.POSITIVE_INFINITY;
  for (const hue of BOT_HUES) {
    const count = counts.get(hue) ?? 0;
    if (count < fewest) {
      fewest = count;
      best = hue;
    }
  }
  return best;
}

/** `hues` with `profileId` set to `hue`, as the sorted-key, compact JSON text the server also
 * writes for the same value, so two windows agreeing on the map agree on its text too. */
export function withBotHue(hues: BotHues, profileId: string, hue: Hue): string {
  const next: Record<string, Hue> = { ...hues, [profileId]: hue };
  const sorted: Record<string, Hue> = {};
  for (const key of Object.keys(next).sort()) sorted[key] = next[key] as Hue;
  return JSON.stringify(sorted);
}

/** `icons` with one emblem set or cleared, as sorted compact JSON. */
export function withBotIcon(icons: BotIcons, profileId: string, icon: Emblem | null): string {
  const next: Record<string, Emblem> = { ...icons };
  if (icon === null) delete next[profileId];
  else next[profileId] = icon;
  const sorted: Record<string, Emblem> = {};
  for (const key of Object.keys(next).sort()) sorted[key] = next[key] as Emblem;
  return JSON.stringify(sorted);
}

type AutoHue = (hues: BotHues) => Hue | undefined;
type QueueHue = Hue | AutoHue;
type PendingLook = {
  profileId: string;
  hue?: QueueHue;
  icon?: Emblem | null;
  callers: {
    hue: boolean;
    icon: boolean;
    superseded: boolean;
    resolve(result: BotLookResult): void;
    reject(error: unknown): void;
  }[];
};

/** Shared serialized settings writer for manual Look choices and automatic hue assignment. */
export function createBotLookQueue(deps: {
  read(): { hues: BotHues; icons: BotIcons };
  patch(changes: Record<string, unknown>): Promise<unknown>;
}): {
  set(profileId: string, choice: BotLookChoice): Promise<BotLookResult>;
  assign(profileId: string, chooseHue: AutoHue): Promise<void>;
} {
  const pending: PendingLook[] = [];
  let draining = false;

  const enqueue = (
    profileId: string,
    choice: { hue?: QueueHue; icon?: Emblem | null },
    automatic = false,
  ) => {
    let item = pending.find((candidate) => candidate.profileId === profileId);
    if (!item) {
      item = { profileId, callers: [] };
      pending.push(item);
    }
    if (
      choice.hue !== undefined &&
      (!automatic || item.hue === undefined || typeof item.hue === "function")
    ) {
      for (const caller of item.callers) if (caller.hue) caller.superseded = true;
      item.hue = choice.hue;
    }
    if (choice.icon !== undefined) {
      for (const caller of item.callers) if (caller.icon) caller.superseded = true;
      item.icon = choice.icon;
    }
    return item;
  };

  const drain = async (): Promise<void> => {
    if (draining) return;
    draining = true;
    try {
      while (pending.length) {
        const item = pending.shift() as PendingLook;
        const { hues, icons } = deps.read();
        const hue = typeof item.hue === "function" ? item.hue(hues) : item.hue;
        const icon = item.icon;
        if (hue === undefined && icon === undefined) {
          for (const caller of item.callers) caller.resolve("superseded");
          continue;
        }
        const sortedJson = <T>(values: Readonly<Record<string, T>>) => {
          const sorted: Record<string, T> = {};
          for (const key of Object.keys(values).sort()) sorted[key] = values[key] as T;
          return JSON.stringify(sorted);
        };
        const changes = {
          [BOT_HUES_KEY]:
            hue === undefined ? sortedJson(hues) : withBotHue(hues, item.profileId, hue),
          [BOT_ICONS_KEY]:
            icon === undefined ? sortedJson(icons) : withBotIcon(icons, item.profileId, icon),
        };
        try {
          await deps.patch(changes);
          for (const caller of item.callers) {
            caller.resolve(caller.superseded ? "superseded" : "saved");
          }
        } catch (error) {
          for (const caller of item.callers) caller.reject(error);
        }
      }
    } finally {
      draining = false;
    }
  };

  return {
    set(profileId, choice) {
      const result = new Promise<BotLookResult>((resolve, reject) => {
        const item = enqueue(profileId, choice);
        item.callers.push({
          hue: choice.hue !== undefined,
          icon: choice.icon !== undefined,
          superseded: false,
          resolve,
          reject,
        });
      });
      void drain();
      return result;
    },
    assign(profileId, chooseHue) {
      return new Promise<void>((resolve, reject) => {
        const item = enqueue(profileId, { hue: chooseHue }, true);
        item.callers.push({
          hue: false,
          icon: false,
          superseded: false,
          resolve: () => resolve(),
          reject,
        });
        void drain();
      });
    },
  };
}

/** Queues one Bot's assignment unless it has no profile, is a seat, or shares its profile. */
export function hueAssigner(deps: {
  list(): Promise<readonly BotRow[]>;
  assign(profileId: string, chooseHue: AutoHue): Promise<void>;
}): (bot: BotRow) => Promise<void> {
  return async (bot) => {
    const profile = bot.profile;
    if (profile === null) return; // missing profile
    if (designFor(bot.profile_id, profile.identity.name).seat !== null) return; // seat name
    const rows = await deps.list();
    if (rows.some((row) => row.profile_id === bot.profile_id && row.bot_id !== bot.bot_id)) return; // another Bot row
    const others = rows.flatMap((row) =>
      row.profile && row.profile_id !== bot.profile_id
        ? [{ profileId: row.profile_id, name: row.profile.identity.name }]
        : [],
    );
    const chooseHue: AutoHue = (hues) =>
      bot.profile_id in hues ? undefined : leastUsedHue(others, hues);
    await deps.assign(bot.profile_id, chooseHue);
  };
}

/** Wraps a Bot create so each non-replayed assignment finishes its list and shared-queue write
 * before the next starts. A replay assigns nothing; assignment failure never fails the create.
 * Every other method passes through. */
export function assigningHues(api: BotsApi, assign: (bot: BotRow) => Promise<void>): BotsApi {
  let queue: Promise<void> = Promise.resolve();
  return {
    ...api,
    async create(botId, profileId, expectedProfileRevision, workspaceId, signal) {
      const result = await api.create(
        botId,
        profileId,
        expectedProfileRevision,
        workspaceId,
        signal,
      );
      if (!result.replayed) {
        queue = queue.then(() => assign(result.bot)).catch(() => undefined);
        await queue;
      }
      return result;
    },
  };
}
