import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { readCss, ruleIn } from "../../testing/css";
import { type BotHueControls, BotHuesContext, type BotLookResult } from "../Bots/botHues";
import { BOT_ICON_IDS, type Emblem, type Hue } from "../Bots/botIconDesign";
import { BotIconDialog } from "./BotIconDialog";

afterEach(cleanup);

const LABELS: Record<Emblem, string> = {
  door: "Door",
  loop: "Loop",
  lens: "Lens",
  nib: "Nib",
  wrench: "Wrench",
  tray: "Tray",
  check: "Check",
  bolt: "Bolt",
  star: "Star",
  heart: "Heart",
  leaf: "Leaf",
  bell: "Bell",
  note: "Note",
  bubble: "Bubble",
  key: "Key",
  flag: "Flag",
  flask: "Flask",
  shield: "Shield",
  globe: "Globe",
  pin: "Pin",
  cloud: "Cloud",
  book: "Book",
  chart: "Chart",
  spark: "Spark",
  moon: "Moon",
  gear: "Gear",
  target: "Target",
};

const HUE: Hue = "pink";
const CSS = readCss(import.meta.url, "BotIconDialog.css");

it("bounds the rendered viewport and keeps actions outside the only scroll region", () => {
  const backdrop = ruleIn(CSS, ".bot-icon-dialog-backdrop");
  expect(backdrop).toMatch(/width:\s*calc\(100vw \/ var\(--font-scale, 1\)\)/);
  expect(backdrop).toMatch(/height:\s*calc\(100dvh \/ var\(--font-scale, 1\)\)/);
  expect(backdrop).toMatch(/box-sizing:\s*border-box/);
  expect(ruleIn(CSS, ".bot-icon-dialog")).toMatch(
    /max-height:\s*calc\(100dvh \/ var\(--font-scale, 1\) - 24px\)/,
  );
  expect(ruleIn(CSS, ".bot-icon-dialog-options")).toMatch(/min-height:\s*0/);
  expect(ruleIn(CSS, ".bot-icon-dialog-options")).toMatch(/overflow:\s*auto/);
  expect(ruleIn(CSS, ".bot-icon-dialog-option span")).toMatch(/max-width:\s*100%/);
  expect(ruleIn(CSS, ".bot-icon-dialog-option span")).toMatch(/overflow-wrap:\s*anywhere/);
  expect(ruleIn(CSS, ".bot-icon-dialog-actions")).toMatch(/flex:\s*none/);
});

it("sizes option rows for the radio, icon and full wrapped caption", () => {
  expect(ruleIn(CSS, ".bot-icon-dialog-options")).toMatch(/grid-auto-rows:\s*max-content/);
  expect(ruleIn(CSS, ".bot-icon-dialog-option input")).toMatch(/flex:\s*none/);
  expect(ruleIn(CSS, ".bot-icon-dialog-option .bot-icon")).toMatch(/flex:\s*none/);
  expect(ruleIn(CSS, ".bot-icon-dialog-option span")).toMatch(/flex:\s*none/);
});

function renderDialog(controls: BotHueControls, onSaved = vi.fn(), onClose = vi.fn()) {
  return render(
    <BotHuesContext.Provider value={controls}>
      <button type="button">Open icon dialog</button>
      <BotIconDialog
        profileId="profile-1"
        name="Nova"
        hue={HUE}
        icon={null}
        onClose={onClose}
        onSaved={onSaved}
      />
    </BotHuesContext.Provider>,
  );
}

it("offers Automatic and all 27 named emblems as one radio group", () => {
  renderDialog({ hues: {}, icons: {}, set: vi.fn().mockResolvedValue("saved") });
  expect(screen.getByRole("radiogroup", { name: "Choose an icon" })).toBeTruthy();
  expect(screen.getAllByRole("radio")).toHaveLength(28);
  expect(screen.getByRole("radio", { name: "Automatic" })).toBeTruthy();
  for (const icon of BOT_ICON_IDS) {
    expect(screen.getByRole("radio", { name: LABELS[icon] })).toBeTruthy();
  }
  expect(document.querySelectorAll("form")).toHaveLength(0);
  expect(document.querySelectorAll('[data-hue="pink"]')).toHaveLength(28);
  const dialog = screen.getByRole("dialog");
  const options = screen.getByRole("radiogroup");
  expect(options.contains(dialog.querySelector("h2"))).toBe(false);
  expect(options.contains(dialog.querySelector(".bot-icon-dialog-actions"))).toBe(false);
});

