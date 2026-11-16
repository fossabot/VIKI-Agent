/**
 * Opening a team seat: find or make the seat's profile and Bot in a vault, then prepare the Bot's
 * chat. Each create runs at most once; a `conflict` re-reads the list and adopts what another open
 * made. Nothing here starts a turn, a new chat or a routine.
 *
 * `fillTeam` runs that same find-or-make for every unclaimed seat of a vault, and
 * `fillStarterTeam` runs it once for a freshly created starter vault — never the chat prepare,
 * since that vault is not this launch's vault.
 */
import type { CreateVaultResult } from "../../clients/app/shellApi";
import {
  type BotChatResult,
  type BotRow,
  type BotsApi,
  BotsApiError,
} from "../../clients/messaging/botsApi";
import {
  type ProfileApi,
  ProfileApiError,
  type ProfileSummary,
} from "../../clients/profiles/profileApi";
import { starterDocument } from "../Profiles/seatStarters";
import { botErrorText } from "./BotChatActivity";
import { seatOf, TEAM, type TeamSeat, teamRoster } from "./team";

export class SeatError extends Error {
  readonly code = "seat_profile_archived";

  constructor() {
    super("The seat's profile is archived.");
    this.name = "SeatError";
  }
}

/** The non-archived profile that serves a seat: its fixed id, else the first one named for it. */
export function seatProfile(
  rows: readonly ProfileSummary[],
  seat: TeamSeat,
): ProfileSummary | null {
  return (
    rows.find((r) => r.profile_id === seat.profileId) ??
    rows.find(
      (r) =>
        seatOf(r.identity.name) === seat.seat &&
        !TEAM.some((t) => t !== seat && t.profileId === r.profile_id),
    ) ??
    null
  );
}

function isConflict(error: unknown): boolean {
  return (
    (error instanceof ProfileApiError || error instanceof BotsApiError) && error.code === "conflict"
  );
}

/** The seat's Bot: claimed from the roster, else made (profile then Bot), else adopted from a
 * `conflict` another open already resolved. */
export async function fillSeat(
  api: BotsApi,
  profiles: ProfileApi,
  seat: TeamSeat,
  workspaceId: string,
  signal: AbortSignal,
): Promise<BotRow> {
  if (seat.profileId === null || seat.starter === null)
    throw new Error(`${seat.name} has no conversation to open.`);
  const claimed = teamRoster((await api.list(signal)).bots, workspaceId).seats.find(
    (s) => s.seat.seat === seat.seat,
  )?.bot;
  if (claimed) return claimed;

  let profile = seatProfile(await profiles.list(false, signal), seat);
  let revision = profile?.current_revision ?? 1;
  if (!profile) {
    try {
      await profiles.create(
        seat.profileId,
        crypto.randomUUID(),
        starterDocument(seat.starter),
        signal,
      );
    } catch (error) {
      if (!isConflict(error)) throw error;
      profile = seatProfile(await profiles.list(false, signal), seat);
      if (!profile) throw new SeatError();
      revision = profile.current_revision;
    }
  }
  const profileId = profile?.profile_id ?? seat.profileId;

  try {
    return (await api.create(crypto.randomUUID(), profileId, revision, workspaceId, signal)).bot;
  } catch (error) {
    if (!isConflict(error)) throw error;
    const adopted = (await api.list(signal)).bots.find(
      (b) => b.profile_id === profileId && b.workspace_id === workspaceId,
    );
    if (!adopted) throw error;
    return adopted;
  }
}

export async function openSeat(
  api: BotsApi,
  profiles: ProfileApi,
  seat: TeamSeat,
  workspaceId: string,
  signal: AbortSignal,
): Promise<{ bot: BotRow; chat: BotChatResult }> {
  const bot = await fillSeat(api, profiles, seat, workspaceId, signal);
  return { bot, chat: await api.chat(bot, signal) };
}

/** `created_files` entry marking a vault made from the starter template: the one-time signal for
 * `fillStarterTeam` to run. */
export const STARTER_KANBAN_FILE = ".claude/skills/kanban/SKILL.md";

/**
 * Fills every Bot seat of a freshly created starter vault, once. No chat is prepared here: the
 * new vault is not this launch's vault, so the server refuses a prepare
 * (`workspace_restart_required`) until a restart. A seat whose fill fails is left: it stays on
 * Core team, where Open conversation tries again.
 */
export async function fillStarterTeam(
  api: BotsApi,
  profiles: ProfileApi,
  created: Pick<CreateVaultResult, "workspace" | "created_files">,
): Promise<void> {
  if (!created.created_files.includes(STARTER_KANBAN_FILE)) return;
  // A seat whose fill fails stays on Core team, where Open conversation tries again.
  await fillTeam(api, profiles, created.workspace.workspace_id, new AbortController().signal).catch(
    () => undefined,
  );
}

export interface TeamFill {
  added: TeamSeat[];
  present: TeamSeat[];
  failed: { seat: TeamSeat; error: unknown }[];
}

/**
 * Fills every unclaimed Bot seat of a vault, one at a time in `TEAM` order, and reports what it
 * did. A seat whose fill fails is kept in `failed` and the rest go on; an abort rejects. No chat
 * is prepared.
 */
export async function fillTeam(
  api: BotsApi,
  profiles: ProfileApi,
  workspaceId: string,
  signal: AbortSignal,
): Promise<TeamFill> {
  const result: TeamFill = { added: [], present: [], failed: [] };
  for (const { seat, bot } of teamRoster((await api.list(signal)).bots, workspaceId).seats) {
    if (seat.profileId === null) continue;
    if (bot) {
      result.present.push(seat);
      continue;
    }
    signal.throwIfAborted();
    try {
      await fillSeat(api, profiles, seat, workspaceId, signal);
      result.added.push(seat);
    } catch (error) {
      if (signal.aborted) throw error;
      result.failed.push({ seat, error });
    }
  }
  return result;
}

/** The user-facing line for a failed seat open. */
export function seatErrorText(error: unknown, seat: TeamSeat): string {
  if (error instanceof SeatError)
    return `${seat.name}'s profile is archived. Unarchive it in Bots → Bot settings, then open the seat again.`;
  if (error instanceof ProfileApiError)
    return error.code === "capacity"
      ? "VIKI Agent cannot store another profile. Add a Bot for one of your profiles with New Bot instead."
      : "The seat's profile could not be created. Refresh Bots to try again.";
  return botErrorText(error);
}
