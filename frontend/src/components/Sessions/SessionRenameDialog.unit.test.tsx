import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { SessionMetadata } from "../../clients/conversations/sessionMetadataApi";
import { SessionRenameDialog } from "./SessionRenameDialog";

afterEach(cleanup);

const session: SessionMetadata = {
  session_id: "session-a",
  title: "Original title",
  revision: 3,
  pinned: false,
  archived: false,
};

function mount(over: Partial<Parameters<typeof SessionRenameDialog>[0]> = {}) {
  const props = {
    session,
    pending: false,
    errorCode: null,
    onSave: vi.fn(async () => session),
    onReload: vi.fn(),
    onClose: vi.fn(),
    ...over,
  };
  return { ...render(<SessionRenameDialog {...props} />), props };
}

test("seeds the title, focuses its capped input and saves only the trimmed name", async () => {
  const { props } = mount();
  const input = screen.getByRole("textbox", { name: "Session name" }) as HTMLInputElement;
  expect(input.value).toBe("Original title");
  expect(input.maxLength).toBe(256);
  expect(document.activeElement).toBe(input);
  fireEvent.change(input, { target: { value: "  New title  " } });
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Save" })));
  expect(props.onSave).toHaveBeenCalledExactlyOnceWith("New title");
  expect(props.onClose).toHaveBeenCalledOnce();
});

test.each(["conflict", "update_failed"])(
  "%s keeps entered text and offers Reload",
  async (code) => {
    const { props } = mount({ onSave: vi.fn(async () => null), errorCode: code });
    const input = screen.getByRole("textbox", { name: "Session name" }) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "My unsaved title" } });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Save" })));
    expect(input.value).toBe("My unsaved title");
    expect(props.onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("alert").textContent).toContain(
      code === "conflict" ? "changed elsewhere" : "Could not save",
    );
    fireEvent.click(screen.getByRole("button", { name: "Reload" }));
    expect(props.onReload).toHaveBeenCalledOnce();
  },
);

test("a rejected save is visible and a pending save cannot be sent twice", async () => {
  let reject!: (cause: Error) => void;
  const onSave = vi.fn(
    () =>
      new Promise<SessionMetadata | null>((_, fail) => {
        reject = fail;
      }),
  );
  const { props } = mount({ onSave });
  const button = screen.getByRole("button", { name: "Save" }) as HTMLButtonElement;
  fireEvent.click(button);
  fireEvent.click(button);
  expect(button.disabled).toBe(true);
  expect(onSave).toHaveBeenCalledOnce();
  await act(async () => reject(new Error("synthetic refusal")));
  expect(screen.getByRole("alert").textContent).toContain("Could not save");
  expect(button.disabled).toBe(false);
  expect(props.onClose).not.toHaveBeenCalled();
});

test("an external pending mutation and composing Enter refuse another save", () => {
  const { props } = mount({ pending: true });
  fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter" });
  expect(props.onSave).not.toHaveBeenCalled();
  expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
});

test("Enter supports empty-title fallback while an IME Enter is ignored", async () => {
  const { props } = mount();
  const input = screen.getByRole("textbox");
  fireEvent.change(input, { target: { value: "   " } });
  fireEvent.keyDown(input, { key: "Enter", keyCode: 229, isComposing: true });
  expect(props.onSave).not.toHaveBeenCalled();
  await act(async () => fireEvent.keyDown(input, { key: "Enter" }));
  expect(props.onSave).toHaveBeenCalledExactlyOnceWith("");
});

test("Escape dismisses and unmount restores the connected opener", () => {
  const opener = document.createElement("button");
  document.body.append(opener);
  opener.focus();
  const { props, unmount } = mount();
  fireEvent.keyDown(window, { key: "Escape" });
  expect(props.onClose).toHaveBeenCalledOnce();
  unmount();
  expect(document.activeElement).toBe(opener);
  opener.remove();
});

test("completion after dismissal cannot close a later dialog", async () => {
  let resolve!: (row: SessionMetadata) => void;
  const { props, unmount } = mount({
    onSave: vi.fn(
      () =>
        new Promise<SessionMetadata>((done) => {
          resolve = done;
        }),
    ),
  });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  unmount();
  await act(async () => resolve(session));
  expect(props.onClose).not.toHaveBeenCalled();
});
