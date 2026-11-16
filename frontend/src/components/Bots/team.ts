/**
 * The default team: Chief, Ops, the specialists Research, Writer and Engineer, Desk and the independent Checker Bot.
 * A Bot fills a seat by its profile's fixed id, else by its profile name; the roster sorts a vault's
 * Bots into seats, other Bots, and Bots of other vaults.
 */
import type { BotRow } from "../../clients/messaging/botsApi";
import type { ProfileIdentityV1 } from "../../clients/profiles/profileApi";
import { SEAT_STARTERS, type SeatStarter } from "../Profiles/seatStarters";

export type SeatId = "chief" | "ops" | "research" | "writer" | "engineer" | "desk" | "checker";

export interface TeamSeat {
  seat: SeatId;
  name: string;
  role: string;
  starter: SeatStarter | null;
  profileId: string | null;
}

function seat(
  id: SeatId,
  name: string,
  role: string,
  profileId: string | null,
  starterName: string | null,
): TeamSeat {
  const starter = SEAT_STARTERS.find((s) => s.name === starterName) ?? null;
  return { seat: id, name, role, starter, profileId };
}

export const TEAM: readonly TeamSeat[] = [
  seat("chief", "Chief", "Front door", "6a0d02f9-f8dc-4034-941d-03cedde6c839", "Chief"),
  seat("ops", "Ops", "Manager", "e7b63f13-9eb1-4b0d-8df6-cc08d3be33a1", "Ops"),
  seat("research", "Research", "Specialist", "b0c7ec1e-85a0-4c20-97f3-e79509370f20", "Research"),
  seat("writer", "Writer", "Specialist", "61a1448b-613a-4880-92f7-ec0dd6604bf9", "Writer"),
  seat("engineer", "Engineer", "Specialist", "3f1c9a6e-5b2d-4e8f-9a07-c41d2b6e8f53", "Engineer"),
  seat("desk", "Desk", "General desk", "b9f8d2d5-0670-4212-a744-f4180c3a36fe", "Desk"),
  seat("checker", "Checker", "Quality gate", "9b0e32bf-2d57-46d8-86ce-799a81ecb248", "Checker"),
];

const BOT_SEATS = TEAM.filter((s) => s.profileId !== null);

/** The Bot seat a name claims (trimmed, any letter case), or null. */
export function seatOf(name: string): SeatId | null {
  const key = name.trim().toLowerCase();
  return BOT_SEATS.find((s) => s.seat === key)?.seat ?? null;
}

/** A seat's face as a profile id and identity, for BotAvatar. */
export function seatFace(id: SeatId): { profileId: string; identity: ProfileIdentityV1 } {
  const found = TEAM.find((s) => s.seat === id) as TeamSeat;
  return {
    profileId: found.profileId ?? "seat:checker",
    identity: {
      name: found.starter?.name ?? found.name,
      purpose: found.starter?.purpose ?? "",
      avatar_asset_id: null,
      accent: found.starter?.accent ?? "blue",
      shape: "rounded",
    },
  };
}

export function botName(bot: BotRow): string {
  return bot.profile?.identity.name ?? "Unavailable profile";
}

export interface TeamRoster {
  seats: { seat: TeamSeat; bot: BotRow | null }[];
  others: BotRow[];
  away: BotRow[];
}

export function teamRoster(bots: readonly BotRow[], activeWorkspaceId: string): TeamRoster {
  const here = bots.filter((b) => b.workspace_id === activeWorkspaceId);
  const away = bots.filter((b) => b.workspace_id !== activeWorkspaceId);
  const claimed = new Map<SeatId, BotRow>();
  const take = (matches: (bot: BotRow, s: TeamSeat) => boolean): void => {
    for (const s of BOT_SEATS) {
      if (claimed.has(s.seat)) continue;
      const bot = here.find((b) => ![...claimed.values()].includes(b) && matches(b, s));
      if (bot) claimed.set(s.seat, bot);
    }
  };
  take((b, s) => b.profile_id === s.profileId);
  take((b, s) => b.profile !== null && seatOf(b.profile.identity.name) === s.seat);
  const taken = new Set(claimed.values());
  return {
    seats: TEAM.map((s) => ({ seat: s, bot: claimed.get(s.seat) ?? null })),
    others: here.filter((b) => !taken.has(b)),
    away,
  };
}
