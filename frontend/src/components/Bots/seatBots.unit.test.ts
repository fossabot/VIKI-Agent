import { expect, test, vi } from "vitest";
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
import {
  fillStarterTeam,
  fillTeam,
  openSeat,
  SeatError,
  STARTER_KANBAN_FILE,
  seatErrorText,
  seatProfile,
} from "./seatBots";
import { TEAM, type TeamSeat } from "./team";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const WORKSPACE = "11111111-1111-4111-8111-111111111111";
const signal = new AbortController().signal;
const seatById = (id: string): TeamSeat => TEAM.find((s) => s.seat === id) as TeamSeat;
const ops = seatById("ops");
const checker = seatById("checker");
const opsId = ops.profileId as string;

function bot(id: string, profileId: string, workspace = WORKSPACE): BotRow {
  return { bot_id: id, profile_id: profileId, workspace_id: workspace, profile: null } as BotRow;
}

function profile(id: string, name: string, current = 1): ProfileSummary {
  return { profile_id: id, current_revision: current, identity: { name } } as ProfileSummary;
}

function created(files: string[]): Parameters<typeof fillStarterTeam>[2] {
  return { workspace: { workspace_id: WORKSPACE }, created_files: files } as Parameters<
    typeof fillStarterTeam
  >[2];
}

const chatResult = (b: BotRow): BotChatResult => ({ bot_id: b.bot_id }) as BotChatResult;
const profileConflict = (): ProfileApiError => new ProfileApiError(409, "conflict");
const botConflict = (): BotsApiError => new BotsApiError(409, "conflict");

function fakes(options: {
  bots?: BotRow[][];
  profiles?: ProfileSummary[][];
  createProfile?: (...args: unknown[]) => Promise<unknown>;
  createBot?: (profileId: string) => Promise<{ bot: BotRow; replayed: boolean }>;
}) {
  const botLists = [...(options.bots ?? [[]])];
  const profileLists = [...(options.profiles ?? [[]])];
  const api = {
    list: vi.fn(async () => ({ bots: botLists.shift() ?? [], scheduler: {} })),
    create: vi.fn(
      options.createBot ??
        (async (id: string, profileId: string) => ({
          bot: bot(id, profileId),
          replayed: false,
        })),
    ),
    chat: vi.fn(async (b: BotRow) => chatResult(b)),
    newChat: vi.fn(),
    remove: vi.fn(),
    reset: vi.fn(),
    setDelivery: vi.fn(),
    get: vi.fn(),
  };
  const profiles = {
    list: vi.fn(async () => profileLists.shift() ?? []),
    create: vi.fn(options.createProfile ?? (async (..._args: unknown[]) => ({}))),
  };
  return {
    api,
    profiles,
    run: () =>
      openSeat(
        api as unknown as BotsApi,
        profiles as unknown as ProfileApi,
        ops,
        WORKSPACE,
        signal,
      ),
  };
}

function untouched(api: ReturnType<typeof fakes>["api"]): void {
  expect(api.newChat).not.toHaveBeenCalled();
  expect(api.remove).not.toHaveBeenCalled();
  expect(api.reset).not.toHaveBeenCalled();
  expect(api.setDelivery).not.toHaveBeenCalled();
}

test("seatProfile prefers the seat id, then the first row named for the seat", () => {
  const named = profile("a", " OPS ");
  const byId = profile(opsId, "Renamed");
  expect(seatProfile([named, byId], ops)).toBe(byId);
  expect(seatProfile([profile("x", "Other"), named, profile("b", "ops")], ops)).toBe(named);
  expect(seatProfile([profile("x", "Other")], ops)).toBeNull();
});

const chiefId = seatById("chief").profileId as string;

test("a name match never takes another seat's fixed-id profile", async () => {
  const renamed = profile(chiefId, "Ops");
  expect(seatProfile([renamed], ops)).toBeNull();
  expect(seatProfile([renamed, profile("x", "Ops")], ops)?.profile_id).toBe("x");
  const f = fakes({ profiles: [[renamed]] });
  const result = await f.run();
  expect(f.profiles.create).toHaveBeenCalledWith(
    opsId,
    expect.stringMatching(UUID),
    starterDocument(ops.starter as NonNullable<TeamSeat["starter"]>),
    signal,
  );
  expect(f.api.create).toHaveBeenCalledWith(
    expect.stringMatching(UUID),
    opsId,
    1,
    WORKSPACE,
    signal,
  );
  expect(result.bot.profile_id).toBe(opsId);
});

