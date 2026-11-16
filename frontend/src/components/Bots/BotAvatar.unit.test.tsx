import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import type { ProfileIdentityV1 } from "../../clients/profiles/profileApi";
import type { AvatarUrls } from "../Avatar/avatarUrls";
import { DEFAULT_IDENTITY, IdentityContext } from "../Avatar/identity";
import { BotAvatar } from "./BotAvatar";
import { BotHuesContext } from "./botHues";

afterEach(cleanup);

const identity = (name: string, avatar: string | null = null): ProfileIdentityV1 => ({
  name,
  purpose: "",
  avatar_asset_id: avatar,
  accent: "blue",
  shape: "circle",
});

it("shows a profile without an avatar as its bot icon, in colour", () => {
  render(<BotAvatar profileId="p1" identity={identity("Chief")} name="Chief" size={40} />);
  const avatar = screen.getByRole("img", { name: "Chief" });
  expect(avatar.getAttribute("data-color")).toBe("true");
  expect(avatar.querySelector("svg.bot-icon")?.getAttribute("data-seat")).toBe("chief");
  expect(avatar.querySelector(".avatar-initials")).toBeNull();
});

it("shows an uploaded avatar instead of the bot icon, in colour", () => {
  const urls: AvatarUrls = { acquire: () => () => {}, get: () => "blob:1", dispose: () => {} };
  render(
    <IdentityContext.Provider value={{ ...DEFAULT_IDENTITY, urls }}>
      <BotAvatar profileId="p1" identity={identity("Planner", "a1")} name="Planner" size={40} />
    </IdentityContext.Provider>,
  );
  const avatar = screen.getByRole("img", { name: "Planner" });
  expect(avatar.querySelector("img")?.getAttribute("src")).toBe("blob:1");
  expect(avatar.querySelector("svg.bot-icon")).toBeNull();
  expect(avatar.getAttribute("data-color")).toBe("true");
});

it("draws a profile's stored hue from BotHuesContext, overriding the design hue", () => {
  render(
    <BotHuesContext.Provider value={{ hues: { p1: "pink" }, icons: {} }}>
      <BotAvatar profileId="p1" identity={identity("Chief")} name="Chief" size={40} />
    </BotHuesContext.Provider>,
  );
  const avatar = screen.getByRole("img", { name: "Chief" });
  expect(avatar.querySelector("svg.bot-icon")?.getAttribute("data-hue")).toBe("pink");
});

it("uses the saved icon only for the generated fallback", () => {
  const { rerender } = render(
    <BotHuesContext.Provider value={{ hues: {}, icons: { p1: "door" } }}>
      <BotAvatar profileId="p1" identity={identity("Planner")} name="Planner" size={40} />
    </BotHuesContext.Provider>,
  );
  expect(
    screen
      .getByRole("img", { name: "Planner" })
      .querySelector("svg.bot-icon")
      ?.getAttribute("data-icon"),
  ).toBe("door");

  const urls: AvatarUrls = { acquire: () => () => {}, get: () => "blob:2", dispose: () => {} };
  rerender(
    <IdentityContext.Provider value={{ ...DEFAULT_IDENTITY, urls }}>
      <BotHuesContext.Provider value={{ hues: {}, icons: { p1: "door" } }}>
        <BotAvatar profileId="p1" identity={identity("Planner", "a2")} name="Planner" size={40} />
      </BotHuesContext.Provider>
    </IdentityContext.Provider>,
  );
  const avatar = screen.getByRole("img", { name: "Planner" });
  expect(avatar.querySelector("img")?.getAttribute("src")).toBe("blob:2");
  expect(avatar.querySelector("svg.bot-icon")).toBeNull();
});

it("falls back to the design hue when nothing is stored for the profile", () => {
  render(
    <BotHuesContext.Provider value={{ hues: { other: "amber" }, icons: {} }}>
      <BotAvatar profileId="p1" identity={identity("Chief")} name="Chief" size={40} />
    </BotHuesContext.Provider>,
  );
  const avatar = screen.getByRole("img", { name: "Chief" });
  expect(avatar.querySelector("svg.bot-icon")?.getAttribute("data-hue")).toBe("orange");
});

it("draws one icon for every Bot on a profile", () => {
  const { container } = render(
    <>
      <BotAvatar profileId="p7" identity={identity("Planner")} name="Planner" size={40} />
      <BotAvatar profileId="p7" identity={identity("Planner")} name="Planner" size={32} />
    </>,
  );
  const [first, second] = [...container.querySelectorAll("svg.bot-icon")].map((svg) =>
    [...svg.querySelectorAll("path")].map((path) => path.getAttribute("d")).join(""),
  );
  expect(first).toBeTruthy();
  expect(second).toBe(first);
});
