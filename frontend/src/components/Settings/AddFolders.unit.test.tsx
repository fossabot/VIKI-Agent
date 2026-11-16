import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { ShellApiError } from "../../clients/app/shellApi";
import type { CommandState } from "../../commands";
import { fakeShellApi, workspaceFixture } from "../../testShell";
import { FolderPickerProvider } from "../Onboarding/FolderPicker";
import { AddFolders } from "./AddFolders";

afterEach(cleanup);

const ENABLED: CommandState = { enabled: true, reason: null };

/** Settles the microtasks a picker call and its state updates need (PathForm.unit.test.tsx's
 * own helper, copied here for the same reason that file has it: a chain of plain awaits, not a
 * timer). */
async function flush(): Promise<void> {
  await act(async () => {
    for (let turn = 0; turn < 10; turn += 1) await Promise.resolve();
  });
}

function renderAdd(api: ReturnType<typeof fakeShellApi>, state: CommandState = ENABLED) {
  const onAdded = vi.fn(async () => {});
  const onTrust = vi.fn();
  render(
    <FolderPickerProvider api={api}>
      <AddFolders
        heading="Workspaces"
        api={api}
        state={state}
        onAdded={onAdded}
        onTrust={onTrust}
      />
    </FolderPickerProvider>,
  );
  return { onAdded, onTrust };
}

function codeFolder(overrides: Parameters<typeof workspaceFixture>[0] = {}) {
  return workspaceFixture({
    kind: "code",
    policy: "existing_readonly",
    name: "Project",
    ...overrides,
  });
}

test("each picked folder registers", async () => {
  const ALREADY = "D:\\Code\\already";
  const api = fakeShellApi({
    picker: { available: true, multiple: true, reason: null },
    picked: ["D:\\Code\\refused", "D:\\Code\\added", ALREADY],
    workspace: codeFolder(),
  });
  api.fail("openCodeFolder", new ShellApiError(409, "kind_mismatch"));
  const original = api.openCodeFolder;
  api.openCodeFolder = (id, path) =>
    original(id, path).then((result) =>
      path === ALREADY ? { ...result, created: false } : result,
    );
  const { onAdded } = renderAdd(api);

  fireEvent.click(await screen.findByRole("button", { name: "Add folders…" }));
  await flush();

  const status = screen.getByRole("status");
  expect(within(status).getByText("Added 1 of 3 folders")).toBeTruthy();
  const rows = within(status).getAllByRole("listitem");
  expect(rows).toHaveLength(3);

  const refused = rows.find((row) => row.textContent?.includes("refused"));
  expect(refused?.textContent).toContain(
    "This folder is already registered as the other kind (vault or code).",
  );

  const added = rows.find((row) => row.textContent?.includes("Added · Not trusted"));
  expect(added?.textContent).toContain("Project");
  within(added as HTMLElement).getByRole("button", { name: "Trust…" });

  const already = rows.find((row) => row.textContent?.includes("Already in your workspaces"));
  expect(already?.textContent).toContain("Project");

  const calls = api.calls.filter((call) => call.method === "openCodeFolder");
  expect(calls.map((call) => call.args[1])).toEqual([
    "D:\\Code\\refused",
    "D:\\Code\\added",
    ALREADY,
  ]);
  expect(onAdded).toHaveBeenCalledTimes(1);
});

test("no picker shows the typed field directly, with the unavailable reason", async () => {
  // fakeShellApi()'s own default picker is unavailable with reason "unsupported".
  const api = fakeShellApi();
  renderAdd(api);
  await flush();
  expect(
    screen.getByText("This computer can't show a folder picker, so type the path."),
  ).toBeTruthy();
  expect(screen.getByLabelText("Code folder path")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Add folders…" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Type a path instead" })).toBeNull();
});

