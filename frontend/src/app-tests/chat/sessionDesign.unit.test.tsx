import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki, sessionRow } from "../../accept/m3b/fakes";
import {
  type SessionMetadata,
  type SessionMetadataApi,
  SessionMetadataError,
} from "../../clients/conversations/sessionMetadataApi";
import { fakeDurable } from "../../testDurable";

afterEach(() => {
  cleanup();
  sessionStorage.clear();
});

const ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const saved: SessionMetadata = {
  session_id: ID,
  revision: 4,
  title: "Saved title",
  pinned: false,
  archived: false,
};

async function mount(patch?: SessionMetadataApi["patch"]) {
  let current = saved;
  const metadata: SessionMetadataApi = {
    list: vi.fn(async () => [current]),
    patch: vi.fn(
      patch ??
        (async (id, revision, changes) => {
          current = { ...current, ...changes, session_id: id, revision: revision + 1 };
          return current;
        }),
    ),
    remove: vi.fn(),
  };
  const { viki, socket } = fakeViki();
  const rendered = render(
    <App
      viki={viki}
      rest={fakeRest({ sessions: [sessionRow({ session_id: ID, title: "Transcript title" })] })}
      durable={fakeDurable()}
      organization={metadata}
    />,
  );
  await act(async () => socket.open());
  const row = rendered.container.querySelector<HTMLElement>(`[data-session-id="${ID}"]`);
  if (!row) throw new Error("missing listed session");
  return { ...rendered, metadata, viki, row };
}

async function menu(row: HTMLElement) {
  const trigger = within(row).getByRole("button", { name: "Session menu" });
  await act(async () => fireEvent.click(trigger));
  return screen.getByRole("menu");
}

test("unopened saved rows expose organization without live-only actions or opening the chat", async () => {
  const { row, container, viki } = await mount();
  const panel = await menu(row);
  expect(within(panel).getByRole("menuitem", { name: "Rename…" })).toBeTruthy();
  expect(within(panel).getByRole("menuitem", { name: "Copy ID" })).toBeTruthy();
  expect(within(panel).queryByText("Fork")).toBeNull();
  expect(within(panel).queryByText("Close session")).toBeNull();
  expect(container.querySelector(".sess-selected")).toBeNull();
  expect(viki.store.getSnapshot().sessions[ID]).toBeUndefined();
  viki.client.close();
});

test("pin uses the saved revision and existing pinned grouping without selecting or launching", async () => {
  const { row, metadata, container, viki } = await mount();
  const panel = await menu(row);
  await act(async () => fireEvent.click(within(panel).getByRole("menuitem", { name: "Pin" })));
  expect(metadata.patch).toHaveBeenCalledExactlyOnceWith(ID, 4, { pinned: true });
  const group = container.querySelector(`[data-session-id="${ID}"]`)?.closest(".sess-group");
  expect(group?.querySelector(".lbl")?.textContent).toBe("Pinned");
  expect(document.activeElement).toBe(
    container.querySelector(`[data-session-id="${ID}"] .sess-menu`),
  );
  expect(container.querySelector(".sess-selected")).toBeNull();
  expect(viki.store.getSnapshot().sessions[ID]).toBeUndefined();
  viki.client.close();
});

test("rename captures the exact unopened row, preserves text on conflict, and reloads the revision", async () => {
  const patch = vi
    .fn<SessionMetadataApi["patch"]>()
    .mockRejectedValueOnce(new SessionMetadataError(409, "conflict", 5))
    .mockResolvedValueOnce({ ...saved, revision: 6, title: "My name" });
  const { row, metadata, container, viki } = await mount(patch);
  const panel = await menu(row);
  await act(async () => fireEvent.click(within(panel).getByRole("menuitem", { name: "Rename…" })));
  const input = screen.getByRole("textbox", { name: "Session name" }) as HTMLInputElement;
  expect(input.value).toBe("Saved title");
  fireEvent.change(input, { target: { value: " My name " } });
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Save" })));
  expect(patch).toHaveBeenNthCalledWith(1, ID, 4, { title: "My name" });
  expect(screen.getByRole("dialog", { name: "Rename session" })).toBeTruthy();
  expect(input.value).toBe(" My name ");
  expect(screen.getByRole("alert").textContent).toContain("changed elsewhere");
  expect(container.querySelector(".sess-selected")).toBeNull();
  metadata.list = vi.fn(async () => [{ ...saved, revision: 5 }]);
  await act(async () =>
    fireEvent.click(within(screen.getByRole("alert")).getByRole("button", { name: "Reload" })),
  );
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Save" })));
  expect(patch).toHaveBeenNthCalledWith(2, ID, 5, { title: "My name" });
  expect(screen.queryByRole("dialog", { name: "Rename session" })).toBeNull();
  expect(screen.getByText("My name")).toBeTruthy();
  expect(viki.store.getSnapshot().sessions[ID]).toBeUndefined();
  viki.client.close();
});

test("keyboard rename returns focus to the row trigger after its transient menu item disappears", async () => {
  const { row, viki } = await mount();
  const trigger = within(row).getByRole("button", { name: "Session menu" });
  const panel = await menu(row);
  const rename = within(panel).getByRole("menuitem", { name: "Rename…" });
  rename.focus();
  await act(async () => fireEvent.keyDown(rename, { key: "Enter" }));
  expect(document.activeElement).toBe(screen.getByRole("textbox", { name: "Session name" }));
  await act(async () => fireEvent.keyDown(window, { key: "Escape" }));
  expect(screen.queryByRole("dialog", { name: "Rename session" })).toBeNull();
  expect(document.activeElement).toBe(trigger);
  viki.client.close();
});

test("a delayed pin completion preserves focus the user moved to the composer", async () => {
  let resolve!: (row: SessionMetadata) => void;
  const { row, viki } = await mount(
    () =>
      new Promise<SessionMetadata>((done) => {
        resolve = done;
      }),
  );
  await act(async () =>
    fireEvent.click(within(await menu(row)).getByRole("menuitem", { name: "Pin" })),
  );
  const input = screen.getByRole("textbox", { name: "Message" });
  input.focus();
  await act(async () => resolve({ ...saved, pinned: true, revision: 5 }));
  expect(document.activeElement).toBe(input);
  viki.client.close();
});
