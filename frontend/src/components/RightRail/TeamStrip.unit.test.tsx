/**
 * TeamStrip: the head, the body states (busy, failed, empty, no vault, members), the tile contents,
 * every click path, and the guards that drop a late answer.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { BotChatResult, BotRow, BotsApi } from "../../clients/messaging/botsApi";
import type { PreparedProfilePreview } from "../../clients/profiles/profileLaunchApi";
import { emptyView } from "../../state/view";
import { narrowBlock, readCss, ruleIn } from "../../testing/css";
import { BotHuesContext } from "../Bots/botHues";
import type { TeamLive } from "../Bots/botStatus";
import { TEAM } from "../Bots/team";
import { READINESS_TEXT, type TeamReadiness } from "../Bots/teamReadiness";
import { TeamStrip } from "./TeamStrip";

afterEach(cleanup);

const WS = "w1";
let counter = 0;

function bot(name: string, over: Partial<BotRow> = {}): BotRow {
  counter += 1;
  return {
    bot_id: `bot-${counter}`,
    profile_id: `profile-${counter}`,
    workspace_id: WS,
    chat_session_id: `session-${counter}`,
    state_revision: 1,
    profile: {
      revision: 1,
      archived: false,
      identity: { name, purpose: "", avatar_asset_id: null, accent: "blue", shape: "circle" },
    },
    workspace: { name: "Home", policy: "ordinary", active: true },
    chat: { profile_revision: 1, started: true, archived: false },
    routine_ids: [],
    ...over,
  } as BotRow;
}

const seatBot = (seat: number, over: Partial<BotRow> = {}) =>
  bot(TEAM[seat]?.name ?? "", { profile_id: TEAM[seat]?.profileId ?? "", ...over });

const LIVE: TeamLive = {
  online: true,
  sessions: {},
  others: [],
  routines: { status: "ready", items: [], scheduler: null, error: null },
  checker: "listed",
  sessionRows: [],
  nowMs: 0,
};

const prepared = (sessionId: string) =>
  ({ launchable: true, session_id: sessionId, profile_id: "p" }) as PreparedProfilePreview;
const BLOCKED = { launchable: false, blockers: ["x"], fields: [], profile_id: "p" } as never;

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (error: unknown) => void;
}
function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function fakeApi(bots: BotRow[] | Error | Promise<never>) {
  return {
    list: vi.fn((_signal?: AbortSignal) =>
      bots instanceof Error
        ? Promise.reject(bots)
        : bots instanceof Promise
          ? bots
          : Promise.resolve({
              bots,
              scheduler: { state: "running", attended: true, paused: false },
            }),
    ),
    chat: vi.fn((_bot: BotRow, _signal?: AbortSignal) => new Promise<BotChatResult>(() => {})),
    create: vi.fn(),
    newChat: vi.fn(),
    remove: vi.fn(),
    reset: vi.fn(),
    setDelivery: vi.fn(),
    get: vi.fn(),
  };
}

function setup(
  bots: BotRow[] | Error | Promise<never>,
  over: {
    readiness?: TeamReadiness;
    live?: Partial<TeamLive>;
    selectedId?: string | null;
    currentBotId?: string | null;
    api?: ReturnType<typeof fakeApi>;
    workspace?: string;
  } = {},
) {
  const api = over.api ?? fakeApi(bots);
  const calls = {
    onOpenSession: vi.fn(),
    onOpenPrepared: vi.fn(),
    onShowBot: vi.fn(),
    onShowSeat: vi.fn(),
    onShowAll: vi.fn(),
  };
  const element = (selectedId: string | null, workspace: string) => (
    <TeamStrip
      api={api as unknown as BotsApi}
      activeWorkspaceId={workspace}
      readiness={over.readiness ?? "ready"}
      live={{ ...LIVE, ...over.live }}
      selectedId={selectedId}
      {...(over.currentBotId !== undefined ? { currentBotId: over.currentBotId } : {})}
      {...calls}
    />
  );
  const view = render(element(over.selectedId ?? null, over.workspace ?? WS));
  return {
    api,
    calls,
    container: view.container,
    unmount: view.unmount,
    rerender: (selectedId: string | null, workspace = over.workspace ?? WS) =>
      view.rerender(element(selectedId, workspace)),
  };
}

const settle = () => act(async () => {});
const names = (container: HTMLElement) =>
  [...container.querySelectorAll(".rail-team-name")].map((n) => n.textContent);

describe("the head and the body states", () => {
  it("shows the head and an empty busy list before the first answer", async () => {
    const { container } = setup(new Promise<never>(() => {}));
    await settle();
    const section = container.querySelector("section.rail-team");
    expect(section?.getAttribute("aria-label")).toBe("Bots");
    expect(section?.querySelector(".rail-team-head .lbl")?.textContent).toBe("Bots");
    const list = container.querySelector("ul.rail-team-members");
    expect(list?.getAttribute("aria-busy")).toBe("true");
    expect(list?.children.length).toBe(0);
    expect(screen.queryByRole("button", { name: /All Bots/ })).toBeNull();
  });

  it("shows the default team while optional Kanban readiness loads", async () => {
    const { container } = setup([], { readiness: "loading" });
    await settle();
    expect(names(container)).toEqual([
      "Chief",
      "Ops",
      "Research",
      "Writer",
      "Engineer",
      "Desk",
      "Checker",
    ]);
    expect(container.querySelector(".rail-team-note")).toBeNull();
  });

  it("shows a failed read as one line", async () => {
    const { container } = setup(new Error("down"));
    await settle();
    expect(container.querySelector(".rail-team-note")?.textContent).toBe(
      "Could not load your Bots.",
    );
    expect(container.querySelector("ul")).toBeNull();
  });

  it("asks to open a vault when no vault is active", async () => {
    const { container } = setup([], { readiness: "no_vault" });
    await settle();
    expect(container.querySelector("p.rail-team-note")?.textContent).toBe(READINESS_TEXT.no_vault);
    expect(container.querySelector(".rail-team-count")).toBeNull();
  });

  it("lists once on mount and again when the vault changes, with no timer", async () => {
    const view = setup([seatBot(0)]);
    await settle();
    expect(view.api.list).toHaveBeenCalledTimes(1);
    const first = view.api.list.mock.calls[0]?.[0];
    view.rerender(null, "w2");
    await settle();
    expect(view.api.list).toHaveBeenCalledTimes(2);
    expect(first?.aborted).toBe(true);
  });

  it("picks up added and removed Bots when the window regains focus", async () => {
    const engineer = bot("Build engineer");
    const otherVault = bot("Other vault", { workspace_id: "w2" });
    const view = setup([]);
    await settle();
    expect(screen.queryByRole("button", { name: "Build engineer" })).toBeNull();

    view.api.list.mockResolvedValue({
      bots: [engineer, otherVault],
      scheduler: { state: "running", attended: true, paused: false },
    });
    fireEvent.focus(window);
    await settle();
    expect(screen.getByRole("button", { name: "Build engineer" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Other vault" })).toBeNull();

    view.api.list.mockResolvedValue({
      bots: [],
      scheduler: { state: "running", attended: true, paused: false },
    });
    fireEvent.focus(window);
    await settle();
    expect(screen.queryByRole("button", { name: "Build engineer" })).toBeNull();
    expect(view.api.list).toHaveBeenCalledTimes(3);

    view.unmount();
    fireEvent.focus(window);
    expect(view.api.list).toHaveBeenCalledTimes(3);
  });

  it("keeps the loaded Bots when a focus refresh fails", async () => {
    const view = setup([bot("Build engineer")]);
    await settle();
    view.api.list.mockRejectedValueOnce(new Error("down"));
    fireEvent.focus(window);
    await settle();
    expect(view.api.list).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("button", { name: "Build engineer" })).toBeTruthy();
  });

  it("drops an older list that arrives after a focus refresh", async () => {
    const old = deferred<never>();
    const view = setup(old.promise);
    const signal = view.api.list.mock.calls[0]?.[0];
    view.api.list.mockResolvedValue({
      bots: [bot("Build engineer")],
      scheduler: { state: "running", attended: true, paused: false },
    });
    fireEvent.focus(window);
    await settle();
    expect(signal?.aborted).toBe(true);
    await act(async () => old.reject(new Error("late")));
    expect(screen.getByRole("button", { name: "Build engineer" })).toBeTruthy();
    expect(view.container.querySelector(".rail-team-note")).toBeNull();
  });
});

describe("the tiles", () => {
  it("names a tile exactly by its Bot, and its title and description equal the status", async () => {
    const { container } = setup([seatBot(0)]);
    await settle();
    const chief = screen.getByRole("button", { name: "Chief" });
    expect(chief.getAttribute("title")).toBe("Idle");
    const described = document.getElementById(chief.getAttribute("aria-describedby") ?? "");
    expect(described?.textContent).toBe("Idle");
    expect(described?.className).toBe("sr-only");
    expect(described?.parentElement).toBe(chief.parentElement);
    expect(chief.contains(described)).toBe(false);
    expect(chief.querySelector(".bot-face")?.getAttribute("aria-hidden")).toBe("true");
    expect(chief.querySelector(".bot-face-star")).not.toBeNull();
    expect(container.querySelector(".rail-team-name")?.textContent).toBe("Chief");
  });

  it("shows unfilled seats in team order without requiring Kanban skills, including Checker", async () => {
    const ready = setup([bot("Mine"), seatBot(2)]);
    await settle();
    expect(names(ready.container)).toEqual([
      "Chief",
      "Ops",
      "Research",
      "Writer",
      "Engineer",
      "Desk",
      "Checker",
      "Mine",
    ]);
    expect(screen.getByRole("button", { name: "Checker" })).toBeTruthy();
    const chief = screen.getByRole("button", { name: "Chief" });
    expect(chief.getAttribute("title")).toBe("Not started");
    cleanup();

    for (const readiness of ["missing", "unknown"] as const) {
      const other = setup([bot("Mine"), seatBot(2)], { readiness });
      await settle();
      expect(names(other.container)).toEqual([
        "Chief",
        "Ops",
        "Research",
        "Writer",
        "Engineer",
        "Desk",
        "Checker",
        "Mine",
      ]);
      cleanup();
    }
  });

  it("counts a live chat with turns as started, without listing again", async () => {
    const mine = bot("Mine", { chat: { profile_revision: 1, started: false, archived: false } });
    const id = mine.chat_session_id;
    const turn = (status: string) => [{ status, blocks: [{ kind: "thinking", done: false }] }];
    const sessions = (turns: unknown) => ({
      [id]: { ...emptyView("a"), turns: turns as never },
    });
    const bots = [mine];
    const api = fakeApi(bots);
    const { rerender } = render(
      <TeamStrip
        api={api as unknown as BotsApi}
        activeWorkspaceId={WS}
        readiness="missing"
        live={{ ...LIVE, sessions: sessions([]) }}
        selectedId={null}
        onOpenSession={vi.fn()}
        onOpenPrepared={vi.fn()}
        onShowBot={vi.fn()}
        onShowSeat={vi.fn()}
        onShowAll={vi.fn()}
      />,
    );
    await settle();
    const title = () => screen.getByRole("button", { name: "Mine" }).getAttribute("title");
    expect(title()).toBe("Not started");
    for (const [turns, expected] of [
      [turn("running"), "Thinking"],
      [turn("done"), "Idle"],
    ] as const) {
      rerender(
        <TeamStrip
          api={api as unknown as BotsApi}
          activeWorkspaceId={WS}
          readiness="missing"
          live={{ ...LIVE, sessions: sessions(turns) }}
          selectedId={null}
          onOpenSession={vi.fn()}
          onOpenPrepared={vi.fn()}
          onShowBot={vi.fn()}
          onShowSeat={vi.fn()}
          onShowAll={vi.fn()}
        />,
      );
      await settle();
      expect(title()).toBe(expected);
    }
    expect(api.list).toHaveBeenCalledTimes(1);
  });

  it("shows every vault Bot after the default seats, including Bots beyond the eighth tile", async () => {
    const many = Array.from({ length: 10 }, (_, i) => bot(`Engineer ${i}`));
    const { container, calls } = setup(many, { readiness: "missing" });
    await settle();
    expect(names(container)).toEqual([
      ...TEAM.map((seat) => seat.name),
      ...many.map((b) => b.profile?.identity.name),
    ]);
    fireEvent.click(screen.getByRole("button", { name: "All Bots" }));
    expect(calls.onShowAll).toHaveBeenCalledTimes(1);
  });

  it("offers All Bots for a small team too", async () => {
    setup([bot("Mine")], { readiness: "missing" });
    await settle();
    expect(screen.getByRole("button", { name: "All Bots" })).toBeTruthy();
  });

  it("counts what needs you and what works over all members", async () => {
    const waiting = seatBot(1);
    const thinking = seatBot(2);
    const { container } = setup([waiting, thinking, bot("Idle one")], {
      live: {
        sessions: {
          [waiting.chat_session_id]: { ...emptyView("a"), cards: [{}] as never },
          [thinking.chat_session_id]: {
            ...emptyView("b"),
            turns: [{ status: "running", blocks: [{ kind: "thinking", done: false }] }] as never,
          },
        },
      },
    });
    await settle();
    expect(container.querySelector(".rail-team-count")?.textContent).toBe(
      "1 needs you · 1 working",
    );
  });

  it("marks only the current Bot tile even when another chat is selected", async () => {
    const mine = seatBot(0);
    const other = seatBot(1);
    setup([mine, other], {
      selectedId: other.chat_session_id,
      currentBotId: mine.bot_id,
      readiness: "missing",
    });
    await settle();
    const tiles = [
      screen.getByRole("button", { name: "Chief" }),
      screen.getByRole("button", { name: "Ops" }),
    ];
    expect(tiles[0]?.getAttribute("aria-current")).toBe("true");
    expect(tiles[0]?.hasAttribute("data-bot-hue")).toBe(true);
    expect(tiles[1]?.hasAttribute("aria-current")).toBe(false);
    expect(tiles[1]?.hasAttribute("data-bot-hue")).toBe(false);
  });

  it("does not mark a tile current from its selected session alone", async () => {
    const mine = bot("Mine");
    setup([mine], { selectedId: mine.chat_session_id, readiness: "missing" });
    await settle();
    expect(screen.getByRole("button", { name: "Mine" }).hasAttribute("aria-current")).toBe(false);
  });

  it("uses the assigned Bot hue on the current tile", async () => {
    const mine = bot("Mine", { profile_id: "profile-pink" });
    render(
      <BotHuesContext.Provider value={{ hues: { "profile-pink": "pink" }, icons: {} }}>
        <TeamStrip
          api={fakeApi([mine]) as unknown as BotsApi}
          activeWorkspaceId={WS}
          readiness="missing"
          live={LIVE}
          selectedId={null}
          currentBotId={mine.bot_id}
          onOpenSession={vi.fn()}
          onOpenPrepared={vi.fn()}
          onShowBot={vi.fn()}
          onShowSeat={vi.fn()}
          onShowAll={vi.fn()}
        />
      </BotHuesContext.Provider>,
    );
    await settle();
    expect(screen.getByRole("button", { name: "Mine" }).getAttribute("data-bot-hue")).toBe("pink");
  });

  it("styles hover and current states separately, with forced-colour focus intact", () => {
    const css = readCss(import.meta.url, "TeamStrip.css");
    const hover = ruleIn(css, ".rail-team-member:hover");
    expect(hover).toMatch(/background:\s*var\(--s1\)/);
    expect(hover).not.toMatch(/box-shadow/);
    expect(ruleIn(css, '.rail-team-member[aria-current="true"]')).toMatch(
      /box-shadow:\s*inset 0 0 0 2px var\(--bot-hue\)/,
    );
    const forced = narrowBlock(css, "@media (forced-colors: active)");
    expect(ruleIn(forced, '.rail-team-member[aria-current="true"]')).toMatch(
      /outline:\s*2px solid Highlight/,
    );
  });

  it("draws a Bot whose profile is gone from its name, as unavailable", async () => {
    setup([bot("x", { profile: null })], { readiness: "missing" });
    await settle();
    const tile = screen.getByRole("button", { name: "Unavailable profile" });
    expect(tile.getAttribute("title")).toBe("Its profile is unavailable.");
    const face = tile.querySelector(".bot-face");
    expect(face?.getAttribute("data-state")).toBe("unavailable");
    expect(face?.querySelector("svg.bot-icon")).not.toBeNull();
    expect(face?.querySelector(".bot-face-star")).toBeNull();
  });

  it("uses none of the Agents section's selectors", async () => {
    const { container } = setup([seatBot(0)]);
    await settle();
    for (const selector of [".agent", ".s", ".s-on", ".chip", ".tile", ".tile-n"])
      expect(container.querySelector(selector), selector).toBeNull();
    expect(container.querySelector(".rail-section-count")).toBeNull();
    expect(container.querySelector("[data-agent]")).toBeNull();
  });
});

describe("a click", () => {
  it("shows an unfilled seat", async () => {
    const { calls, api } = setup([]);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Writer" }));
    expect(calls.onShowSeat).toHaveBeenCalledWith(TEAM[3]);
    expect(api.chat).not.toHaveBeenCalled();
  });

  it("shows an unavailable Bot", async () => {
    const gone = bot("x", { profile: null });
    const { calls, api } = setup([gone], { readiness: "missing" });
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Unavailable profile" }));
    expect(calls.onShowBot).toHaveBeenCalledWith(gone.bot_id);
    expect(api.chat).not.toHaveBeenCalled();
  });

  it("opens a chat that is already attached", async () => {
    const mine = bot("Mine");
    const { calls, api } = setup([mine], {
      readiness: "missing",
      live: { sessions: { [mine.chat_session_id]: emptyView("a") } },
    });
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Mine" }));
    expect(calls.onOpenSession).toHaveBeenCalledWith(mine.chat_session_id);
    expect(api.chat).not.toHaveBeenCalled();
  });

  it("opens the prepared chat of a Bot that is not attached", async () => {
    const mine = bot("Mine");
    const { calls, api } = setup([mine], { readiness: "missing" });
    await settle();
    const chat = deferred<BotChatResult>();
    api.chat.mockReturnValueOnce(chat.promise);
    fireEvent.click(screen.getByRole("button", { name: "Mine" }));
    expect(api.chat).toHaveBeenCalledTimes(1);
    expect(api.chat.mock.calls[0]?.[0]).toBe(mine);
    const preview = prepared(mine.chat_session_id);
    await act(async () => chat.resolve({ bot_id: mine.bot_id, state_revision: 1, preview }));
    expect(calls.onOpenPrepared).toHaveBeenCalledWith(preview, mine);
    expect(calls.onShowBot).not.toHaveBeenCalled();
  });

  it("shows the Bot when its preview is blocked or the read fails", async () => {
    const mine = bot("Mine");
    const { calls, api } = setup([mine], { readiness: "missing" });
    await settle();
    api.chat.mockResolvedValueOnce({ bot_id: mine.bot_id, state_revision: 1, preview: BLOCKED });
    fireEvent.click(screen.getByRole("button", { name: "Mine" }));
    await settle();
    expect(calls.onShowBot).toHaveBeenCalledTimes(1);
    api.chat.mockRejectedValueOnce(new Error("no"));
    fireEvent.click(screen.getByRole("button", { name: "Mine" }));
    await settle();
    expect(calls.onShowBot).toHaveBeenCalledTimes(2);
    expect(calls.onShowBot).toHaveBeenLastCalledWith(mine.bot_id);
    expect(calls.onOpenPrepared).not.toHaveBeenCalled();
  });

  it("reads one chat at a time", async () => {
    // The seven seats and A fill the strip's eight tiles, so a second plain Bot would sit
    // past the cap: click A and the Chief seat's Bot.
    const { api } = setup([bot("A"), seatBot(0)], { readiness: "missing" });
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "A" }));
    fireEvent.click(screen.getByRole("button", { name: "Chief" }));
    fireEvent.click(screen.getByRole("button", { name: "A" }));
    expect(api.chat).toHaveBeenCalledTimes(1);
  });
});

describe("a late answer", () => {
  async function pendingChat() {
    const mine = bot("Mine");
    const view = setup([mine], { readiness: "missing", selectedId: "here" });
    await settle();
    const chat = deferred<BotChatResult>();
    view.api.chat.mockReturnValueOnce(chat.promise);
    fireEvent.click(screen.getByRole("button", { name: "Mine" }));
    const signal = view.api.chat.mock.calls[0]?.[1] as AbortSignal;
    const answer = (preview: unknown) =>
      act(async () =>
        chat.resolve({ bot_id: mine.bot_id, state_revision: 1, preview: preview as never }),
      );
    return { ...view, mine, signal, answer };
  }

  it("is dropped when the selection changed since the click", async () => {
    const { calls, mine, rerender, answer } = await pendingChat();
    rerender("elsewhere");
    await answer(prepared(mine.chat_session_id));
    expect(calls.onOpenPrepared).not.toHaveBeenCalled();
    expect(calls.onShowBot).not.toHaveBeenCalled();
  });

  it("is dropped for a blocked preview when the selection changed", async () => {
    const { calls, rerender, answer } = await pendingChat();
    rerender(null);
    await answer(BLOCKED);
    expect(calls.onShowBot).not.toHaveBeenCalled();
  });

  it("is dropped when the vault changed", async () => {
    const { calls, mine, signal, rerender, answer } = await pendingChat();
    rerender("here", "w2");
    expect(signal.aborted).toBe(true);
    await answer(prepared(mine.chat_session_id));
    expect(calls.onOpenPrepared).not.toHaveBeenCalled();
  });

  it("is dropped on unmount, which aborts the read", async () => {
    const { calls, mine, signal, unmount, answer } = await pendingChat();
    unmount();
    expect(signal.aborted).toBe(true);
    await answer(prepared(mine.chat_session_id));
    expect(calls.onOpenPrepared).not.toHaveBeenCalled();
  });

  it("drops a list that arrives after unmount", async () => {
    const list = deferred<never>();
    const view = setup(list.promise);
    const signal = view.api.list.mock.calls[0]?.[0];
    view.unmount();
    expect(signal?.aborted).toBe(true);
    await act(async () => list.reject(new Error("late")));
  });

  it("ends the busy wait when the answer comes, so the next click reads again", async () => {
    const { api, mine, answer } = await pendingChat();
    await answer(prepared(mine.chat_session_id));
    fireEvent.click(screen.getByRole("button", { name: "Mine" }));
    expect(api.chat).toHaveBeenCalledTimes(2);
  });
});

describe("a read-only strip", () => {
  it("never creates, resets or changes a Bot", async () => {
    const mine = bot("Mine");
    const { api } = setup([mine, seatBot(0)], { readiness: "ready" });
    await settle();
    for (const name of ["Chief", "Ops", "Mine"]) {
      fireEvent.click(screen.getByRole("button", { name }));
      await settle();
    }
    fireEvent.click(screen.getByRole("button", { name: "All Bots" }));
    for (const call of [api.create, api.newChat, api.remove, api.reset, api.setDelivery])
      expect(call).not.toHaveBeenCalled();
  });
});
