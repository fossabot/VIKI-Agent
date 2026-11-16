import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, expect, test, vi } from "vitest";
import type { Command } from "../../protocol";
import { MediaComposer } from "./MediaComposer";
import { SlashMenu } from "./SlashMenu";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
const skill: Command = {
  name: "keep",
  skill: true,
  description: "Keeps a note <img src=x>",
  argument_hint: "<item>",
};
const commands: Command[] = [skill];
function show(query: string) {
  const onPick = vi.fn();
  const onClose = vi.fn();
  return {
    ...render(<SlashMenu commands={commands} query={query} onPick={onPick} onClose={onClose} />),
    onPick,
    onClose,
  };
}

test("empty chrome leaves zero-row Enter unconsumed", () => {
  const { onPick, onClose } = show("missing");
  expect(screen.getByText("No matching commands")).toBeTruthy();
  expect(screen.queryByText("↑↓")).toBeNull();
  const enter = new KeyboardEvent("keydown", { key: "Enter", cancelable: true });
  window.dispatchEvent(enter);
  expect(enter.defaultPrevented).toBe(false);
  expect(onPick).not.toHaveBeenCalled();
  expect(onClose).not.toHaveBeenCalled();
});

test("footer preserves literal provider text, counts and actual pick callbacks", () => {
  const { container, onPick } = show("");
  expect(screen.getByText("↑↓").parentElement?.textContent).toBe("↑↓ Choose");
  expect(screen.getByText("Enter").parentElement?.textContent).toBe("Enter Select");
  expect(screen.getByText("Esc").parentElement?.textContent).toBe("Esc Close");
  expect(screen.getByText("1 commands · 1 vault skills")).toBeTruthy();
  expect(screen.getByText(skill.description)).toBeTruthy();
  expect(screen.getByText("<item>")).toBeTruthy();
  expect(container.querySelector("img")).toBeNull();
  fireEvent.click(screen.getByRole("button"));
  expect(onPick).toHaveBeenCalledExactlyOnceWith(skill);
  fireEvent.keyDown(window, { key: "Enter" });
  expect(onPick).toHaveBeenLastCalledWith(skill);
  expect(onPick).toHaveBeenCalledTimes(2);
});

test.each([{ isComposing: true }, { keyCode: 229 }, { shiftKey: true }])(
  "IME or newline Enter preserves the command and text without a pick",
  (ime) => {
    const { onPick, onClose } = show("");
    const enter = new KeyboardEvent("keydown", { key: "Enter", cancelable: true, ...ime });
    window.dispatchEvent(enter);
    expect(enter.defaultPrevented).toBe(false);
    expect(onPick).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByText(skill.description)).toBeTruthy();
  },
);

function mediaHarness() {
  const onSend = vi.fn(async () => true);
  const keepAttachment = vi.fn();
  const attachment = {
    asset_id: "44444444-4444-4444-8444-444444444444",
    media_type: "image/png" as const,
    byte_size: 2,
    width: 2,
    height: 2,
    name: "image.png",
    source: "picker" as const,
    modified_at: null,
  };
  const api = { upload: vi.fn(), read: vi.fn(() => new Promise<Blob>(() => {})) };
  function Harness() {
    const [value, setValue] = useState("/re");
    return (
      <MediaComposer
        api={api}
        attachments={[attachment]}
        onAttachments={vi.fn()}
        destinationLabel="Synthetic session"
        editorKey="synthetic-session"
        running={false}
        onStop={vi.fn()}
        onSend={onSend}
        value={value}
        onEdit={setValue}
        commands={[
          { name: "retro", description: "", argument_hint: "", skill: false },
          { name: "recap", description: "", argument_hint: "", skill: false },
        ]}
        keepInVault={{
          api: { keepAttachment },
          workspaceId: "synthetic-vault",
          policyRevision: 1,
          rootPhysicalId: "1:2",
        }}
      />
    );
  }
  return { ...render(<Harness />), onSend, keepAttachment };
}

test.each(["Enter", "ArrowDown", "ArrowUp", "Escape"])(
  "%s belongs to KeepInVault Name without changing the draft, menu or focus",
  (key) => {
    const { container, onSend, keepAttachment } = mediaHarness();
    fireEvent.click(screen.getByRole("button", { name: "Save to vault" }));
    const name = screen.getByRole<HTMLInputElement>("textbox", { name: "Name" });
    const message = screen.getByRole<HTMLTextAreaElement>("textbox", { name: "Message" });
    const selected = container.querySelector(".mi.on");
    name.focus();
    fireEvent.change(name, { target: { value: "new image name" } });
    const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
    fireEvent(name, event);
    expect(event.defaultPrevented).toBe(false);
    expect(document.activeElement).toBe(name);
    expect(message.value).toBe("/re");
    expect(name.value).toBe("new image name");
    expect(container.querySelector(".mi.on")).toBe(selected);
    expect(container.querySelector(".menu")).not.toBeNull();
    expect(keepAttachment).not.toHaveBeenCalled();
    expect(onSend).not.toHaveBeenCalled();
  },
);

test("Message and its own menu retain arrow, Enter and focus ownership", () => {
  const { container, onSend } = mediaHarness();
  const message = screen.getByRole<HTMLTextAreaElement>("textbox", { name: "Message" });
  message.focus();
  fireEvent.keyDown(message, { key: "ArrowDown" });
  expect(container.querySelector(".mi.on")?.textContent).toBe("/retro");
  const row = container.querySelector<HTMLButtonElement>(".mi.on");
  if (!row) throw new Error("Missing selected row");
  row.focus();
  fireEvent.keyDown(row, { key: "Enter" });
  expect(message.value).toBe("/retro ");
  expect(document.activeElement).toBe(message);
  expect(container.querySelector(".menu")).toBeNull();
  expect(onSend).not.toHaveBeenCalled();
});

test.each(["body", "null"])("%s focus preserves standalone keyboard control", (focus) => {
  const { onPick } = show("");
  if (focus === "null") vi.spyOn(document, "activeElement", "get").mockReturnValue(null);
  else expect(document.activeElement).toBe(document.body);
  fireEvent.keyDown(window, { key: "Enter" });
  expect(onPick).toHaveBeenCalledExactlyOnceWith(skill);
});
