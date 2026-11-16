import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { BotRow, BotsApi } from "../../clients/messaging/botsApi";
import { t } from "../../i18n/t";
import type { DisplayMetadata } from "../../state/noteMetadata";
import type { VaultApi, VaultMetadataBatch, VaultMetadataItem } from "../../state/vaultApi";
import { TEAM } from "../Bots/team";
import { READINESS_TEXT } from "../Bots/teamReadiness";
import { KanbanView, type KanbanViewProps } from "./KanbanView";

afterEach(cleanup);

interface Deferred<T> {
  promise: Promise<T>;
  resolve(value: T): void;
  reject(reason: unknown): void;
}
function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((ok, fail) => {
    resolve = ok;
    reject = fail;
  });
  return { promise, resolve, reject };
}

const meta = (over: Partial<DisplayMetadata> = {}): DisplayMetadata => ({
  state: "ready",
  frontmatter_state: "ready",
  heading_state: "absent",
  display_title: null,
  title_source: null,
  tags: [],
  type: "card",
  status: "todo",
  ...over,
});
const item = (ref: string, over: Partial<DisplayMetadata> = {}): VaultMetadataItem => ({
  ref,
  state: "ready",
  reason: null,
  read_bytes: 1,
  display_metadata: meta(over),
});
const batchOf = (items: VaultMetadataItem[]): VaultMetadataBatch => ({
  items,
  partial: false,
  partial_reasons: [],
  read_bytes: items.length,
  omitted: 0,
});
const refsOf = (count: number) => Array.from({ length: count }, (_, i) => `kanban/n${i}.md`);
const listingOf = (refs: string[], over: Record<string, unknown> = {}) => ({
  entries: refs.map((ref) => ({ ref })),
  partial: false,
  partial_reasons: [],
  ...over,
});

type Api = Pick<VaultApi, "entries" | "metadata">;
function setup(api: Api, over: Partial<KanbanViewProps> = {}) {
  const onOpenNote = vi.fn();
  const props: KanbanViewProps = {
    api,
    workspaceId: "w1",
    onOpenNote,
    readiness: "unknown",
    onOpenSeat: vi.fn(),
    onRefresh: vi.fn(),
    ...over,
  };
  const view = render(<KanbanView {...props} />);
  return { onOpenNote, ...view, props };
}
const groupNamed = (name: string) => within(screen.getByRole("region", { name }));
const cardOf = (ref: string) => screen.getByText(ref).closest("li") as HTMLElement;

const scheduler = { state: "running", attended: true, paused: false } as const;
type Bots = Pick<BotsApi, "list">;
const botsOf = (bots: BotRow[]): Bots => ({ list: async () => ({ bots, scheduler }) });
const botNamed = (name: string, profileId: string): BotRow =>
  ({
    bot_id: `bot-${name}`,
    profile_id: profileId,
    workspace_id: "w1",
    profile: {
      revision: 1,
      archived: false,
      identity: { name, purpose: "", avatar_asset_id: null, accent: "blue", shape: "circle" },
    },
  }) as BotRow;
const seatProfileId = (seat: string) => TEAM.find((s) => s.seat === seat)?.profileId ?? "";
const ownersApi = (cards: Record<string, string[]>): Api => ({
  entries: async () => listingOf(Object.keys(cards)),
  metadata: async (_id, asked) =>
    batchOf(asked.map((ref) => item(ref, { tags: cards[ref] ?? [] }))),
});
const tagName = (li: HTMLElement) => li.querySelector(".bot-tag-name")?.textContent;
const at = (day: number, hour: number, minute = 0) =>
  new Date(2026, 10, day, hour, minute).getTime();

