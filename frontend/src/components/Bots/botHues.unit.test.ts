import { describe, expect, it, vi } from "vitest";
import type { BotRow, BotsApi } from "../../clients/messaging/botsApi";
import {
  assigningHues,
  BOT_HUES_KEY,
  BOT_ICONS_KEY,
  type BotHues,
  type BotIcons,
  botHue,
  createBotLookQueue,
  hueAssigner,
  leastUsedHue,
  parseBotHues,
  parseBotIcons,
  withBotHue,
  withBotIcon,
} from "./botHues";
import { designFor, type Hue } from "./botIconDesign";

function row(botId: string, profileId: string, name: string | null): BotRow {
  return {
    bot_id: botId,
    profile_id: profileId,
    profile: name
      ? {
          revision: 1,
          archived: false,
          identity: { name, purpose: "", avatar_asset_id: null, accent: "slate", shape: "rounded" },
        }
      : null,
  } as BotRow;
}

function fakeApi(create: BotsApi["create"]): BotsApi {
  return {
    list: vi.fn(),
    get: vi.fn(),
    create,
    chat: vi.fn(),
    newChat: vi.fn(),
    setDelivery: vi.fn(),
    remove: vi.fn(),
    reset: vi.fn(),
    groups: vi.fn(),
    saveGroup: vi.fn(),
    removeGroup: vi.fn(),
    threads: vi.fn(),
  } as unknown as BotsApi;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

it("names the stored settings key", () => {
  expect(BOT_HUES_KEY).toBe("bots.hues");
  expect(BOT_ICONS_KEY).toBe("bots.icons");
});

describe("parseBotIcons", () => {
  const ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";

  it("keeps canonical UUID keys with known icon ids", () => {
    expect(
      parseBotIcons(
        JSON.stringify({
          [ID]: "door",
          [ID.toUpperCase()]: "loop",
          "not-a-uuid": "loop",
          "22222222-2222-2222-2222-222222222222": "unknown",
        }),
      ),
    ).toEqual({ [ID]: "door" });
  });

  it("returns an empty map for malformed input", () => {
    for (const bad of ["not json", "[]", "null", 42, undefined]) {
      expect(parseBotIcons(bad)).toEqual({});
    }
  });
});

it("writes and clears sorted bot icon settings", () => {
  expect(withBotIcon({ b: "door" }, "a", "loop")).toBe('{"a":"loop","b":"door"}');
  expect(withBotIcon({ a: "door", b: "loop" }, "a", null)).toBe('{"b":"loop"}');
});

describe("createBotLookQueue", () => {
  const p1 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
  const p2 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb";

  it("coalesces pending fields, preserves first Bot order and settles overwritten callers", async () => {
    const maps = { hues: {} as BotHues, icons: {} as BotIcons };
    const first = deferred<void>();
    const writes: Record<string, unknown>[] = [];
    const queue = createBotLookQueue({
      read: () => maps,
      patch: async (changes) => {
        writes.push(changes);
        if (writes.length === 1) await first.promise;
        maps.hues = parseBotHues(changes[BOT_HUES_KEY]);
        maps.icons = parseBotIcons(changes[BOT_ICONS_KEY]);
      },
    });

    const inFlight = queue.set(p1, { hue: "blue" });
    const oldHue = queue.set(p1, { hue: "red" });
    const icon = queue.set(p1, { icon: "door" });
    const latestHue = queue.set(p1, { hue: "pink" });
    const other = queue.set(p2, { icon: "loop" });
    first.resolve();

    await expect(inFlight).resolves.toBe("saved");
    await expect(oldHue).resolves.toBe("superseded");
    await expect(icon).resolves.toBe("saved");
    await expect(latestHue).resolves.toBe("saved");
    await expect(other).resolves.toBe("saved");
    expect(writes).toHaveLength(3);
    expect(writes.map((write) => write[BOT_HUES_KEY])).toEqual([
      '{"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa":"blue"}',
      '{"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa":"pink"}',
      '{"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa":"pink"}',
    ]);
    expect(writes[1]?.[BOT_ICONS_KEY]).toBe('{"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa":"door"}');
    expect(parseBotIcons(writes[2]?.[BOT_ICONS_KEY])).toEqual({ [p1]: "door", [p2]: "loop" });
    expect(maps.hues).toEqual({ [p1]: "pink" });
    expect(maps.icons).toEqual({ [p2]: "loop", [p1]: "door" });
  });

  it.each([
    ["hue", { hue: "red" }, { hue: "green" }],
    ["icon", { icon: "door" }, { icon: "loop" }],
  ] as const)(
    "supersedes the first %s choice after a pending ABA sequence",
    async (_field, a, b) => {
      const first = deferred<void>();
      const writes: Record<string, unknown>[] = [];
      const queue = createBotLookQueue({
        read: () => ({ hues: {}, icons: {} }),
        patch: async (changes) => {
          writes.push(changes);
          if (writes.length === 1) await first.promise;
        },
      });
      const inFlight = queue.set(p1, { hue: "blue" });
      const earlierA = queue.set(p1, a);
      const overwrittenB = queue.set(p1, b);
      const latestA = queue.set(p1, a);
      first.resolve();

      await expect(inFlight).resolves.toBe("saved");
      await expect(earlierA).resolves.toBe("superseded");
      await expect(overwrittenB).resolves.toBe("superseded");
      await expect(latestA).resolves.toBe("saved");
      expect(writes).toHaveLength(2);
      expect(writes[1]?.[_field === "hue" ? BOT_HUES_KEY : BOT_ICONS_KEY]).toBe(
        JSON.stringify({ [p1]: _field === "hue" ? "red" : "door" }),
      );
    },
  );

  it.each([
    ["hue", { hue: "green" }, { hue: "red" }],
    ["icon", { icon: "loop" }, { icon: "door" }],
  ] as const)(
    "supersedes a mixed choice when its %s is overwritten",
    async (_field, middle, last) => {
      const first = deferred<void>();
      const writes: Record<string, unknown>[] = [];
      const queue = createBotLookQueue({
        read: () => ({ hues: {}, icons: {} }),
        patch: async (changes) => {
          writes.push(changes);
          if (writes.length === 1) await first.promise;
        },
      });
      const inFlight = queue.set(p2, { hue: "blue" });
      const mixed = queue.set(p1, { hue: "red", icon: "door" });
      const overwritten = queue.set(p1, middle);
      const latest = queue.set(p1, last);
      first.resolve();

      await expect(inFlight).resolves.toBe("saved");
      await expect(mixed).resolves.toBe("superseded");
      await expect(overwritten).resolves.toBe("superseded");
      await expect(latest).resolves.toBe("saved");
      expect(writes).toHaveLength(2);
      expect(writes[1]).toEqual({
        [BOT_HUES_KEY]: `{"${p1}":"red"}`,
        [BOT_ICONS_KEY]: `{"${p1}":"door"}`,
      });
    },
  );

  it("rejects a failed batch and continues with the next queued Bot", async () => {
    const maps = { hues: {} as BotHues, icons: {} as BotIcons };
    const first = deferred<void>();
    const error = new Error("patch failed");
    const writes: Record<string, unknown>[] = [];
    const queue = createBotLookQueue({
      read: () => maps,
      patch: async (changes) => {
        writes.push(changes);
        if (writes.length === 1) await first.promise;
        if (writes.length === 2) throw error;
        maps.hues = parseBotHues(changes[BOT_HUES_KEY]);
        maps.icons = parseBotIcons(changes[BOT_ICONS_KEY]);
      },
    });
    const inFlight = queue.set(p1, { hue: "blue" });
    const failed0 = queue.set(p1, { hue: "red" });
    const failed1 = queue.set(p1, { icon: "door" });
    const failed2 = queue.set(p1, { hue: "amber" });
    const next = queue.set(p2, { hue: "green" });
    first.resolve();
    await expect(inFlight).resolves.toBe("saved");
    await expect(failed0).rejects.toBe(error);
    await expect(failed1).rejects.toBe(error);
    await expect(failed2).rejects.toBe(error);
    await expect(next).resolves.toBe("saved");
    expect(writes).toHaveLength(3);
    expect(maps).toEqual({ hues: { [p1]: "blue", [p2]: "green" }, icons: {} });
  });

  it("merges an automatic assignment with a later manual choice from confirmed state", async () => {
    const maps = { hues: {} as BotHues, icons: {} as BotIcons };
    const first = deferred<void>();
    const writes: Record<string, unknown>[] = [];
    const queue = createBotLookQueue({
      read: () => maps,
      patch: async (changes) => {
        writes.push(changes);
        if (writes.length === 1) await first.promise;
        maps.hues = parseBotHues(changes[BOT_HUES_KEY]);
        maps.icons = parseBotIcons(changes[BOT_ICONS_KEY]);
      },
    });
    const initial = queue.set(p1, { icon: "loop" });
    const automatic = queue.assign(p1, (confirmed) => (p1 in confirmed ? undefined : "blue"));
    const manual = queue.set(p1, { hue: "pink", icon: "door" });
    first.resolve();

    await expect(initial).resolves.toBe("saved");
    await expect(automatic).resolves.toBeUndefined();
    await expect(manual).resolves.toBe("saved");
    expect(writes).toHaveLength(2);
    expect(writes[1]).toEqual({
      [BOT_HUES_KEY]: `{"${p1}":"pink"}`,
      [BOT_ICONS_KEY]: `{"${p1}":"door"}`,
    });
    expect(maps).toEqual({ hues: { [p1]: "pink" }, icons: { [p1]: "door" } });
  });
});

describe("parseBotHues", () => {
  const ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";

  it("keeps a canonical lower-case UUID key with a BOT_HUES value, and drops the rest", () => {
    const text = JSON.stringify({
      [ID]: "amber",
      [ID.toUpperCase()]: "pink", // not canonical lower-case
      "not-a-uuid": "pink",
      "22222222-2222-2222-2222-222222222222": "chartreuse", // not a BOT_HUES value
    });
    expect(parseBotHues(text)).toEqual({ [ID]: "amber" });
  });

  it("drops to {} on bad text, a non-object, an array, or a non-string input", () => {
    for (const bad of ["not json", "[]", "null", "42", '"text"', undefined, null, 42, {}]) {
      expect(parseBotHues(bad)).toEqual({});
    }
  });
});

describe("botHue", () => {
  it("gives a seat's stored entry priority over its fixed hue", () => {
    expect(botHue("p1", "Chief", {})).toBe("orange"); // no stored entry: falls back to the fixed hue
    expect(botHue("p1", "chief", { p1: "pink" })).toBe("pink"); // a stored entry wins, even for a seat
  });

  it("gives a stored profile its stored hue over its drawn one", () => {
    expect(designFor("p1", "Planner").hue).not.toBe("pink"); // drawn hues are never amber or pink
    expect(botHue("p1", "Planner", { p1: "pink" })).toBe("pink");
  });

  it("falls back to the drawn hue with no stored entry for that profile", () => {
    expect(botHue("p1", "Planner", {})).toBe(designFor("p1", "Planner").hue);
    expect(botHue("p1", "Planner", { other: "pink" })).toBe(designFor("p1", "Planner").hue);
  });
});

describe("leastUsedHue", () => {
  const CORE_SEVEN = [
    { profileId: "chief", name: "Chief" },
    { profileId: "ops", name: "Ops" },
    { profileId: "checker", name: "Checker" },
    { profileId: "research", name: "Research" },
    { profileId: "writer", name: "Writer" },
    { profileId: "engineer", name: "Engineer" },
    { profileId: "desk", name: "Desk" },
  ];
  const BUILDER = { profileId: "builder", name: "Builder" };

  it("picks the first BOT_HUES entry when nothing is used yet", () => {
    expect(leastUsedHue([], {})).toBe("blue");
  });

  it("picks pink once the seven core seats cover the drawn pool and amber, with or without Builder", () => {
    expect(leastUsedHue(CORE_SEVEN, {})).toBe("pink");
    expect(leastUsedHue([...CORE_SEVEN, BUILDER], {})).toBe("pink");
  });

  it("counts a profile once no matter how many times it is listed", () => {
    const eight = [
      ...CORE_SEVEN,
      { profileId: "checker", name: "Checker" }, // duplicate of an already-listed profile
      { profileId: "assistant", name: "Assistant" }, // stored pink below, completes the 8-way tie
    ];
    expect(leastUsedHue(eight, { assistant: "pink" })).toBe("blue");
  });
});

describe("withBotHue", () => {
  it("returns sorted-key, compact JSON text", () => {
    expect(withBotHue({ b: "pink" }, "a", "amber")).toBe('{"a":"amber","b":"pink"}');
  });

  it("overwrites an existing entry for the same profile", () => {
    expect(withBotHue({ a: "pink" }, "a", "amber")).toBe('{"a":"amber"}');
  });
});

describe("hueAssigner", () => {
  it("skips a Bot with no profile", async () => {
    const list = vi.fn();
    const assign = vi.fn();
    await hueAssigner({ list, assign })(row("b1", "p1", null));
    expect(list).not.toHaveBeenCalled();
    expect(assign).not.toHaveBeenCalled();
  });

  it("skips a Bot named after a seat", async () => {
    const list = vi.fn();
    const assign = vi.fn();
    await hueAssigner({ list, assign })(row("b1", "p1", "Chief"));
    expect(list).not.toHaveBeenCalled();
    expect(assign).not.toHaveBeenCalled();
  });

  it("skips a profile that already has another Bot row", async () => {
    const list = vi
      .fn()
      .mockResolvedValue([row("other", "p1", "Other"), row("b1", "p1", "Planner")]);
    const assign = vi.fn();
    await hueAssigner({ list, assign })(row("b1", "p1", "Planner"));
    expect(list).toHaveBeenCalledTimes(1);
    expect(assign).not.toHaveBeenCalled();
  });

  it("defers the stored-entry check to assignment dequeue", async () => {
    const list = vi.fn().mockResolvedValue([row("b1", "p1", "Planner")]);
    const assign = vi.fn(
      async (_profileId: string, chooseHue: (hues: BotHues) => Hue | undefined) => {
        expect(chooseHue({ p1: "pink" })).toBeUndefined();
      },
    );
    await hueAssigner({ list, assign })(row("b1", "p1", "Planner"));
    expect(list).toHaveBeenCalledTimes(1);
    expect(assign).toHaveBeenCalledTimes(1);
  });

  it("chooses the least-used hue over other Bots at dequeue", async () => {
    const list = vi.fn().mockResolvedValue([row("b1", "p1", "Planner"), row("b2", "c1", "Chief")]);
    const assign = vi.fn(
      async (_profileId: string, chooseHue: (hues: BotHues) => Hue | undefined) => {
        expect(chooseHue({})).toBe("blue");
      },
    );
    await hueAssigner({ list, assign })(row("b1", "p1", "Planner"));
    // Chief (orange) is the only other distinct profile; blue is BOT_HUES's first unused entry.
    expect(assign).toHaveBeenCalledWith("p1", expect.any(Function));
  });
});

describe("assigningHues", () => {
  it("passes every other method through unchanged, including the optional ones", () => {
    const api = fakeApi(vi.fn());
    const wrapped = assigningHues(api, vi.fn());
    expect(wrapped.list).toBe(api.list);
    expect(wrapped.get).toBe(api.get);
    expect(wrapped.chat).toBe(api.chat);
    expect(wrapped.newChat).toBe(api.newChat);
    expect(wrapped.setDelivery).toBe(api.setDelivery);
    expect(wrapped.remove).toBe(api.remove);
    expect(wrapped.reset).toBe(api.reset);
    expect(wrapped.groups).toBe(api.groups);
    expect(wrapped.saveGroup).toBe(api.saveGroup);
    expect(wrapped.removeGroup).toBe(api.removeGroup);
    expect(wrapped.threads).toBe(api.threads);
  });

  it("assigns nothing for a replayed create", async () => {
    const bot = row("b1", "p1", "Planner");
    const create = vi.fn().mockResolvedValue({ bot, replayed: true });
    const assign = vi.fn();
    const result = await assigningHues(fakeApi(create), assign).create("b1", "p1", 1, "w1");
    expect(result).toEqual({ bot, replayed: true });
    expect(assign).not.toHaveBeenCalled();
  });

  it("passes a create error straight through, without assigning", async () => {
    const error = new Error("boom");
    const create = vi.fn().mockRejectedValue(error);
    const assign = vi.fn();
    await expect(assigningHues(fakeApi(create), assign).create("b1", "p1", 1, "w1")).rejects.toBe(
      error,
    );
    expect(assign).not.toHaveBeenCalled();
  });

  it("still returns the create result when assign fails, for a non-replayed create", async () => {
    const bot = row("b1", "p1", "Planner");
    const create = vi.fn().mockResolvedValue({ bot, replayed: false });
    const assign = vi.fn().mockRejectedValue(new Error("write failed"));
    const result = await assigningHues(fakeApi(create), assign).create("b1", "p1", 1, "w1");
    expect(result).toEqual({ bot, replayed: false });
    expect(assign).toHaveBeenCalledWith(bot);
  });

  it("keeps the queue usable after a failed assign, so a later create still gets one", async () => {
    const bot1 = row("b1", "p1", "Planner");
    const bot2 = row("b2", "p2", "Scout");
    const create = vi
      .fn()
      .mockResolvedValueOnce({ bot: bot1, replayed: false })
      .mockResolvedValueOnce({ bot: bot2, replayed: false });
    const assign = vi
      .fn()
      .mockRejectedValueOnce(new Error("write failed"))
      .mockResolvedValueOnce(undefined);
    const wrapped = assigningHues(fakeApi(create), assign);

    await wrapped.create("b1", "p1", 1, "w1");
    await wrapped.create("b2", "p2", 1, "w1");

    expect(assign).toHaveBeenCalledTimes(2);
    expect(assign).toHaveBeenNthCalledWith(1, bot1);
    expect(assign).toHaveBeenNthCalledWith(2, bot2);
  });

  it("still returns the create result when the write itself fails", async () => {
    const bot = row("b1", "p1", "Planner");
    const create = vi.fn().mockResolvedValue({ bot, replayed: false });
    const list = vi.fn().mockResolvedValue([bot]);
    const assign = vi.fn().mockRejectedValue(new Error("settings patch failed"));
    const result = await assigningHues(fakeApi(create), hueAssigner({ list, assign })).create(
      "b1",
      "p1",
      1,
      "w1",
    );
    expect(result).toEqual({ bot, replayed: false });
    expect(assign).toHaveBeenCalled();
  });

  it("keeps automatic list and assignment in create order through the shared settings queue", async () => {
    const p1 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
    const p2 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb";
    const a = row("b1", p1, "Planner");
    const b = row("b2", p2, "Scout");
    expect(botHue(p1, "Planner", {})).toBe("red");
    const firstListStarted = deferred<void>();
    const releaseFirstList = deferred<void>();
    let listCalls = 0;
    const list = vi.fn(async () => {
      listCalls += 1;
      if (listCalls === 1) {
        firstListStarted.resolve();
        await releaseFirstList.promise;
        return [a];
      }
      return [a, b];
    });
    const maps = { hues: {} as BotHues, icons: {} as BotIcons };
    const writes: Record<string, unknown>[] = [];
    const queue = createBotLookQueue({
      read: () => maps,
      patch: async (changes) => {
        writes.push(changes);
        maps.hues = parseBotHues(changes[BOT_HUES_KEY]);
        maps.icons = parseBotIcons(changes[BOT_ICONS_KEY]);
      },
    });
    const create = vi
      .fn()
      .mockResolvedValueOnce({ bot: a, replayed: false })
      .mockResolvedValueOnce({ bot: b, replayed: false });
    const wrapped = assigningHues(fakeApi(create), hueAssigner({ list, assign: queue.assign }));

    const first = wrapped.create("b1", p1, 1, "w1");
    await firstListStarted.promise;
    const second = wrapped.create("b2", p2, 1, "w1");
    await Promise.resolve();
    await Promise.resolve();
    releaseFirstList.resolve();
    await Promise.all([first, second]);

    expect(list).toHaveBeenCalledTimes(2);
    expect(writes).toHaveLength(2);
    expect(maps.hues).toEqual({ [p1]: "blue", [p2]: "green" });
  });

  it("assigns each non-replayed create", async () => {
    const first = row("b1", "p1", "Planner");
    const second = row("b2", "p2", "Scout");
    const create = vi
      .fn()
      .mockResolvedValueOnce({ bot: first, replayed: false })
      .mockResolvedValueOnce({ bot: second, replayed: false });
    const assign = vi.fn(async () => {});
    const wrapped = assigningHues(fakeApi(create), assign);
    await Promise.all([wrapped.create("b1", "p1", 1, "w1"), wrapped.create("b2", "p2", 1, "w1")]);
    expect(assign).toHaveBeenNthCalledWith(1, first);
    expect(assign).toHaveBeenNthCalledWith(2, second);
  });
});