test("a Bot that already fills the seat claimed by profile id is chatted with, nothing created", async () => {
  const claimed = bot("b1", opsId);
  const f = fakes({ bots: [[claimed]] });
  const result = await f.run();
  expect(result).toEqual({ bot: claimed, chat: chatResult(claimed) });
  expect(f.api.chat).toHaveBeenCalledWith(claimed, signal);
  expect(f.profiles.list).not.toHaveBeenCalled();
  expect(f.profiles.create).not.toHaveBeenCalled();
  expect(f.api.create).not.toHaveBeenCalled();
  untouched(f.api);
});

test("a Bot whose profile is named for the seat is claimed by name", async () => {
  const claimed = {
    ...bot("b1", "other-profile"),
    profile: { revision: 1, archived: false, identity: { name: "ops" } },
  } as BotRow;
  const f = fakes({ bots: [[bot("b0", "p0", "22222222-2222-4222-8222-222222222222"), claimed]] });
  const result = await f.run();
  expect(result.bot).toBe(claimed);
  expect(f.api.create).not.toHaveBeenCalled();
});

test("the seat-id profile is reused: only a Bot is created, at its current revision", async () => {
  const f = fakes({ profiles: [[profile("x", "Ops"), profile(opsId, "Mine", 4)]] });
  const result = await f.run();
  expect(f.profiles.create).not.toHaveBeenCalled();
  expect(f.api.create).toHaveBeenCalledTimes(1);
  expect(f.api.create).toHaveBeenCalledWith(
    expect.stringMatching(UUID),
    opsId,
    4,
    WORKSPACE,
    signal,
  );
  expect(f.api.chat).toHaveBeenCalledWith(result.bot, signal);
  untouched(f.api);
});

test("the oldest profile named for the seat is reused", async () => {
  const f = fakes({ profiles: [[profile("older", "Ops", 2), profile("newer", "ops")]] });
  await f.run();
  expect(f.profiles.create).not.toHaveBeenCalled();
  expect(f.api.create).toHaveBeenCalledWith(
    expect.stringMatching(UUID),
    "older",
    2,
    WORKSPACE,
    signal,
  );
});

test("with no profile it creates the profile, then the Bot, then prepares the chat", async () => {
  const f = fakes({});
  const result = await f.run();
  expect(f.profiles.create).toHaveBeenCalledTimes(1);
  expect(f.profiles.create).toHaveBeenCalledWith(
    opsId,
    expect.stringMatching(UUID),
    starterDocument(ops.starter as NonNullable<TeamSeat["starter"]>),
    signal,
  );
  expect(f.api.create).toHaveBeenCalledTimes(1);
  expect(f.api.create).toHaveBeenCalledWith(
    expect.stringMatching(UUID),
    opsId,
    1,
    WORKSPACE,
    signal,
  );
  expect(f.profiles.list).toHaveBeenCalledWith(false, signal);
  expect(f.profiles.create.mock.invocationCallOrder[0]).toBeLessThan(
    f.api.create.mock.invocationCallOrder[0] as number,
  );
  expect(f.api.create.mock.invocationCallOrder[0]).toBeLessThan(
    f.api.chat.mock.invocationCallOrder[0] as number,
  );
  expect(f.api.chat).toHaveBeenCalledWith(result.bot, signal);
  expect(f.profiles.create.mock.calls[0]?.[1]).not.toBe(f.api.create.mock.calls[0]?.[0]);
  untouched(f.api);
});

test("a profile conflict re-lists and uses the profile another open made", async () => {
  const f = fakes({
    profiles: [[], [profile(opsId, "Ops", 3)]],
    createProfile: async () => {
      throw profileConflict();
    },
  });
  await f.run();
  expect(f.profiles.create).toHaveBeenCalledTimes(1);
  expect(f.profiles.list).toHaveBeenCalledTimes(2);
  expect(f.api.create).toHaveBeenCalledWith(
    expect.stringMatching(UUID),
    opsId,
    3,
    WORKSPACE,
    signal,
  );
});