it("moves the radio with arrows and commits the focused choice with Enter", async () => {
  const set = vi.fn().mockResolvedValue("saved");
  const onSaved = vi.fn();
  const onClose = vi.fn();
  renderDialog({ hues: {}, icons: {}, set }, onSaved, onClose);
  const automatic = screen.getByRole<HTMLInputElement>("radio", { name: "Automatic" });
  automatic.focus();
  fireEvent.keyDown(automatic, { key: "ArrowRight" });
  const door = screen.getByRole<HTMLInputElement>("radio", { name: "Door" });
  expect(document.activeElement).toBe(door);
  expect(door.checked).toBe(true);
  expect(set).not.toHaveBeenCalled();
  await act(async () => fireEvent.keyDown(door, { key: "Enter" }));
  expect(set).toHaveBeenCalledWith("profile-1", { icon: "door" });
  expect(onSaved).toHaveBeenCalledWith("door");
  expect(onClose).toHaveBeenCalledOnce();
});

it("Use icon saves selected emblem and restores focus after close", async () => {
  const set = vi.fn().mockResolvedValue("saved");
  const onSaved = vi.fn();
  const onClose = vi.fn();
  function Host() {
    const [open, setOpen] = useState(false);
    return (
      <BotHuesContext.Provider value={{ hues: {}, icons: {}, set }}>
        <main className="app-view" style={{ overflow: "hidden" }}>
          <button type="button" onClick={() => setOpen(true)}>
            Open icon dialog
          </button>
          {open ? (
            <BotIconDialog
              profileId="profile-1"
              name="Nova"
              hue={HUE}
              icon={null}
              onClose={() => {
                onClose();
                setOpen(false);
              }}
              onSaved={onSaved}
            />
          ) : null}
        </main>
      </BotHuesContext.Provider>
    );
  }
  const { container, getByRole } = render(<Host />);
  const opener = getByRole("button", { name: "Open icon dialog" });
  opener.focus();
  fireEvent.click(opener);
  const dialog = screen.getByRole("dialog");
  expect(dialog.parentElement?.parentElement).toBe(document.body);
  expect(dialog.closest(".app-view")).toBeNull();
  expect(container.hasAttribute("inert")).toBe(true);
  expect(document.activeElement).toBe(screen.getByRole("radio", { name: "Automatic" }));
  fireEvent.click(screen.getByRole("radio", { name: "Gear" }));
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Use icon" })));
  expect(set).toHaveBeenCalledWith("profile-1", { icon: "gear" });
  expect(onSaved).toHaveBeenCalledWith("gear");
  expect(onClose).toHaveBeenCalledOnce();
  await waitFor(() => expect(document.activeElement).toBe(opener));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(container.hasAttribute("inert")).toBe(false);
});

it("keeps selected icon and dialog open with an alert when save rejects", async () => {
  const set = vi.fn().mockRejectedValue(new Error("nope"));
  const onSaved = vi.fn();
  const onClose = vi.fn();
  renderDialog({ hues: {}, icons: {}, set }, onSaved, onClose);
  const target = screen.getByRole<HTMLInputElement>("radio", { name: "Gear" });
  fireEvent.click(target);
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Use icon" })));
  expect(screen.getByRole("alert").textContent).toBe("The icon could not be saved. Try again.");
  expect(target.checked).toBe(true);
  expect(screen.getByRole("dialog")).toBeTruthy();
  expect(onSaved).not.toHaveBeenCalled();
  expect(onClose).not.toHaveBeenCalled();
});

it("closes superseded choices quietly and cancel does not save", async () => {
  const set = vi.fn().mockResolvedValueOnce("superseded").mockResolvedValueOnce("saved");
  const onSaved = vi.fn();
  const onClose = vi.fn();
  const view = renderDialog({ hues: {}, icons: {}, set }, onSaved, onClose);
  fireEvent.click(screen.getByRole("radio", { name: "Gear" }));
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Use icon" })));
  expect(onSaved).not.toHaveBeenCalled();
  expect(onClose).toHaveBeenCalledOnce();
  view.unmount();

  const cancelled = renderDialog({ hues: {}, icons: {}, set }, onSaved, onClose);
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(set).toHaveBeenCalledOnce();
  expect(onSaved).not.toHaveBeenCalled();
  expect(onClose).toHaveBeenCalledTimes(2);
  cancelled.unmount();
});

