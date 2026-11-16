/**
 * The Wrap up menu and the vault banner. wrapUp.ts's own frozen test owns every word; this owns
 * the two components' behaviour — above all that the refusal is visible AND explained, because
 * a disabled row with no reason is the one failure mode this surface cannot afford.
 */
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { OtherSession } from "../../protocol";
import { HeaderMenu } from "./HeaderMenu";
import { VaultBanner } from "./VaultBanner";
import { vaultBannerText } from "./wrapUp";

afterEach(cleanup);

const other = (over: Partial<OtherSession> = {}): OtherSession => ({
  session_id: "s9",
  entrypoint: "claude-vscode",
  pid: 20412,
  title: "retro H1-H4",
  ...over,
});

test("with the vault to ourselves the menu offers Wrap up and sending it is one click", () => {
  const onWrapUp = vi.fn();
  const { container } = render(<HeaderMenu others={[]} onWrapUp={onWrapUp} />);
  const toggle = container.querySelector(".header-menu-button") as HTMLButtonElement;
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  fireEvent.click(toggle);
  const item = container.querySelector(".header-menu .mi") as HTMLButtonElement;
  expect(item.textContent).toBe("Wrap up (log + commit)");
  expect(item.disabled).toBe(false);
  expect(item.getAttribute("title")).toBe("");
  fireEvent.click(item);
  expect(onWrapUp).toHaveBeenCalledTimes(1);
  // Picking it closes the menu; it is not a toggle the user has to dismiss.
  expect(container.querySelector(".header-menu")).toBeNull();
});

test("another session in the vault disables it and says which one, rather than just refusing", () => {
  const onWrapUp = vi.fn();
  const { container } = render(
    <HeaderMenu
      others={[other(), other({ session_id: "s8", entrypoint: "cli", title: "" })]}
      onWrapUp={onWrapUp}
    />,
  );
  fireEvent.click(container.querySelector(".header-menu-button") as HTMLElement);
  const item = container.querySelector(".header-menu .mi") as HTMLButtonElement;
  expect(item.disabled).toBe(true);
  expect(item.getAttribute("title")).toContain("VS Code: retro H1-H4");
  expect(item.getAttribute("title")).toContain("CLI: Untitled session");
  fireEvent.click(item);
  expect(onWrapUp).not.toHaveBeenCalled();
});

test("Escape closes the menu, and a composing Escape belongs to the IME", () => {
  const { container } = render(<HeaderMenu others={[]} onWrapUp={vi.fn()} />);
  fireEvent.click(container.querySelector(".header-menu-button") as HTMLElement);
  fireEvent.keyDown(window, { key: "Escape", isComposing: true });
  expect(container.querySelector(".header-menu")).not.toBeNull();
  fireEvent.keyDown(window, { key: "Escape" });
  expect(container.querySelector(".header-menu")).toBeNull();
});

test("the banner is absent with the vault to ourselves, not an empty bar", () => {
  const { container } = render(<VaultBanner others={[]} />);
  expect(container.innerHTML).toBe("");
});

test("the banner says where, which conversation, and that Wrap up waits on it", () => {
  const { container } = render(<VaultBanner others={[other()]} />);
  const banner = container.querySelector(".vault-banner") as HTMLElement;
  // The sentence itself belongs to wrapUp.ts and is pinned by accept/m4/wrapUp.accept.test.ts.
  // Spelling it out again here would make a one-word copy edit a two-file change across two
  // authorities — exactly what this file's own rule about frozen wording forbids.
  expect(banner.textContent).toBe(vaultBannerText([other()]));
  expect(banner.getAttribute("role")).toBe("status");
  expect(banner.getAttribute("aria-live")).toBe("polite");
  expect(banner.querySelector(".vault-banner-dot")).not.toBeNull();
});

test("a title from a transcript is text, whatever it holds", () => {
  const { container } = render(
    <VaultBanner others={[other({ title: '<img src=x onerror="window.__pwned=1">' })]} />,
  );
  expect(container.querySelector("img")).toBeNull();
  expect(container.textContent).toContain('<img src=x onerror="window.__pwned=1">');
});