test("a profile conflict with no active profile is an archived-profile error and makes no Bot", async () => {
  const f = fakes({
    profiles: [[], []],
    createProfile: async () => {
      throw profileConflict();
    },
  });
  const error = await f.run().catch((e: unknown) => e);
  expect(error).toBeInstanceOf(SeatError);
  expect((error as SeatError).code).toBe("seat_profile_archived");
  expect(f.api.create).not.toHaveBeenCalled();
  expect(f.api.chat).not.toHaveBeenCalled();
});

test("a Bot conflict adopts the Bot for that profile and vault", async () => {
  const mine = bot("b9", opsId);
  const f = fakes({
    profiles: [[profile(opsId, "Ops")]],
    bots: [[], [bot("b8", opsId, "22222222-2222-4222-8222-222222222222"), bot("b7", "p7"), mine]],
    createBot: async () => {
      throw botConflict();
    },
  });
  const result = await f.run();
  expect(result.bot).toBe(mine);
  expect(f.api.create).toHaveBeenCalledTimes(1);
  expect(f.api.chat).toHaveBeenCalledWith(mine, signal);
});

test("a Bot conflict with no matching Bot throws that error", async () => {
  const conflict = botConflict();
  const f = fakes({
    profiles: [[profile(opsId, "Ops")]],
    bots: [[], []],
    createBot: async () => {
      throw conflict;
    },
  });
  await expect(f.run()).rejects.toBe(conflict);
  expect(f.api.chat).not.toHaveBeenCalled();
});

test("other errors pass through and Checker can open a conversation", async () => {
  const failure = new ProfileApiError(507, "capacity");
  const f = fakes({
    createProfile: async () => {
      throw failure;
    },
  });
  await expect(f.run()).rejects.toBe(failure);
  expect(f.api.create).not.toHaveBeenCalled();
  const g = fakes({});
  await expect(
    openSeat(
      g.api as unknown as BotsApi,
      g.profiles as unknown as ProfileApi,
      checker,
      WORKSPACE,
      signal,
    ),
  ).resolves.toBeDefined();
  expect(g.api.create).toHaveBeenCalled();
});

test("seatErrorText maps the seat errors", () => {
  expect(seatErrorText(new SeatError(), ops)).toBe(
    "Ops's profile is archived. Unarchive it in Bots → Bot settings, then open the seat again.",
  );
  expect(seatErrorText(new ProfileApiError(507, "capacity"), ops)).toBe(
    "VIKI Agent cannot store another profile. Add a Bot for one of your profiles with New Bot instead.",
  );
  expect(seatErrorText(new ProfileApiError(500, "unavailable"), ops)).toBe(
    "The seat's profile could not be created. Refresh Bots to try again.",
  );
  expect(seatErrorText(new BotsApiError(409, "conflict"), ops)).toBe(
    "This Bot changed. Refresh Bots before trying again.",
  );
  expect(seatErrorText(new Error("x"), ops)).toBe(
    "Bots are unavailable. Refresh Bots to try again.",
  );
});

test("fillStarterTeam fills every seat once, in TEAM order, when the starter Kanban is new", async () => {
  const f = fakes({});
  await fillStarterTeam(
    f.api as unknown as BotsApi,
    f.profiles as unknown as ProfileApi,
    created([STARTER_KANBAN_FILE]),
  );
  const seats = TEAM.filter((s) => s.profileId !== null);
  expect(f.profiles.create).toHaveBeenCalledTimes(seats.length);
  expect(f.api.create).toHaveBeenCalledTimes(seats.length);
  seats.forEach((seat, index) => {
    expect(f.profiles.create).toHaveBeenNthCalledWith(
      index + 1,
      seat.profileId,
      expect.stringMatching(UUID),
      starterDocument(seat.starter as NonNullable<TeamSeat["starter"]>),
      expect.any(AbortSignal),
    );
    expect(f.api.create).toHaveBeenNthCalledWith(
      index + 1,
      expect.stringMatching(UUID),
      seat.profileId,
      1,
      WORKSPACE,
      expect.any(AbortSignal),
    );
  });
  expect(f.api.chat).not.toHaveBeenCalled();
  // One seat at a time: each seat's roster read waits for the previous seat's Bot create.
  const lists = f.api.list.mock.invocationCallOrder;
  const creates = f.api.create.mock.invocationCallOrder;
  for (let i = 1; i < seats.length; i++) {
    expect(lists[i + 1]).toBeGreaterThan(creates[i - 1] as number);
  }
  untouched(f.api);
});

