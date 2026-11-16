import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { ShellApiError } from "../../clients/app/shellApi";
import { settingsFixture } from "../../testShell";
import { ReviewPanel } from "./ReviewPanel";

afterEach(cleanup);

const STALE = new ShellApiError(409, "stale_revision", {
  current: settingsFixture({
    "identity.display_name": "Someone else",
    "appearance.theme": "light",
  }),
  currentRevision: 9,
});

function renderPanel(
  error: ShellApiError,
  changes: Record<string, unknown>,
): { onReload: ReturnType<typeof vi.fn>; onClose: ReturnType<typeof vi.fn> } {
  const onReload = vi.fn();
  const onClose = vi.fn();
  render(<ReviewPanel error={error} changes={changes} onReload={onReload} onClose={onClose} />);
  return { onReload, onClose };
}

test("a stale revision shows what was entered beside what is current, with its source", () => {
  const { onReload, onClose } = renderPanel(STALE, {
    "identity.display_name": "Synth User",
    "appearance.theme": "dark",
  });
  const dialog = screen.getByRole("dialog");
  expect(dialog.getAttribute("aria-modal")).toBe("true");
  expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("Changed in another window");
  expect(dialog.querySelector(".settings-review-reason")?.textContent).toBe(
    "Changed elsewhere; review the current values.",
  );

  const name = dialog.querySelector('[data-setting-key="identity.display_name"]');
  expect(name?.textContent).toContain("Synth User");
  expect(name?.textContent).toContain("Someone else");
  expect(name?.querySelector(".chip")?.textContent).toBe("Set here");

  // An enum's values are rendered through the catalog on both sides, never as the raw token.
  const theme = dialog.querySelector('[data-setting-key="appearance.theme"]');
  expect(theme?.textContent).toContain("Dark");
  expect(theme?.textContent).toContain("Light");

  // The reload takes focus, so Enter answers the panel's only real question.
  const reload = screen.getByRole("button", { name: "Load the current values and re-enter" });
  expect(document.activeElement).toBe(reload);
  fireEvent.click(reload);
  expect(onReload).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  expect(onClose).toHaveBeenCalledOnce();
});

test("nothing here is saved: the panel offers no control that writes", () => {
  renderPanel(STALE, { "identity.display_name": "Synth User" });
  const labels = screen.getAllByRole("button").map((button) => button.textContent);
  expect(labels).toEqual(["Load the current values and re-enter", "Close"]);
  expect(document.querySelectorAll("input, select")).toHaveLength(0);
});

test("a command conflict is the sentence and the reload alone, and Escape still closes", () => {
  const { onClose } = renderPanel(new ShellApiError(409, "command_conflict"), {
    "identity.display_name": "Synth User",
  });
  expect(screen.getAllByRole("button").map((button) => button.textContent)).toEqual([
    "Load the current values and re-enter",
  ]);
  expect(screen.getByRole("dialog").querySelector(".settings-review-reason")?.textContent).toBe(
    "This request was already processed with different content.",
  );
  // No document came with it, so there is nothing to list.
  expect(document.querySelector(".settings-review-rows")).toBeNull();
  fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Escape", isComposing: true });
  expect(onClose).not.toHaveBeenCalled();
  fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Escape" });
  expect(onClose).toHaveBeenCalledOnce();
});

test("a cleared key reads as not set rather than as an empty row", () => {
  renderPanel(STALE, { "identity.assistant_name": null });
  const row = document.querySelector('[data-setting-key="identity.assistant_name"]');
  expect(row?.textContent).toContain("(not set)");
  // The current side still shows the backend's value.
  expect(row?.textContent).toContain("VIKI");
});
