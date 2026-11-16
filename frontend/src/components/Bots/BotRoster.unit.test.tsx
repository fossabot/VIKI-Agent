import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { BotRow } from "../../clients/messaging/botsApi";
import type { SessionRow } from "../../protocol";
import { BotRoster } from "./BotRoster";
import { CHECKING, NOT_STARTED, type TeamLive } from "./botStatus";
import { TEAM, teamRoster } from "./team";
import type { TeamReadiness } from "./teamReadiness";

afterEach(cleanup);

const NOW = new Date(2026, 10, 4, 9, 0).getTime();

function bot(id: string, name: string | null, over: Partial<BotRow> = {}): BotRow {
  return {
    bot_id: id,
    profile_id: `p-${id}`,
    workspace_id: "w1",
    chat_session_id: `s-${id}`,
    profile:
      name === null
        ? null
        : {
            revision: 1,
            archived: false,
            identity: {
              name,
              purpose: `${name} purpose`,
              avatar_asset_id: null,
              accent: "blue",
              shape: "circle",
            },
          },
    workspace: { name: "Home", policy: "ordinary", active: true },
    chat: { profile_revision: 1, started: true, archived: false },
    routine_ids: [],
    ...over,
  } as BotRow;
}

function live(over: Partial<TeamLive> = {}): TeamLive {
  return {
    online: true,
    sessions: {},
    others: [],
    routines: { status: "ready", items: [], scheduler: null, error: null },
    checker: "listed",
    sessionRows: [],
    nowMs: NOW,
    ...over,
  };
}

const BOTS = [bot("c", "Chief"), bot("m", "Mine"), bot("a", "Far", { workspace_id: "w2" })];

function show(
  bots: BotRow[] = BOTS,
  readiness: TeamReadiness | null = "ready",
  extra: Partial<Parameters<typeof BotRoster>[0]> = {},
  l: TeamLive = live(),
) {
  const onSelectBot = vi.fn();
  const onSelectSeat = vi.fn();
  render(
    <BotRoster
      roster={teamRoster(bots, "w1")}
      team={readiness ? { live: l, readiness } : null}
      seatFallback={NOT_STARTED}
      activeWorkspaceId="w1"
      selectedId={null}
      selectedSeat={null}
      onSelectBot={onSelectBot}
      onSelectSeat={onSelectSeat}
      {...extra}
    />,
  );
  return { onSelectBot, onSelectSeat };
}

const row = (name: string) => screen.getByRole("button", { name }).closest("li") as HTMLElement;

