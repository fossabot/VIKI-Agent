import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { readCss } from "../../testing/css";
import type { AvatarUrls } from "../Avatar/avatarUrls";
import {
  type BoundProfile,
  DEFAULT_IDENTITY,
  type Identity,
  IdentityContext,
} from "../Avatar/identity";
import { Face } from "./Face";

afterEach(cleanup);

// jsdom applies no CSS from stylesheets (only inline styles), so the ring-colour check below
// reads the source directly rather than a computed style.
const css = readCss(import.meta.url, "Face.css");

const CUSTOM_URLS: AvatarUrls = {
  acquire: () => () => {},
  get: () => "blob:custom-avatar",
  dispose: () => {},
};
const CUSTOM_IDENTITY: Identity = {
  ...DEFAULT_IDENTITY,
  assistantAvatarId: "custom-avatar",
  urls: CUSTOM_URLS,
};

test("the presence dot only renders when requested", () => {
  const { container, rerender } = render(
    <IdentityContext.Provider value={CUSTOM_IDENTITY}>
      <Face size={48} />
    </IdentityContext.Provider>,
  );
  expect(container.querySelector(".face-presence")).toBeNull();

  rerender(
    <IdentityContext.Provider value={CUSTOM_IDENTITY}>
      <Face size={48} presence />
    </IdentityContext.Provider>,
  );
  expect(container.querySelector(".face-presence")).not.toBeNull();
});

test("requests the given custom avatar variant", () => {
  const acquire = vi.fn<AvatarUrls["acquire"]>(() => () => {});
  const identity: Identity = {
    ...CUSTOM_IDENTITY,
    urls: { ...CUSTOM_URLS, acquire },
  };
  const { container } = render(
    <IdentityContext.Provider value={identity}>
      <Face size={24} imageSize={64} />
    </IdentityContext.Provider>,
  );
  const img = container.querySelector("img");
  expect(img?.getAttribute("src")).toBe("blob:custom-avatar");
  expect(acquire.mock.calls.map(([id, size]) => [id, size])).toEqual([["custom-avatar", 64]]);
});

test("an unset custom avatar omits the default portrait and presence dot", () => {
  const { container } = render(<Face size={24} imageSize={64} presence />);
  expect(container.childElementCount).toBe(0);
});

test("sizes the presence dot content-box, per the design's per-size offsets", () => {
  const sizes: [number, string, string][] = [
    [24, "8", "-2"],
    [36, "10", "-2"],
    [48, "12", "-1"],
  ];
  for (const [size, dot, offset] of sizes) {
    const { container, unmount } = render(
      <IdentityContext.Provider value={CUSTOM_IDENTITY}>
        <Face size={size} presence />
      </IdentityContext.Provider>,
    );
    const dotEl = container.querySelector<HTMLElement>(".face-presence");
    expect(dotEl?.style.width).toBe(`${dot}px`);
    expect(dotEl?.style.height).toBe(`${dot}px`);
    expect(dotEl?.style.right).toBe(`${offset}px`);
    expect(dotEl?.style.bottom).toBe(`${offset}px`);
    unmount();
  }
});

test("the presence ring takes its colour from the surface, defaulting to --g0", () => {
  const rule = css.match(/\.face-presence\s*\{[^}]*\}/)?.[0];
  expect(rule).toContain("box-sizing: content-box");
  expect(rule).toMatch(/border:\s*2px solid var\(--face-ring,\s*var\(--g0\)\)/);
});

test("a bound Face exposes no role=img, so its name is not announced twice", () => {
  const profile: BoundProfile = {
    name: "Planner",
    avatarId: null,
    accent: "orange",
    shape: "circle",
    revision: 1,
  };
  const identity: Identity = { ...DEFAULT_IDENTITY, profile };
  const { container, queryByRole } = render(
    <IdentityContext.Provider value={identity}>
      <Face size={36} />
    </IdentityContext.Provider>,
  );
  // queryByRole excludes anything inside an aria-hidden subtree, so this checks what a screen
  // reader would actually expose, not the raw role="img" attribute Avatar still carries.
  expect(queryByRole("img")).toBeNull();
  const avatar = container.querySelector(".avatar");
  expect(avatar).not.toBeNull();
  expect(avatar?.closest('[aria-hidden="true"]')).not.toBeNull();
});

test("a profile bound to a Bot draws its generated bot icon, not initials", () => {
  const profile: BoundProfile = {
    name: "Chief",
    avatarId: null,
    accent: "orange",
    shape: "circle",
    revision: 1,
    botProfileId: "p1",
  };
  const identity: Identity = { ...DEFAULT_IDENTITY, profile };
  const { container } = render(
    <IdentityContext.Provider value={identity}>
      <Face size={36} />
    </IdentityContext.Provider>,
  );
  expect(container.querySelector("svg.bot-icon")).not.toBeNull();
  expect(container.querySelector(".avatar-initials")).toBeNull();
});

test("a Bot profile's uploaded avatar still renders in colour", () => {
  const urls: AvatarUrls = { acquire: () => () => {}, get: () => "blob:bot-1", dispose: () => {} };
  const profile: BoundProfile = {
    name: "Chief",
    avatarId: "bot-avatar",
    accent: "orange",
    shape: "circle",
    revision: 1,
    botProfileId: "p1",
  };
  const identity: Identity = { ...DEFAULT_IDENTITY, profile, urls };
  const { container } = render(
    <IdentityContext.Provider value={identity}>
      <Face size={36} />
    </IdentityContext.Provider>,
  );
  const avatar = container.querySelector(".avatar");
  expect(avatar?.getAttribute("data-color")).toBe("true");
  expect(avatar?.querySelector("img")?.getAttribute("src")).toBe("blob:bot-1");
});

test("a profile with no Bot binding still shows its initial, never the bot icon", () => {
  const profile: BoundProfile = {
    name: "Planner",
    avatarId: null,
    accent: "orange",
    shape: "circle",
    revision: 1,
  };
  const identity: Identity = { ...DEFAULT_IDENTITY, profile };
  const { container } = render(
    <IdentityContext.Provider value={identity}>
      <Face size={36} />
    </IdentityContext.Provider>,
  );
  expect(container.querySelector(".avatar-initials")?.textContent).toBe("P");
  expect(container.querySelector("svg.bot-icon")).toBeNull();
});

test("a bound Face fetches its profile's avatar variant and never the assistant's", () => {
  const acquire = vi.fn<AvatarUrls["acquire"]>(() => () => {});
  const urls: AvatarUrls = { acquire, get: () => null, dispose: () => {} };
  const profile: BoundProfile = {
    name: "Planner",
    avatarId: "profile-avatar",
    accent: "orange",
    shape: "circle",
    revision: 1,
  };
  const identity: Identity = {
    ...DEFAULT_IDENTITY,
    profile,
    urls,
    assistantAvatarId: "app-avatar",
  };
  render(
    <IdentityContext.Provider value={identity}>
      <Face size={36} />
    </IdentityContext.Provider>,
  );
  expect(acquire.mock.calls.map(([id, size]) => [id, size])).toEqual([["profile-avatar", 64]]);
});