test.each([
  [
    409,
    "kind_mismatch",
    {},
    "This folder is already registered as the other kind (vault or code).",
  ],
  [
    403,
    "denied_location",
    { extra: { denied: "install" } },
    "This is VIKI Agent's own folder, so it can't be opened here. To work on VIKI Agent itself, open a Git worktree or a second checkout of it instead.",
  ],
  [
    403,
    "denied_location",
    { extra: { denied: "temp" } },
    "Not allowed here (temporary, system, cloud-synced or app folder).",
  ],
] as const)(
  "a typed refusal (%s) reads as today's field error",
  async (status, code, detail, expected) => {
    const api = fakeShellApi({ picker: { available: true, multiple: true, reason: null } });
    api.fail("openCodeFolder", new ShellApiError(status, code, detail));
    renderAdd(api);
    fireEvent.click(await screen.findByRole("button", { name: "Type a path instead" }));
    fireEvent.change(screen.getByLabelText("Code folder path"), { target: { value: "D:\\x" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await flush();
    expect(screen.getByText(expected)).toBeTruthy();
    // A field-level refusal, not a results row.
    expect(screen.queryByRole("status")).toBeNull();
  },
);

test("a successful typed submit shows one result row and calls onAdded", async () => {
  const api = fakeShellApi({ workspace: codeFolder() });
  const { onAdded } = renderAdd(api);
  await flush();
  fireEvent.change(screen.getByLabelText("Code folder path"), {
    target: { value: "D:\\Code\\one" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Add" }));
  await flush();
  const status = screen.getByRole("status");
  expect(within(status).getAllByRole("listitem")).toHaveLength(1);
  expect(status.textContent).toContain("Project");
  expect(onAdded).toHaveBeenCalledTimes(1);
});

test("a failed pick shows the generic refusal, not a per-row result", async () => {
  const api = fakeShellApi({ picker: { available: true, multiple: true, reason: null } });
  api.fail("pickFolders", new ShellApiError(503, "picker_failed"));
  renderAdd(api);
  fireEvent.click(await screen.findByRole("button", { name: "Add folders…" }));
  await flush();
  expect(screen.getByRole("alert").textContent).toBe(
    "The folder picker couldn't open. Type the path instead.",
  );
  expect(screen.queryByRole("status")).toBeNull();
});

test("Add folders… renders before Type a path instead (primary action first)", async () => {
  const api = fakeShellApi({ picker: { available: true, multiple: true, reason: null } });
  renderAdd(api);
  await screen.findByRole("button", { name: "Add folders…" });
  const order = screen.getAllByRole("button").map((button) => button.textContent);
  expect(order.indexOf("Add folders…")).toBeLessThan(order.indexOf("Type a path instead"));
});

test("Type a path instead toggles the typed field open and closed", async () => {
  const api = fakeShellApi({ picker: { available: true, multiple: true, reason: null } });
  renderAdd(api);
  const toggle = await screen.findByRole("button", { name: "Type a path instead" });
  expect(screen.queryByLabelText("Code folder path")).toBeNull();
  fireEvent.click(toggle);
  expect(screen.getByLabelText("Code folder path")).toBeTruthy();
  fireEvent.click(toggle);
  expect(screen.queryByLabelText("Code folder path")).toBeNull();
});

test("Done closes the results panel", async () => {
  const api = fakeShellApi({
    picker: { available: true, multiple: false, reason: null },
    picked: ["D:\\Code\\one"],
    workspace: codeFolder({ policy: "ordinary" }),
  });
  renderAdd(api);
  fireEvent.click(await screen.findByRole("button", { name: "Add folders…" }));
  await flush();
  expect(screen.getByRole("status")).toBeTruthy();
  // An ordinary (already-trusted) added row shows no Trust… button.
  expect(screen.queryByRole("button", { name: "Trust…" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Done" }));
  expect(screen.queryByRole("status")).toBeNull();
});

test("the head actions carry the disabled reason when the command is refused", async () => {
  const api = fakeShellApi({ picker: { available: true, multiple: true, reason: null } });
  const disabled: CommandState = { enabled: false, reason: "reason.storage_not_ready" };
  renderAdd(api, disabled);
  const addButton = await screen.findByRole("button", { name: "Add folders…" });
  const typeButton = screen.getByRole("button", { name: "Type a path instead" });
  expect(addButton.getAttribute("aria-disabled")).toBe("true");
  expect(typeButton.getAttribute("aria-disabled")).toBe("true");
  fireEvent.click(typeButton);
  expect(screen.queryByLabelText("Code folder path")).toBeNull();
  expect(api.calls.some((call) => call.method === "openCodeFolder")).toBe(false);
});
