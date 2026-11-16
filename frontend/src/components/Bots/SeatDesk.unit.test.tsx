import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ProfileSummary } from "../../clients/profiles/profileApi";
import { NOT_STARTED, type TeamLive } from "./botStatus";
import { DeskHead, SeatDesk } from "./SeatDesk";
import { TEAM, type TeamSeat } from "./team";

const live = { online: true, checker: "listed" } as unknown as TeamLive;
const chief = TEAM[0] as TeamSeat;
const checker = TEAM.find((row) => row.seat === "checker") as TeamSeat;

afterEach(cleanup);

function profile(id: string, name: string) {
  return { profile_id: id, identity: { name } } as unknown as ProfileSummary;
}

function desk(over: Partial<Parameters<typeof SeatDesk>[0]> = {}) {
  const onOpen = vi.fn();
  render(
    <SeatDesk
      seat={chief}
      status={NOT_STARTED}
      live={live}
      vaultName="Home"
      policy="ordinary"
      profile={null}
      canOpen
      blockedReason={null}
      busy={false}
      onOpen={onOpen}
      {...over}
    />,
  );
  return onOpen;
}

describe("SeatDesk", () => {
  it("seat profile line", () => {
    desk({ profile: profile(chief.profileId as string, "Chief") });
    expect(screen.getByText("Uses the seat's profile, Chief.")).toBeTruthy();
  });
  it("other profile line", () => {
    desk({ profile: profile("x", "Mine") });
    expect(screen.getByText("Uses your profile Mine.")).toBeTruthy();
  });
  it("null profile line", () => {
    desk();
    expect(
      screen.getByText("Creates a profile named Chief from the seat's job description."),
    ).toBeTruthy();
    expect(screen.getByText("Adds Chief to Home.")).toBeTruthy();
  });
  it("undefined profile line", () => {
    desk({ profile: undefined });
    expect(
      screen.getByText(
        "Uses your profile named Chief if you have one; otherwise creates it from the seat's job description.",
      ),
    ).toBeTruthy();
  });
  it("disables with reason", () => {
    desk({ canOpen: false, blockedReason: "Wait." });
    const button = screen.getByRole("button", { name: "Open conversation" });
    expect((button as HTMLButtonElement).disabled).toBe(true);
    const id = button.getAttribute("aria-describedby") as string;
    expect(document.getElementById(id)?.textContent).toBe("Wait.");
  });
  it("calls onOpen once", () => {
    const onOpen = desk();
    fireEvent.click(screen.getByRole("button", { name: "Open conversation" }));
    expect(onOpen).toHaveBeenCalledTimes(1);
  });
  it("disables when busy", () => {
    desk({ busy: true });
    expect(
      (screen.getByRole("button", { name: "Open conversation" }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });
  it("shows the scheduled-routines note for a non-ordinary policy", () => {
    desk({ policy: "read_only" });
    expect(screen.getByText(/Scheduled routines need a vault/)).toBeTruthy();
  });
  it("hides the scheduled-routines note when ordinary", () => {
    desk();
    expect(screen.queryByText(/Scheduled routines need a vault/)).toBeNull();
  });
  it("shows the job", () => {
    desk();
    expect(screen.getByText("Read the job")).toBeTruthy();
    expect(document.querySelector("pre.bots-job")?.textContent).toBe(chief.starter?.instructions);
  });
  it("checker texts", () => {
    desk({ seat: checker });
    expect(screen.getByText("Quality gate")).toBeTruthy();

    expect(document.querySelector("pre.bots-job")?.textContent).toContain(
      "independent quality gate",
    );
    expect(screen.getByRole("button", { name: "Open conversation" })).toBeTruthy();
  });
});

describe("DeskHead", () => {
  it("hides the face and gives headingRef the h2", () => {
    const ref = vi.fn();
    render(<DeskHead face={<img alt="x" src="a.png" />} name="N" headingRef={ref} />);
    expect(screen.queryByRole("img")).toBeNull();
    expect(ref).toHaveBeenCalledWith(screen.getByRole("heading", { level: 2 }));
  });
});