describe("KanbanView", () => {
  it("renders cards with their ref, owner and text-only title", async () => {
    const title = "<img src=x onerror=alert(1)>";
    const api: Api = {
      entries: async () => listingOf(["kanban/a.md"]),
      metadata: async () =>
        batchOf([item("kanban/a.md", { display_title: title, tags: ["owner/ann"] })]),
    };
    const { container, onOpenNote } = setup(api);
    const button = await screen.findByRole("button", { name: title });
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText("Owner:")).toBeTruthy();
    expect(screen.getByText("ann")).toBeTruthy();
    const described = button.getAttribute("aria-describedby") ?? "";
    expect(document.getElementById(described)?.textContent).toBe("kanban/a.md");
    expect(screen.getByRole("heading", { name: "To do (1)" })).toBeTruthy();
    button.click();
    expect(onOpenNote).toHaveBeenCalledWith("kanban/a.md");
  });

  it("heads a lane with its status glyph, name and count", async () => {
    const api: Api = {
      entries: async () => listingOf(["kanban/a.md"]),
      metadata: async () => batchOf([item("kanban/a.md")]),
    };
    const { container } = setup(api);
    await screen.findByRole("button", { name: "a" });
    const head = container.querySelector('[data-stage="todo"] h3') as HTMLElement;
    expect(head.querySelector('svg[data-icon="statusTodo"]')).toBeTruthy();
    expect(head.querySelector(".kanban-count")?.textContent).toBe("1");
    expect(screen.getByRole("heading", { name: "To do (1)" })).toBe(head);
    expect(groupNamed("To do (1)").getByRole("button", { name: "a" })).toBeTruthy();
  });

  it("shows the five status lanes in order on an empty listing", async () => {
    const { container } = setup({ entries: async () => listingOf([]) });
    await screen.findByText(t("kanban.empty"));
    expect(
      [...container.querySelectorAll(".kanban-lane")].map((lane) =>
        lane.getAttribute("data-stage"),
      ),
    ).toEqual(["todo", "working", "blocked", "review", "done"]);
    expect(
      screen.getAllByRole("heading", { level: 3 }).map((h) => h.getAttribute("aria-label")),
    ).toEqual(["To do (0)", "Working (0)", "Blocked (0)", "Ready for review (0)", "Done (0)"]);
    expect(
      [...container.querySelectorAll(".kanban-lane h3 svg")].map((svg) =>
        svg.getAttribute("data-icon"),
      ),
    ).toEqual(["statusTodo", "statusWorking", "statusBlocked", "statusReview", "statusDone"]);
    expect(screen.getAllByText("No cards.")).toHaveLength(5);
  });

  it("resolves an owner slug to a seat's Bot, the seat's face or plain text", async () => {
    const scout = botNamed("Scout", seatProfileId("research"));
    setup(
      ownersApi({
        "kanban/r.md": ["owner/research"],
        "kanban/e.md": ["owner/engineer"],
        "kanban/a.md": ["owner/ann"],
        "kanban/c.md": ["owner/Research"],
        "kanban/n.md": [],
      }),
      { botsApi: botsOf([scout]) },
    );
    await screen.findByRole("button", { name: "r" });
    await waitFor(() => expect(tagName(cardOf("kanban/r.md"))).toBe("Scout"));
    expect(tagName(cardOf("kanban/e.md"))).toBe("Engineer");
    for (const ref of ["kanban/a.md", "kanban/c.md"]) {
      expect(cardOf(ref).querySelector(".bot-tag")).toBeNull();
    }
    expect(within(cardOf("kanban/a.md")).getByText("ann")).toBeTruthy();
    expect(within(cardOf("kanban/c.md")).getByText("Research")).toBeTruthy();
    expect(within(cardOf("kanban/n.md")).getByText("No owner")).toBeTruthy();
  });

  it("keeps the seat faces when the Bots list fails", async () => {
    const list = vi.fn<Bots["list"]>().mockRejectedValue(new Error("down"));
    setup(ownersApi({ "kanban/c.md": ["owner/chief", "owner/ops"] }), { botsApi: { list } });
    await screen.findByRole("button", { name: "c" });
    const card = cardOf("kanban/c.md");
    await waitFor(() => expect(list).toHaveBeenCalledTimes(1));
    expect([...card.querySelectorAll(".bot-tag-name")].map((tag) => tag.textContent)).toEqual([
      "Chief",
      "Ops",
    ]);
    expect(within(card).getByText("Owners:")).toBeTruthy();
  });

  it("words Updated and Stopped moving from nowMs and the listing's modified times", async () => {
    const seconds = (ms: number) => ms / 1000;
    const api: Api = {
      entries: async () => ({
        entries: [
          { ref: "kanban/a.md", modified: seconds(at(8, 23)) },
          { ref: "kanban/b.md", modified: seconds(at(5, 12)) },
          { ref: "kanban/c.md", modified: seconds(at(9, 0, 30)) },
          { ref: "kanban/d.md" },
        ],
        partial: false,
        partial_reasons: [],
      }),
      metadata: async (_id, asked) =>
        batchOf(
          asked.map((ref) => item(ref, { status: ref === "kanban/a.md" ? "working" : "todo" })),
        ),
    };
    const { rerender, props } = setup(api, { nowMs: at(9, 12) });
    await screen.findByRole("button", { name: "a" });
    expect(within(cardOf("kanban/a.md")).getByText("Updated yesterday")).toBeTruthy();
    expect(within(cardOf("kanban/a.md")).queryByText("Stopped moving")).toBeNull();
    expect(within(cardOf("kanban/b.md")).getByText("Updated 4 days ago")).toBeTruthy();
    expect(within(cardOf("kanban/b.md")).getByText("Stopped moving")).toBeTruthy();
    expect(within(cardOf("kanban/c.md")).getByText("Updated today")).toBeTruthy();
    expect(within(cardOf("kanban/d.md")).queryByText(/Updated/)).toBeNull();
    rerender(<KanbanView {...props} nowMs={at(10, 12)} />);
    expect(within(cardOf("kanban/a.md")).getByText("Updated 2 days ago")).toBeTruthy();
    expect(within(cardOf("kanban/a.md")).getByText("Stopped moving")).toBeTruthy();
    expect(within(cardOf("kanban/c.md")).getByText("Updated yesterday")).toBeTruthy();
  });

  it("shows what waits for the user only on a complete read", async () => {
    const wait: Api = {
      entries: async () => listingOf(["kanban/r.md", "kanban/b.md"]),
      metadata: async (_id, asked) =>
        batchOf(
          asked.map((ref) => item(ref, { status: ref === "kanban/r.md" ? "review" : "blocked" })),
        ),
    };
    setup(wait);
    expect((await screen.findByText(/Waiting for you/)).textContent).toBe(
      "Waiting for you: 1 ready for review, 1 blocked",
    );
    cleanup();
    setup({
      ...wait,
      entries: async () =>
        listingOf(["kanban/r.md", "kanban/b.md"], { partial: true, partial_reasons: ["deadline"] }),
    });
    await screen.findByRole("status");
    expect(screen.queryByText(/Waiting for you/)).toBeNull();
  });

  it("explains an empty Kanban by the skill check", async () => {
    const empty: Api = { entries: async () => listingOf([]) };
    const onOpenSeat = vi.fn();
    const ready = setup(empty, { readiness: "ready", onOpenSeat });
    fireEvent.click(await screen.findByRole("button", { name: "Open Chief" }));
    expect(onOpenSeat).toHaveBeenCalledWith("chief");
    expect(screen.getByText(t("kanban.empty.ask"))).toBeTruthy();
    ready.unmount();

    const missing = setup(empty, { readiness: "missing", onOpenSeat });
    await screen.findByText(t("kanban.empty"));
    expect(screen.getByText(READINESS_TEXT.missing)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Open Chief" })).toBeNull();
    missing.unmount();

    const unknown = setup(empty, { readiness: "unknown", onOpenSeat });
    await screen.findByText(t("kanban.empty"));
    expect(unknown.container.querySelectorAll(".kanban-empty p")).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Open Chief" })).toBeNull();
  });

  it("keeps the cards inert while Refresh re-reads in place", async () => {
    const listings: Deferred<Record<string, unknown>>[] = [];
    const entries = vi.fn<Api["entries"]>(() => {
      const call = deferred<Record<string, unknown>>();
      listings.push(call);
      return call.promise;
    });
    const list = vi.fn<Bots["list"]>().mockResolvedValue({ bots: [], scheduler });
    const onRefresh = vi.fn();
    const refs = ["kanban/a.md", "kanban/b.md"];
    const api: Api = {
      entries,
      metadata: async (_id, asked) => batchOf(asked.map((ref) => item(ref))),
    };
    const { container } = setup(api, { botsApi: { list }, onRefresh });
    await act(async () => listings[0]?.resolve(listingOf(refs)));
    await screen.findByRole("button", { name: "a" });
    const lanes = container.querySelector(".kanban-lanes") as HTMLElement;
    const refresh = screen.getByRole("button", { name: "Refresh Kanban" });
    refresh.focus();
    fireEvent.click(refresh);
    await waitFor(() => expect(listings).toHaveLength(2));
    expect(lanes.hasAttribute("inert")).toBe(true);
    expect(lanes.getAttribute("aria-busy")).toBe("true");
    expect([...container.querySelectorAll(".kanban-card-ref")].map((n) => n.textContent)).toEqual(
      refs,
    );
    expect(document.activeElement).toBe(refresh);
    expect(onRefresh).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
    await act(async () => listings[1]?.resolve(listingOf(refs)));
    await waitFor(() => expect(lanes.hasAttribute("inert")).toBe(false));
  });

  it("names an unknown status in the Other lane", async () => {
    const { container } = setup({
      entries: async () => listingOf(["kanban/a.md"]),
      metadata: async () => batchOf([item("kanban/a.md", { status: "later" })]),
    });
    await screen.findByRole("button", { name: "a" });
    expect(groupNamed("Other (1)").getByText("Status: later")).toBeTruthy();
    expect(
      container.querySelector('[data-stage="other"] h3 svg[data-icon="statusOther"]'),
    ).toBeTruthy();
  });

  it("has the shared page head", async () => {
    const { container } = setup({ entries: async () => listingOf([]) });
    const head = container.querySelector("header.page-head");
    expect(head?.querySelector("h1.page-title")?.textContent).toBe("Kanban");
    expect(head?.querySelector("p.page-intro")?.textContent).toContain("kanban/ folder");
    expect(head?.querySelector("div.page-actions .btn.alt")?.textContent).toBe("Refresh Kanban");
    expect(container.querySelector("p.bots-note")).toBeNull();
    await screen.findByText(/No cards yet/);
  });

  it("shows no card until every read settles", async () => {
    const refs = refsOf(33);
    const calls: Deferred<VaultMetadataBatch>[] = [];
    const api: Api = {
      entries: async () => listingOf(refs),
      metadata: () => {
        const call = deferred<VaultMetadataBatch>();
        calls.push(call);
        return call.promise;
      },
    };
    setup(api);
    await waitFor(() => expect(calls).toHaveLength(1));
    await act(async () => calls[0]?.resolve(batchOf(refs.slice(0, 32).map((r) => item(r)))));
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(document.querySelectorAll(".kanban-card")).toHaveLength(0);
    expect(screen.getByText("Loading Kanban…")).toBeTruthy();
    await act(async () => calls[1]?.resolve(batchOf([item(refs[32] as string)])));
    expect(await screen.findByRole("heading", { name: "To do (33)" })).toBeTruthy();
    expect(screen.queryByText("Loading Kanban…")).toBeNull();
  });

  it("reads 130 cards in 4 sequential batches", async () => {
    const refs = refsOf(130);
    const calls: { refs: readonly string[]; call: Deferred<VaultMetadataBatch> }[] = [];
    const api: Api = {
      entries: async () => listingOf(refs),
      metadata: (_id, asked) => {
        const call = deferred<VaultMetadataBatch>();
        calls.push({ refs: asked, call });
        return call.promise;
      },
    };
    const { container } = setup(api);
    for (let n = 1; n <= 4; n++) {
      await waitFor(() => expect(calls).toHaveLength(n));
      expect(calls[n - 1]?.refs).toHaveLength(32);
      await act(async () =>
        calls[n - 1]?.call.resolve(batchOf(calls[n - 1]?.refs.map((r) => item(r)) ?? [])),
      );
    }
    expect(await screen.findByRole("heading", { name: "To do (128)" })).toBeTruthy();
    expect(calls).toHaveLength(4);
    expect(screen.getByRole("heading", { name: "Couldn't read (2)" })).toBeTruthy();
    expect(
      container.querySelector('[data-stage="unread"] h3 svg[data-icon="statusUnread"]'),
    ).toBeTruthy();
    expect(
      groupNamed("Couldn't read (2)").getAllByText("Not read: Kanban reads at most 128 cards."),
    ).toHaveLength(2);
  });

  it("stops after a failed batch", async () => {
    const refs = refsOf(70);
    const metadata = vi
      .fn<NonNullable<Api["metadata"]>>()
      .mockResolvedValueOnce(batchOf(refs.slice(0, 32).map((r) => item(r))))
      .mockRejectedValueOnce(new Error("rate_limited"));
    setup({ entries: async () => listingOf(refs), metadata });
    expect(await screen.findByRole("heading", { name: "To do (32)" })).toBeTruthy();
    expect(metadata).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("heading", { name: "Couldn't read (38)" })).toBeTruthy();
    expect(screen.getAllByText("Not read. Refresh Kanban to try again.")).toHaveLength(38);
  });

  it("covers an incomplete listing, an empty Kanban and a failed listing", async () => {
    const partial = listingOf(["kanban/a.md"], {
      partial: true,
      partial_reasons: ["deadline"],
      omitted: 3,
    });
    const first = setup({
      entries: async () => partial,
      metadata: async () => batchOf([item("kanban/a.md")]),
    });
    expect(
      await screen.findByText(
        "The vault listing may be incomplete (deadline; at least 3 omitted), so some cards may be missing.",
      ),
    ).toBeTruthy();
    expect(screen.getAllByText("No cards.").length).toBeGreaterThan(0);
    first.unmount();

    const empty = setup({ entries: async () => listingOf([]) });
    expect((await screen.findByText(t("kanban.empty"))).textContent).toContain("kanban/ folder");
    empty.unmount();

    setup({
      entries: async () => {
        throw new Error("boom");
      },
    });
    expect((await screen.findByRole("alert")).textContent).toBe(
      "Kanban could not be read. Refresh Kanban to try again.",
    );
  });

  it("aborts on a workspace change and re-reads on refresh", async () => {
    const listings: { signal: AbortSignal | undefined; call: Deferred<Record<string, unknown>> }[] =
      [];
    const entries = vi.fn<Api["entries"]>((_id, _q, signal) => {
      const call = deferred<Record<string, unknown>>();
      listings.push({ signal, call });
      return call.promise;
    });
    const api: Api = {
      entries,
      metadata: async (_id, asked) => batchOf(asked.map((r) => item(r))),
    };
    const view = setup(api);
    const rerenderAt = (workspaceId: string) =>
      view.rerender(<KanbanView {...view.props} workspaceId={workspaceId} />);
    await waitFor(() => expect(listings).toHaveLength(1));
    rerenderAt("w2");
    await waitFor(() => expect(listings).toHaveLength(2));
    expect(listings[0]?.signal?.aborted).toBe(true);
    expect(entries.mock.calls.map((call) => call[0])).toEqual(["w1", "w2"]);
    await act(async () => listings[1]?.call.resolve(listingOf(["kanban/two.md"])));
    expect(await screen.findByRole("button", { name: "two" })).toBeTruthy();
    await act(async () => listings[0]?.call.resolve(listingOf(["kanban/one.md"])));
    expect(screen.queryByRole("button", { name: "one" })).toBeNull();
    const refresh = screen.getByRole("button", { name: "Refresh Kanban" });
    fireEvent.click(refresh);
    await waitFor(() => expect(listings).toHaveLength(3));
    expect(listings[1]?.signal?.aborted).toBe(true);
    fireEvent.click(refresh);
    await waitFor(() => expect(listings).toHaveLength(4));
    expect(listings[2]?.signal?.aborted).toBe(true);
    await act(async () => listings[2]?.call.reject(new DOMException("aborted", "AbortError")));
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByText("kanban/two.md")).toBeTruthy();
    expect(view.container.querySelector(".kanban-lanes")?.getAttribute("aria-busy")).toBe("true");
    expect(screen.queryByText("Loading Kanban…")).toBeNull();
  });

  it("starts at loading when the workspace changes after a ready read", async () => {
    const second = deferred<Record<string, unknown>>();
    const entries = vi
      .fn<Api["entries"]>()
      .mockResolvedValueOnce(listingOf(["kanban/one.md"]))
      .mockReturnValueOnce(second.promise);
    const api: Api = {
      entries,
      metadata: async (_id, asked) => batchOf(asked.map((r) => item(r))),
    };
    const view = setup(api);
    expect(await screen.findByText("kanban/one.md")).toBeTruthy();
    view.rerender(<KanbanView {...view.props} workspaceId="w2" />);
    expect(screen.getByText("Loading Kanban…")).toBeTruthy();
    expect(screen.queryByText("kanban/one.md")).toBeNull();
  });
});