test("fillStarterTeam does nothing when the starter Kanban was not created", async () => {
  const f = fakes({});
  await fillStarterTeam(
    f.api as unknown as BotsApi,
    f.profiles as unknown as ProfileApi,
    created(["Welcome.md"]),
  );
  expect(f.api.list).not.toHaveBeenCalled();
  expect(f.api.create).not.toHaveBeenCalled();
  expect(f.profiles.list).not.toHaveBeenCalled();
  expect(f.profiles.create).not.toHaveBeenCalled();
  expect(f.api.chat).not.toHaveBeenCalled();
  untouched(f.api);
});

test.each([".claude/skills/kanban/SKILL.md"])(
  "fillStarterTeam recognizes the literal starter Kanban path %s without preparing chats",
  async (path) => {
    const f = fakes({});
    await fillStarterTeam(
      f.api as unknown as BotsApi,
      f.profiles as unknown as ProfileApi,
      created([path]),
    );
    expect(f.api.create).toHaveBeenCalledTimes(
      TEAM.filter((seat) => seat.profileId !== null).length,
    );
    expect(f.api.chat).not.toHaveBeenCalled();
  },
);

test("fillStarterTeam resolves past per-seat failures, leaving only those seats unfilled", async () => {
  const researchId = seatById("research").profileId;
  const f = fakes({
    createProfile: async (...args: unknown[]) => {
      const [profileId] = args as [string];
      if (profileId === chiefId) throw new ProfileApiError(507, "capacity");
      if (profileId === opsId) throw profileConflict();
      return {};
    },
    createBot: async (...args: unknown[]) => {
      const [id, profileId] = args as [string, string];
      if (profileId === researchId) throw new TypeError("boom");
      return { bot: bot(id, profileId), replayed: false };
    },
  });
  await expect(
    fillStarterTeam(
      f.api as unknown as BotsApi,
      f.profiles as unknown as ProfileApi,
      created([STARTER_KANBAN_FILE]),
    ),
  ).resolves.toBeUndefined();
  expect(f.profiles.create).toHaveBeenCalledTimes(TEAM.length);
  expect(f.api.create.mock.calls.map((call) => call[1])).toEqual(
    TEAM.filter((s) => s.seat !== "chief" && s.seat !== "ops").map((s) => s.profileId),
  );
  expect(f.api.chat).not.toHaveBeenCalled();
});

test("fillStarterTeam reuses every already-listed fixed-id profile at their current revision", async () => {
  const seats = TEAM.filter((s) => s.profileId !== null);
  const fixedProfiles = seats.map((seat) => profile(seat.profileId as string, seat.name, 2));
  const f = fakes({ profiles: seats.map(() => fixedProfiles) });
  await fillStarterTeam(
    f.api as unknown as BotsApi,
    f.profiles as unknown as ProfileApi,
    created([STARTER_KANBAN_FILE]),
  );
  expect(f.profiles.create).not.toHaveBeenCalled();
  expect(f.api.create).toHaveBeenCalledTimes(seats.length);
  seats.forEach((seat, index) => {
    expect(f.api.create).toHaveBeenNthCalledWith(
      index + 1,
      expect.stringMatching(UUID),
      seat.profileId,
      2,
      WORKSPACE,
      expect.any(AbortSignal),
    );
  });
  expect(f.api.chat).not.toHaveBeenCalled();
  untouched(f.api);
});

function team(f: ReturnType<typeof fakes>, abort = signal) {
  return fillTeam(
    f.api as unknown as BotsApi,
    f.profiles as unknown as ProfileApi,
    WORKSPACE,
    abort,
  );
}

