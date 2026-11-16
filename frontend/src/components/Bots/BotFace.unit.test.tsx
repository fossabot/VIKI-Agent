import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import type { ProfileIdentityV1 } from "../../clients/profiles/profileApi";
import { BotFace } from "./BotFace";
import type { BotState } from "./botStatus";

afterEach(cleanup);

const identity: ProfileIdentityV1 = {
  name: "Planner",
  purpose: "",
  avatar_asset_id: null,
  accent: "blue",
  shape: "circle",
};
const face = (state?: BotState, extra: { frontDoor?: boolean; decorative?: boolean } = {}) =>
  render(
    <BotFace
      profileId="p1"
      identity={identity}
      name="Planner"
      size={32}
      {...(state ? { state } : {})}
      {...extra}
    />,
  ).container;

it("names the avatar and sizes the face", () => {
  const container = face("idle");
  expect(screen.getByRole("img", { name: "Planner" })).toBeTruthy();
  const root = container.querySelector<HTMLElement>("span.bot-face");
  expect(root?.dataset.state).toBe("idle");
  expect(root?.style.width).toBe("32px");
  expect(root?.style.height).toBe("32px");
});

it("draws a null identity from the name and the profile id", () => {
  render(<BotFace profileId="p1" identity={null} name="Desk" size={32} />);
  expect(screen.getByRole("img", { name: "Desk" }).querySelector("svg.bot-icon")).toBeTruthy();
});

it("hides a decorative face from assistive technology", () => {
  const container = face("idle", { decorative: true });
  expect(container.querySelector(".bot-face")?.getAttribute("aria-hidden")).toBe("true");
  expect(screen.queryByRole("img", { name: "Planner" })).toBeNull();
  cleanup();
  expect(face("idle").querySelector(".bot-face")?.hasAttribute("aria-hidden")).toBe(false);
});

it("shows a hidden dot for the states that want a glance", () => {
  const dotted: BotState[] = ["needs_you", "working", "elsewhere", "unknown"];
  const plain: BotState[] = ["idle", "away", "not_started", "unavailable"];
  for (const state of dotted) {
    const dot = face(state).querySelector(".bot-face-dot");
    expect(dot?.getAttribute("aria-hidden")).toBe("true");
    cleanup();
  }
  for (const state of plain) {
    expect(face(state).querySelector(".bot-face-dot")).toBeNull();
    cleanup();
  }
  expect(face().querySelector(".bot-face-dot")).toBeNull();
});

it("breathes only while working", () => {
  expect(face("working").querySelector(".bot-face-dot")?.classList.contains("breathe")).toBe(true);
  cleanup();
  expect(face("needs_you").querySelector(".bot-face-dot")?.classList.contains("breathe")).toBe(
    false,
  );
});

it("draws the star only on the front door", () => {
  const star = face("idle", { frontDoor: true }).querySelector("svg.bot-face-star");
  expect(star?.getAttribute("aria-hidden")).toBe("true");
  cleanup();
  expect(face("idle").querySelector("svg.bot-face-star")).toBeNull();
});