describe("BotRoster", () => {
  it("filters names and purposes without changing the current Bot or selecting a row", () => {
    const { onSelectBot, onSelectSeat } = show(BOTS, "ready", { selectedId: "m" });
    const search = screen.getByRole("searchbox", { name: "Find a Bot" });
    fireEvent.change(search, { target: { value: "mine purpose" } });
    expect(screen.getByRole("button", { name: "Mine" }).getAttribute("aria-current")).toBe("true");
    expect(screen.queryByRole("button", { name: "Chief" })).toBeNull();
    fireEvent.change(search, { target: { value: "no match" } });
    expect(screen.getByRole("status").textContent).toBe("No Bots match your search.");
    fireEvent.change(search, { target: { value: "" } });
    expect(screen.getByRole("button", { name: "Mine" }).getAttribute("aria-current")).toBe("true");
    expect(screen.getByRole("button", { name: "Chief" })).toBeTruthy();
    expect(onSelectBot).not.toHaveBeenCalled();
    expect(onSelectSeat).not.toHaveBeenCalled();
  });
  it("lists the groups in order with the exact seat names", () => {
    show();
    expect(screen.getByRole("group", { name: "Bot roster" })).toBeTruthy();
    expect(screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent)).toEqual([
      "Core team",
      "Your Bots",
      "In other vaults",
    ]);
    const core = screen.getByRole("list", { name: "Core team" });
    expect(
      within(core)
        .getAllByRole("button")
        .map((b) => b.textContent),
    ).toEqual(TEAM.map((s) => s.name));
    expect(
      within(screen.getByRole("list", { name: "Your Bots" })).getByRole("button").textContent,
    ).toBe("Mine");
    expect(
      within(screen.getByRole("list", { name: "In other vaults" })).getByRole("button").textContent,
    ).toBe("Far");
  });

  it("shows the roles and a star on Chief only", () => {
    show();
    expect(within(row("Chief")).getByText("Front door").className).toBe("bots-role");
    expect(within(row("Checker")).getByText("Quality gate")).toBeTruthy();
    expect(within(row("Mine")).queryByText(/Front door|Manager|Specialist/)).toBeNull();
    const stars = document.querySelectorAll(".bot-face-star");
    expect(stars).toHaveLength(1);
    expect(row("Chief").contains(stars[0] as Element)).toBe(true);
  });

  it("describes each name button by its status outside the button", () => {
    show();
    const button = screen.getByRole("button", { name: "Mine" });
    const id = button.getAttribute("aria-describedby") as string;
    const status = document.getElementById(id) as HTMLElement;
    expect(status.className).toBe("bots-status");
    expect(status.textContent).toBe("Idle");
    expect(button.contains(status)).toBe(false);
    expect(button.getAttribute("aria-current")).toBeNull();
  });

  it("gives every unfilled seat the fallback", () => {
    show(BOTS, "ready", { seatFallback: CHECKING });
    expect(within(row("Ops")).getByText(CHECKING.text)).toBeTruthy();
    expect(within(row("Checker")).getByText(CHECKING.text)).toBeTruthy();
    expect(within(row("Ops")).getByText(/\S/, { selector: ".bots-purpose" })).toBeTruthy();
  });

  it("names a Bot with a missing profile and still draws a face", () => {
    show([bot("x", null)]);
    const r = row("Unavailable profile");
    expect(within(r).getByRole("img", { name: "Unavailable profile" })).toBeTruthy();
    expect(within(r).getByText("Its profile is unavailable.")).toBeTruthy();
  });

  it("marks the selected Bot or seat", () => {
    show(BOTS, "ready", { selectedId: "m" });
    expect(screen.getByRole("button", { name: "Mine" }).getAttribute("aria-current")).toBe("true");
    expect(row("Mine").className).toContain("bots-row-selected");
    cleanup();
    show(BOTS, "ready", { selectedId: "m", selectedSeat: "ops" });
    expect(screen.getByRole("button", { name: "Ops" }).getAttribute("aria-current")).toBe("true");
    expect(screen.getByRole("button", { name: "Mine" }).getAttribute("aria-current")).toBeNull();
    cleanup();
    show(BOTS, "ready", { selectedId: "c" });
    expect(screen.getByRole("button", { name: "Chief" }).getAttribute("aria-current")).toBe("true");
    expect(row("Chief").className).toContain("bots-row-selected");
  });

  it("shows the chat's last time", () => {
    const rows = [{ session_id: "s-m", modified: new Date(2026, 10, 4, 7, 5).toISOString() }];
    show(BOTS, "ready", {}, live({ sessionRows: rows as SessionRow[] }));
    expect(within(row("Mine")).getByText("07:05").className).toBe("bots-time");
    expect(within(row("Chief")).queryByText(/\d\d:\d\d/)).toBeNull();
  });

  it("omits Core team and every status without a team", () => {
    show(BOTS, null);
    expect(screen.queryByText("Core team")).toBeNull();
    expect(document.querySelector(".bots-status")).toBeNull();
    expect(
      screen.getByRole("button", { name: "Mine" }).getAttribute("aria-describedby"),
    ).toBeNull();
  });

  it("explains a vault that is not ready", () => {
    show(BOTS, "no_vault");
    expect(screen.getByText("Open a vault to start your team.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Chief" })).toBeNull();
    cleanup();
    show(BOTS, "missing");
    expect(screen.getByText(/has no Kanban skill/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Chief" })).toBeTruthy();
  });

  it("offers Add missing Bots under Core team, outside the list, only when given a callback", () => {
    show();
    expect(screen.queryByRole("button", { name: "Add missing Bots" })).toBeNull();
    cleanup();
    const onAddMissing = vi.fn();
    show(BOTS, "ready", { onAddMissing });
    const button = screen.getByRole("button", { name: "Add missing Bots" });
    expect(button.closest(".bots-group-action")).toBeTruthy();
    expect(screen.getByRole("list", { name: "Core team" }).contains(button)).toBe(false);
    expect(button.closest(".bots-group")?.querySelector("h2")?.textContent).toBe("Core team");
    fireEvent.click(button);
    expect(onAddMissing).toHaveBeenCalledTimes(1);
  });

  it("disables Add missing Bots while adding", () => {
    show(BOTS, "ready", { onAddMissing: vi.fn(), adding: true });
    expect(screen.getByRole("button", { name: "Add missing Bots" })).toHaveProperty(
      "disabled",
      true,
    );
  });

  it("calls back for Bots and for unfilled seats and the Checker", () => {
    const { onSelectBot, onSelectSeat } = show();
    fireEvent.click(screen.getByRole("button", { name: "Mine" }));
    expect(onSelectBot).toHaveBeenCalledWith("m");
    fireEvent.click(screen.getByRole("button", { name: "Ops" }));
    fireEvent.click(screen.getByRole("button", { name: "Checker" }));
    expect(onSelectSeat.mock.calls).toEqual([["ops"], ["checker"]]);
    fireEvent.click(screen.getByRole("button", { name: "Chief" }));
    expect(onSelectBot).toHaveBeenLastCalledWith("c");
  });
});