test("fillTeam fills only unclaimed seats, in order, and reports the present ones", async () => {
  const chief = seatById("chief");
  const writer = seatById("writer");
  const f = fakes({
    bots: [[bot("b1", chief.profileId as string), bot("b2", writer.profileId as string)]],
  });
  const result = await team(f);
  const rest = TEAM.filter((s) => s !== chief && s !== writer);
  expect(result.present).toEqual([chief, writer]);
  expect(result.added).toEqual(rest);
  expect(result.failed).toEqual([]);
  expect(f.api.create.mock.calls.map((call) => call[1])).toEqual(rest.map((s) => s.profileId));
  expect(f.api.chat).not.toHaveBeenCalled();
  untouched(f.api);
});

test("fillTeam continues past capacity, archived and Bot-create failures, keeping each error", async () => {
  const research = seatById("research");
  const capacity = new ProfileApiError(507, "capacity");
  const boom = new TypeError("boom");
  const f = fakes({
    profiles: TEAM.map(() => []),
    createProfile: async (...args: unknown[]) => {
      const [profileId] = args as [string];
      if (profileId === chiefId) throw capacity;
      if (profileId === opsId) throw profileConflict();
      return {};
    },
    createBot: async (...args: unknown[]) => {
      const [id, profileId] = args as [string, string];
      if (profileId === research.profileId) throw boom;
      return { bot: bot(id, profileId), replayed: false };
    },
  });
  const result = await team(f);
  expect(result.failed.map((x) => x.seat.seat)).toEqual(["chief", "ops", "research"]);
  expect(result.failed[0]?.error).toBe(capacity);
  expect(result.failed[1]?.error).toBeInstanceOf(SeatError);
  expect(result.failed[2]?.error).toBe(boom);
  expect(result.added.map((s) => s.seat)).toEqual(
    TEAM.filter((s) => !["chief", "ops", "research"].includes(s.seat)).map((s) => s.seat),
  );
  expect(result.present).toEqual([]);
  expect(f.api.chat).not.toHaveBeenCalled();
});

test("fillTeam rejects on an abort after the first seat and creates nothing more", async () => {
  const controller = new AbortController();
  const f = fakes({
    createBot: async (...args: unknown[]) => {
      const [id, profileId] = args as [string, string];
      controller.abort();
      return { bot: bot(id, profileId), replayed: false };
    },
  });
  await expect(team(f, controller.signal)).rejects.toBeDefined();
  expect(f.profiles.create).toHaveBeenCalledTimes(1);
  expect(f.api.create).toHaveBeenCalledTimes(1);
});

test("fillTeam rethrows an error thrown after the abort", async () => {
  const controller = new AbortController();
  const filled = TEAM.filter((s) => s.profileId !== null && s !== checker);
  const f = fakes({
    bots: [filled.map((s, i) => bot(`b${i}`, s.profileId as string))],
    createBot: async () => {
      controller.abort();
      throw new DOMException("aborted", "AbortError");
    },
  });
  await expect(team(f, controller.signal)).rejects.toThrow(/aborted/);
  expect(f.api.create).toHaveBeenCalledTimes(1);
});

test("fillStarterTeam adopts a Bot that another create raced into existence", async () => {
  const mine = bot("adopted-chief", chiefId);
  const f = fakes({
    bots: [[], [], [mine]],
    createBot: async (...args: unknown[]) => {
      const [id, profileId] = args as [string, string];
      if (profileId === chiefId) throw botConflict();
      return { bot: bot(id, profileId), replayed: false };
    },
  });
  await fillStarterTeam(
    f.api as unknown as BotsApi,
    f.profiles as unknown as ProfileApi,
    created([STARTER_KANBAN_FILE]),
  );
  const seats = TEAM.filter((s) => s.profileId !== null);
  expect(f.api.list).toHaveBeenCalledTimes(seats.length + 2);
  expect(f.api.create.mock.calls.map((call) => call[1])).toEqual(seats.map((s) => s.profileId));
  expect(f.api.chat).not.toHaveBeenCalled();
});