it("Escape closes through useModalFocus", () => {
  const onClose = vi.fn();
  renderDialog({ hues: {}, icons: {}, set: vi.fn().mockResolvedValue("saved") }, vi.fn(), onClose);
  fireEvent.keyDown(window, { key: "Escape" });
  expect(onClose).toHaveBeenCalledOnce();
});

it("keeps a held save visible and locked until its captured icon is confirmed", async () => {
  let resolveSave!: (result: BotLookResult) => void;
  const set = vi.fn(() => new Promise<BotLookResult>((resolve) => (resolveSave = resolve)));
  const onSaved = vi.fn();
  const onClose = vi.fn();
  function Host() {
    const [open, setOpen] = useState(false);
    return (
      <BotHuesContext.Provider value={{ hues: {}, icons: {}, set }}>
        <button type="button" onClick={() => setOpen(true)}>
          Open icon dialog
        </button>
        {open ? (
          <BotIconDialog
            profileId="profile-1"
            name="Nova"
            hue={HUE}
            icon={null}
            onClose={() => {
              onClose();
              setOpen(false);
            }}
            onSaved={onSaved}
          />
        ) : null}
      </BotHuesContext.Provider>
    );
  }
  render(<Host />);
  const opener = screen.getByRole("button", { name: "Open icon dialog" });
  opener.focus();
  fireEvent.click(opener);
  const gear = screen.getByRole<HTMLInputElement>("radio", { name: "Gear" });
  fireEvent.click(gear);
  fireEvent.click(screen.getByRole("button", { name: "Use icon" }));
  const dialog = screen.getByRole("dialog");
  expect(dialog.getAttribute("aria-busy")).toBe("true");
  expect(document.activeElement).toBe(dialog);
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  fireEvent.keyDown(window, { key: "Escape" });
  fireEvent.keyDown(window, { key: "Tab" });
  expect(document.activeElement).toBe(dialog);
  fireEvent.click(screen.getByRole("radio", { name: "Door" }));
  fireEvent.keyDown(gear, { key: "ArrowRight" });
  fireEvent.keyDown(gear, { key: "Enter" });
  fireEvent.click(screen.getByRole("button", { name: "Use icon" }));
  expect(screen.getByRole("dialog")).toBe(dialog);
  expect(gear.checked).toBe(true);
  expect(set).toHaveBeenCalledOnce();
  expect(onSaved).not.toHaveBeenCalled();
  expect(onClose).not.toHaveBeenCalled();
  await act(async () => resolveSave("saved"));
  expect(onSaved).toHaveBeenCalledExactlyOnceWith("gear");
  expect(onClose).toHaveBeenCalledOnce();
  expect(screen.queryByRole("dialog")).toBeNull();
  await waitFor(() => expect(document.activeElement).toBe(opener));
});

it("restores selection, focus and actions after a held save fails", async () => {
  let rejectSave!: (error: Error) => void;
  const set = vi
    .fn()
    .mockImplementationOnce(
      () => new Promise<BotLookResult>((_resolve, reject) => (rejectSave = reject)),
    )
    .mockResolvedValueOnce("saved");
  const onSaved = vi.fn();
  const onClose = vi.fn();
  renderDialog({ hues: {}, icons: {}, set }, onSaved, onClose);
  const gear = screen.getByRole<HTMLInputElement>("radio", { name: "Gear" });
  fireEvent.click(gear);
  fireEvent.click(screen.getByRole("button", { name: "Use icon" }));
  await act(async () => rejectSave(new Error("nope")));
  expect(screen.getByRole("alert").textContent).toBe("The icon could not be saved. Try again.");
  expect(gear.checked).toBe(true);
  expect(document.activeElement).toBe(gear);
  expect(screen.getByRole("dialog").getAttribute("aria-busy")).toBe("false");
  expect(screen.getByRole("button", { name: "Cancel" }).hasAttribute("disabled")).toBe(false);
  fireEvent.click(screen.getByRole("radio", { name: "Door" }));
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Use icon" })));
  expect(set).toHaveBeenNthCalledWith(2, "profile-1", { icon: "door" });
  expect(onSaved).toHaveBeenCalledExactlyOnceWith("door");
  expect(onClose).toHaveBeenCalledOnce();
});
