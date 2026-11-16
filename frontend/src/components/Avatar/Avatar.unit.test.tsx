import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { Avatar } from "./Avatar";
import type { AvatarUrls } from "./avatarUrls";
import { type Accent, DEFAULT_IDENTITY, IdentityContext, type Shape } from "./identity";

afterEach(cleanup);

function fakeUrls(state: string | null | "failed"): AvatarUrls {
  return { acquire: () => () => {}, get: () => state, dispose: () => {} };
}

test("without a provider, initials render with the name as the accessible name and no img", () => {
  const { container } = render(
    <Avatar name="Aoi" avatarId="id1" accent="orange" shape="circle" size={32} />,
  );
  const el = container.querySelector(".avatar");
  expect(el?.getAttribute("aria-label")).toBe("Aoi");
  expect(el?.querySelector("img")).toBeNull();
  expect(el?.querySelector(".avatar-initials")?.textContent).toBe("A");
});

test("a loaded url renders the img, not the initials", () => {
  const { container } = render(
    <IdentityContext.Provider value={{ ...DEFAULT_IDENTITY, urls: fakeUrls("blob:1") }}>
      <Avatar name="Aoi" avatarId="id1" accent="blue" shape="rounded" size={32} />
    </IdentityContext.Provider>,
  );
  const img = container.querySelector("img");
  expect(img?.getAttribute("src")).toBe("blob:1");
  expect(img?.getAttribute("alt")).toBe("");
  expect(container.querySelector(".avatar-initials")).toBeNull();
});

test("a failed read keeps the initials", () => {
  const { container } = render(
    <IdentityContext.Provider value={{ ...DEFAULT_IDENTITY, urls: fakeUrls("failed") }}>
      <Avatar name="Aoi" avatarId="id1" accent="red" shape="square" size={32} />
    </IdentityContext.Provider>,
  );
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector(".avatar-initials")?.textContent).toBe("A");
});

test("a fallback replaces the initial while no image shows, and an image replaces both", () => {
  const icon = <svg className="probe" />;
  const { container, unmount } = render(
    <Avatar name="Aoi" avatarId={null} accent="blue" shape="circle" size={32} fallback={icon} />,
  );
  expect(container.querySelector(".probe")).not.toBeNull();
  expect(container.querySelector(".avatar-initials")).toBeNull();
  unmount();
  const loaded = render(
    <IdentityContext.Provider value={{ ...DEFAULT_IDENTITY, urls: fakeUrls("blob:1") }}>
      <Avatar name="Aoi" avatarId="id1" accent="blue" shape="circle" size={32} fallback={icon} />
    </IdentityContext.Provider>,
  );
  expect(loaded.container.querySelector("img")?.getAttribute("src")).toBe("blob:1");
  expect(loaded.container.querySelector(".probe")).toBeNull();
});

test("only a color avatar marks itself for colour", () => {
  const { container } = render(
    <>
      <Avatar name="Aoi" avatarId={null} accent="blue" shape="circle" size={32} color />
      <Avatar name="Ren" avatarId={null} accent="blue" shape="circle" size={32} />
    </>,
  );
  const [colour, plain] = container.querySelectorAll(".avatar");
  expect(colour?.getAttribute("data-color")).toBe("true");
  expect(plain?.hasAttribute("data-color")).toBe(false);
});

test("an empty name falls back to a bare question mark and renders as decorative", () => {
  const { container } = render(
    <Avatar name="  " avatarId={null} accent="slate" shape="circle" size={24} />,
  );
  expect(container.querySelector(".avatar-initials")?.textContent).toBe("?");
  const el = container.querySelector(".avatar");
  expect(el?.getAttribute("aria-hidden")).toBe("true");
  expect(el?.getAttribute("role")).toBeNull();
  expect(el?.getAttribute("aria-label")).toBeNull();
});

test("every accent and shape sets its data attribute", () => {
  const accents: Accent[] = ["blue", "green", "orange", "purple", "red", "slate"];
  const shapes: Shape[] = ["circle", "rounded", "square"];
  for (const accent of accents) {
    for (const shape of shapes) {
      const { container, unmount } = render(
        <Avatar name="Aoi" avatarId={null} accent={accent} shape={shape} size={24} />,
      );
      const el = container.querySelector(".avatar");
      expect(el?.getAttribute("data-accent")).toBe(accent);
      expect(el?.getAttribute("data-shape")).toBe(shape);
      unmount();
    }
  }
});
