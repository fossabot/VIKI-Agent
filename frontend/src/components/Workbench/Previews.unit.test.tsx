import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import type { WorkspaceJson } from "../../clients/app/shellApi";
import {
  type PreviewJson,
  type PreviewOutputJson,
  type PreviewState,
  type PreviewsApi,
  PreviewsApiError,
} from "../../clients/code/previewsApi";
import type { WorktreeJson } from "../../clients/code/rootsApi";
import { noteWorktree, resetRoots } from "../../state/sessionRoots";
import { POLL_MS } from "./CheckOutput";
import { Previews } from "./Previews";

const W = "00000000-0000-4000-8000-0000000000a1";
const OTHER = "00000000-0000-4000-8000-0000000000a2";
const HEAD = "1".repeat(40);
const LIST_MS = 1000;

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  cleanup();
  resetRoots();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

const folder = (over: Partial<WorkspaceJson> = {}) =>
  ({
    workspace_id: W,
    kind: "code",
    name: "Alpha",
    policy: "ordinary",
    policy_revision: 3,
    root: "x",
    root_available: true,
    ...over,
  }) as WorkspaceJson;

let counter = 0;
const worktree = (over: Partial<WorktreeJson> = {}): WorktreeJson => {
  counter += 1;
  return {
    worktree_id: `00000000-0000-4000-8000-${String(counter).padStart(12, "0")}`,
    workspace_id: W,
    branch: `viki/b${counter}`,
    base_oid: HEAD,
    path: `C:\\area\\wt${counter}`,
    state: "ready",
    created_at: "2026-10-03T10:00:00Z",
    updated_at: "2026-10-03T10:00:00Z",
    ...over,
  };
};

const preview = (state: PreviewState, over: Partial<PreviewJson> = {}): PreviewJson => {
  counter += 1;
  const live = state === "starting" || state === "listening" || state === "port_taken";
  return {
    preview_id: `00000000-0000-4000-8000-${String(500 + counter).padStart(12, "0")}`,
    workspace_id: W,
    worktree_id: null,
    command: "npm run dev",
    port: 5173,
    state,
    pid: 4242,
    listener_pid: state === "listening" ? 5252 : null,
    exit_code: state === "exited" ? 1 : null,
    started_at: "2026-10-04T10:00:00.000Z",
    ended_at: live ? null : "2026-10-04T10:05:00.000Z",
    ...over,
  };
};

const outputOf = (over: Partial<PreviewOutputJson> = {}): PreviewOutputJson => ({
  preview: preview("listening"),
  start: 0,
  next: 0,
  text: "",
  dropped: false,
  done: false,
  ...over,
});

/** A list that answers a new array on every read, as the server does. */
const reads = (...rows: PreviewJson[]) => vi.fn(async () => [...rows]);

const unused = () => Promise.reject(new Error("not stubbed"));
function stub(over: Partial<PreviewsApi> = {}) {
  const api = {
    list: vi.fn().mockResolvedValue([]),
    start: vi.fn(unused),
    stop: vi.fn(unused),
    open: vi.fn(unused),
    remove: vi.fn(unused),
    output: vi.fn().mockResolvedValue(outputOf()),
    ...over,
  };
  return api as typeof api & PreviewsApi;
}

const settle = () => act(async () => {});
const tick = (ms: number) => act(async () => vi.advanceTimersByTimeAsync(ms));
const click = (name: string | RegExp, scope: HTMLElement | Document = document) =>
  fireEvent.click(within(scope as HTMLElement).getByRole("button", { name }));
const type = (label: string, value: string) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });

async function show(api: PreviewsApi, props: { canRun?: boolean; folder?: WorkspaceJson } = {}) {
  const view = render(
    <Previews api={api} folder={props.folder ?? folder()} canRun={props.canRun ?? true} />,
  );
  await settle();
  return view;
}

const rowOf = (text: string) => {
  const found = screen.getByText(text).closest("li");
  if (!found) throw new Error(`no row for ${text}`);
  return found;
};
const buttonsOf = (item: HTMLElement) =>
  within(item)
    .getAllByRole("button")
    .map((b) => b.textContent);

describe("the gates", () => {
  test("off Windows the tab shows words and asks nothing", async () => {
    const api = stub();
    await show(api, { canRun: false });
    expect(screen.getByText("Previews work on Windows in this version.")).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
    expect(api.list).not.toHaveBeenCalled();
  });

  test("an untrusted folder lists its live previews with Stop and Output only", async () => {
    const running = preview("listening", { command: "npm run live" });
    const ended = preview("stopped", { command: "npm run old" });
    const api = stub({ list: vi.fn().mockResolvedValue([running, ended]) });
    await show(api, { folder: folder({ policy: "existing_readonly" }) });
    expect(screen.getByText("Sessions start only in a trusted folder.")).toBeTruthy();
    expect(api.list).toHaveBeenCalledWith(W, expect.anything());
    expect(buttonsOf(rowOf("npm run live"))).toEqual(["Stop", "Output"]);
    expect(screen.queryByText("npm run old")).toBeNull();
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("combobox")).toBeNull();
    expect(screen.queryByRole("button", { name: "Start" })).toBeNull();
  });
});

describe("the panel", () => {
  test("it shows the whole note and No previews. for an empty list", async () => {
    await show(stub());
    expect(
      screen.getByText(
        "A preview runs a command you type, through cmd.exe, in this folder or one of its worktrees, and opens the port you name in a separate browser window that has an empty profile, is not signed in to VIKI and sends its web requests only to that port. The command is not a sandbox: like a check, it can change files and use the network.",
      ),
    ).toBeTruthy();
    expect(screen.getByText("No previews.")).toBeTruthy();
  });

  test("the picker lists this folder and its ready worktrees", async () => {
    noteWorktree(worktree({ branch: "viki/one" }));
    noteWorktree(worktree({ branch: "viki/gone", state: "removing" }));
    noteWorktree(worktree({ branch: "viki/elsewhere", workspace_id: OTHER }));
    await show(stub());
    const picker = screen.getByRole("combobox", { name: "Run in" });
    expect(
      within(picker)
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual(["This folder", "viki/one (worktree)"]);
  });

  test("a failed list shows its refusal", async () => {
    const api = stub({ list: vi.fn().mockRejectedValue(new PreviewsApiError("unknown_worktree")) });
    await show(api);
    expect(screen.getByRole("alert").textContent).toBe("This worktree is not ready.");
  });
});

describe("the start form", () => {
  test("Start waits for a command and a port from 1024 to 65535", async () => {
    await show(stub());
    const start = screen.getByRole("button", { name: "Start" }) as HTMLButtonElement;
    expect(start.disabled).toBe(true);
    type("Command", "npm run dev");
    expect(start.disabled).toBe(true);
    for (const bad of ["", "1023", "65536", "80.5"]) {
      type("Port", bad);
      expect(start.disabled).toBe(true);
    }
    type("Port", "1024");
    expect(start.disabled).toBe(false);
    type("Port", "65535");
    expect(start.disabled).toBe(false);
    type("Command", "   ");
    expect(start.disabled).toBe(true);
  });

  test("Start sends the folder's checkout, the command and the port, then lists again", async () => {
    const one = worktree({ branch: "viki/one" });
    noteWorktree(one);
    const started = preview("starting", { worktree_id: one.worktree_id });
    const list = vi.fn().mockResolvedValueOnce([]).mockResolvedValue([started]);
    const api = stub({ list, start: vi.fn().mockResolvedValue(started) });
    await show(api);
    fireEvent.change(screen.getByRole("combobox", { name: "Run in" }), {
      target: { value: one.worktree_id },
    });
    type("Command", "npm run dev");
    type("Port", "5173");
    click("Start");
    await settle();
    expect(api.start).toHaveBeenCalledWith(W, {
      worktree_id: one.worktree_id,
      command: "npm run dev",
      port: 5173,
    });
    expect(list).toHaveBeenCalledTimes(2);
    expect(screen.getByText("Waiting for port 5173…")).toBeTruthy();
    expect((screen.getByLabelText("Command") as HTMLInputElement).value).toBe("");
  });

  test("Start says Starting… and is disabled while the start is asked", async () => {
    let resolve!: (value: PreviewJson) => void;
    const api = stub({ start: vi.fn(() => new Promise<PreviewJson>((r) => (resolve = r))) });
    await show(api);
    type("Command", "npm run dev");
    type("Port", "5173");
    click("Start");
    await settle();
    expect((screen.getByRole("button", { name: "Starting…" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    await act(async () => resolve(preview("starting")));
    expect(screen.getByRole("button", { name: "Start" })).toBeTruthy();
  });

  test.each([
    ["invalid_port", "Use a port from 1024 to 65535."],
    ["port_reserved", "Port 8765 is VIKI's own. Choose another."],
    ["port_in_use", "Another preview uses this port."],
    ["preview_limit", "Two previews are running. Stop one first."],
    ["invalid_command", "Write the command on one line, up to 8,000 characters."],
  ])("a refused start shows %s's words and keeps the command", async (code, words) => {
    const api = stub({ start: vi.fn().mockRejectedValue(new PreviewsApiError(code)) });
    await show(api);
    type("Command", "npm run dev");
    type("Port", "5173");
    click("Start");
    await settle();
    expect(screen.getByRole("alert").textContent).toBe(words);
    expect((screen.getByLabelText("Command") as HTMLInputElement).value).toBe("npm run dev");
  });
});

describe("a preview's row", () => {
  test("it shows the command in code, the checkout and the state line", async () => {
    const one = worktree({ branch: "viki/one" });
    noteWorktree(one);
    const api = stub({
      list: vi.fn().mockResolvedValue([
        preview("listening", { command: "here" }),
        preview("starting", { command: "there", worktree_id: one.worktree_id }),
        preview("port_taken", {
          command: "taken",
          worktree_id: "00000000-0000-4000-8000-0000000000ff",
        }),
        preview("exited", { command: "done" }),
      ]),
    });
    await show(api);
    expect(screen.getByText("here").tagName).toBe("CODE");
    expect(within(rowOf("here")).getByText("This folder")).toBeTruthy();
    expect(within(rowOf("here")).getByText("Listening on port 5173 (process 5252)")).toBeTruthy();
    expect(within(rowOf("there")).getByText("viki/one (worktree)")).toBeTruthy();
    expect(within(rowOf("there")).getByText("Waiting for port 5173…")).toBeTruthy();
    expect(within(rowOf("taken")).getByText("Unknown worktree")).toBeTruthy();
    expect(within(rowOf("taken")).getByText("Port 5173 is used by another program")).toBeTruthy();
    expect(within(rowOf("done")).getByText("Ended (exit code 1)")).toBeTruthy();
  });

  test("the newest preview comes first", async () => {
    const api = stub({
      list: vi
        .fn()
        .mockResolvedValue([
          preview("stopped", { command: "old", started_at: "2026-10-04T09:00:00.000Z" }),
          preview("stopped", { command: "new", started_at: "2026-10-04T11:00:00.000Z" }),
          preview("stopped", { command: "mid", started_at: "2026-10-04T10:00:00.000Z" }),
        ]),
    });
    await show(api);
    expect(
      screen.getAllByRole("listitem").map((li) => li.querySelector("code")?.textContent),
    ).toEqual(["new", "mid", "old"]);
  });

  test("a command with markup stays text", async () => {
    const command = "<script>alert(1)</script><img src=x onerror=alert(1)>";
    const api = stub({ list: vi.fn().mockResolvedValue([preview("stopped", { command })]) });
    const { container } = await show(api);
    expect(screen.getByText(command).tagName).toBe("CODE");
    expect(container.querySelector("script, img")).toBeNull();
  });

  test("a state decides which buttons it offers", async () => {
    const states: [PreviewState, string[]][] = [
      ["starting", ["Stop", "Output"]],
      ["listening", ["Open preview", "Stop", "Output"]],
      ["port_taken", ["Stop", "Output"]],
      ["stopped", ["Output", "Remove"]],
      ["exited", ["Output", "Remove"]],
    ];
    for (const [state, buttons] of states) {
      const command = `cmd ${state}`;
      const api = stub({ list: vi.fn().mockResolvedValue([preview(state, { command })]) });
      await show(api);
      expect(buttonsOf(rowOf(command))).toEqual(buttons);
      cleanup();
    }
  });
});

describe("Open", () => {
  test("Open is offered only while the port is listening", async () => {
    const api = stub({
      list: vi
        .fn()
        .mockResolvedValue([
          preview("starting", { command: "a" }),
          preview("listening", { command: "b" }),
          preview("port_taken", { command: "c" }),
          preview("stopped", { command: "d" }),
          preview("exited", { command: "e" }),
        ]),
    });
    await show(api);
    const offered = screen
      .getAllByRole("button", { name: "Open preview" })
      .map((b) => b.closest("li")?.querySelector("code")?.textContent);
    expect(offered).toEqual(["b"]);
  });

  test("Open asks the server, says Opening… meanwhile and announces the window", async () => {
    let resolve!: () => void;
    const target = preview("listening");
    const api = stub({
      list: vi.fn().mockResolvedValue([target]),
      open: vi.fn(() => new Promise<void>((r) => (resolve = r))),
    });
    await show(api);
    click("Open preview");
    await settle();
    expect(api.open).toHaveBeenCalledWith(target.preview_id);
    expect((screen.getByRole("button", { name: "Opening…" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    await act(async () => resolve());
    const status = screen.getAllByRole("status").map((s) => s.textContent);
    expect(status).toContain("Opened a preview window.");
    expect(screen.getByRole("button", { name: "Open preview" })).toBeTruthy();
    expect(api.list).toHaveBeenCalledTimes(2);
  });

  test("a refused Open shows its words and no done words", async () => {
    const api = stub({
      list: vi.fn().mockResolvedValue([preview("listening")]),
      open: vi.fn().mockRejectedValue(new PreviewsApiError("window_limit")),
    });
    await show(api);
    click("Open preview");
    await settle();
    expect(screen.getByRole("alert").textContent).toBe(
      "Four preview windows are open. Close one first.",
    );
    expect(screen.queryByText("Opened a preview window.")).toBeNull();
  });
});

describe("Stop and Remove", () => {
  test("Stop calls the server, says Stopping… and lists again", async () => {
    let resolve!: (value: PreviewJson) => void;
    const target = preview("listening");
    const list = vi
      .fn()
      .mockResolvedValueOnce([target])
      .mockResolvedValue([
        { ...target, state: "stopped", listener_pid: null, ended_at: "t".repeat(24) },
      ]);
    const api = stub({ list, stop: vi.fn(() => new Promise<PreviewJson>((r) => (resolve = r))) });
    await show(api);
    click("Stop");
    await settle();
    expect(api.stop).toHaveBeenCalledWith(target.preview_id);
    expect((screen.getByRole("button", { name: "Stopping…" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    await act(async () => resolve(target));
    expect(list).toHaveBeenCalledTimes(2);
    expect(screen.getByText("Stopped")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Stop" })).toBeNull();
  });

  test("a refused Stop shows its words", async () => {
    const api = stub({
      list: vi.fn().mockResolvedValue([preview("starting")]),
      stop: vi.fn().mockRejectedValue(new PreviewsApiError("unknown_preview")),
    });
    await show(api);
    click("Stop");
    await settle();
    expect(screen.getByRole("alert").textContent).toBe(
      "This preview no longer exists. Refresh the list.",
    );
  });

  test("Remove calls the server and lists again", async () => {
    const target = preview("exited");
    const list = vi.fn().mockResolvedValueOnce([target]).mockResolvedValue([]);
    const api = stub({ list, remove: vi.fn().mockResolvedValue(undefined) });
    await show(api);
    click("Remove");
    await settle();
    expect(api.remove).toHaveBeenCalledWith(target.preview_id);
    expect(list).toHaveBeenCalledTimes(2);
    expect(screen.getByText("No previews.")).toBeTruthy();
  });

  test("Remove is disabled while its call runs, so two clicks send one remove", async () => {
    const api = stub({
      list: vi.fn().mockResolvedValue([preview("exited")]),
      remove: vi.fn(() => new Promise<void>(() => {})),
    });
    await show(api);
    click("Remove");
    await settle();
    expect((screen.getByRole("button", { name: "Remove" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    click("Remove");
    await settle();
    expect(api.remove).toHaveBeenCalledTimes(1);
  });

  test("a refused Remove shows its words", async () => {
    const api = stub({
      list: vi.fn().mockResolvedValue([preview("stopped")]),
      remove: vi.fn().mockRejectedValue(new PreviewsApiError("preview_running")),
    });
    await show(api);
    click("Remove");
    await settle();
    expect(screen.getByRole("alert").textContent).toBe("Stop the preview first.");
  });

  test("when the focused button goes away, focus moves to the row's next button", async () => {
    const target = preview("listening");
    const list = vi
      .fn()
      .mockResolvedValueOnce([target])
      .mockResolvedValue([
        { ...target, state: "stopped", listener_pid: null, ended_at: "t".repeat(24) },
      ]);
    const api = stub({ list, stop: vi.fn().mockResolvedValue(target) });
    await show(api);
    const stop = screen.getByRole("button", { name: "Stop" });
    act(() => stop.focus());
    click("Stop");
    await settle();
    expect(document.activeElement?.textContent).toBe("Output");
  });
});

describe("the list while a preview is live", () => {
  test("it is read again every second while one is live", async () => {
    const api = stub({ list: reads(preview("starting")) });
    await show(api);
    expect(api.list).toHaveBeenCalledTimes(1);
    await tick(LIST_MS);
    expect(api.list).toHaveBeenCalledTimes(2);
    await tick(LIST_MS);
    expect(api.list).toHaveBeenCalledTimes(3);
  });

  test("a failed read does not stop the re-listing", async () => {
    const live = preview("listening");
    const list = vi
      .fn()
      .mockResolvedValueOnce([live])
      .mockRejectedValueOnce(new PreviewsApiError("overloaded"))
      .mockResolvedValue([live]);
    await show(stub({ list }));
    await tick(LIST_MS);
    expect(list).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("alert")).toBeTruthy();
    await tick(LIST_MS);
    expect(list).toHaveBeenCalledTimes(3);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  test("it stops once nothing is live", async () => {
    const live = preview("listening");
    const list = vi
      .fn()
      .mockResolvedValueOnce([live])
      .mockResolvedValue([
        { ...live, state: "stopped", listener_pid: null, ended_at: "t".repeat(24) },
      ]);
    const api = stub({ list });
    await show(api);
    await tick(LIST_MS);
    expect(list).toHaveBeenCalledTimes(2);
    await tick(10 * LIST_MS);
    expect(list).toHaveBeenCalledTimes(2);
  });

  test("it does not run for an empty or an ended list", async () => {
    const api = stub({ list: vi.fn().mockResolvedValue([preview("exited")]) });
    await show(api);
    await tick(5 * LIST_MS);
    expect(api.list).toHaveBeenCalledTimes(1);
  });

  test("it pauses while an output view is open and resumes on Back", async () => {
    const api = stub({ list: reads(preview("listening")) });
    await show(api);
    click("Output");
    await settle();
    await tick(5 * LIST_MS);
    expect(api.list).toHaveBeenCalledTimes(1);
    click("Back to the list");
    await settle();
    expect(api.list).toHaveBeenCalledTimes(2);
    await tick(LIST_MS);
    expect(api.list).toHaveBeenCalledTimes(3);
  });

  test("it stops when the panel unmounts", async () => {
    const api = stub({ list: vi.fn().mockResolvedValue([preview("listening")]) });
    const { unmount } = await show(api);
    unmount();
    await tick(5 * LIST_MS);
    expect(api.list).toHaveBeenCalledTimes(1);
  });

  test("a live preview of an untrusted folder is read again too", async () => {
    const api = stub({ list: vi.fn().mockResolvedValue([preview("listening")]) });
    await show(api, { folder: folder({ policy: "existing_readonly" }) });
    await tick(LIST_MS);
    expect(api.list).toHaveBeenCalledTimes(2);
  });
});

describe("the output view", () => {
  test("Output opens a view headed by the port and polls from next until done", async () => {
    const target = preview("listening", { port: 3000 });
    const ended = {
      ...target,
      state: "stopped" as const,
      listener_pid: null,
      ended_at: "t".repeat(24),
    };
    const output = vi
      .fn()
      .mockResolvedValueOnce(outputOf({ preview: target, next: 4, text: "one\n" }))
      .mockResolvedValue(
        outputOf({ preview: ended, start: 4, next: 8, text: "two\n", done: true }),
      );
    const api = stub({ list: vi.fn().mockResolvedValue([target]), output });
    await show(api);
    click("Output");
    await settle();
    expect(screen.getByRole("heading", { name: "Output on port 3000" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Stop" })).toBeNull();
    expect(output).toHaveBeenCalledWith(target.preview_id, 0, expect.anything());
    await tick(POLL_MS);
    expect(output.mock.calls.map((call) => call[1])).toEqual([0, 4]);
    expect(screen.getByText(/one\s+two/).tagName).toBe("PRE");
    expect(screen.getAllByRole("status").map((s) => s.textContent)).toContain("Stopped");
  });

  test("it stops polling at done", async () => {
    const output = vi
      .fn()
      .mockResolvedValue(
        outputOf({ preview: preview("stopped"), next: 3, text: "end", done: true }),
      );
    const api = stub({ list: vi.fn().mockResolvedValue([preview("stopped")]), output });
    await show(api);
    click("Output");
    await settle();
    await tick(10 * POLL_MS);
    expect(output).toHaveBeenCalledTimes(1);
  });

  test("it stops polling on Back and on unmount", async () => {
    const api = stub({ list: vi.fn().mockResolvedValue([preview("listening")]) });
    const { unmount } = await show(api);
    click("Output");
    await settle();
    await tick(POLL_MS);
    expect(api.output).toHaveBeenCalledTimes(2);
    click("Back to the list");
    await settle();
    await tick(5 * POLL_MS);
    expect(api.output).toHaveBeenCalledTimes(2);
    click("Output");
    await settle();
    unmount();
    await tick(5 * POLL_MS);
    expect(api.output).toHaveBeenCalledTimes(3);
  });

  test("it waits, says none, notes dropped output and cleans escapes", async () => {
    const waiting = stub({ list: vi.fn().mockResolvedValue([preview("listening")]) });
    await show(waiting);
    click("Output");
    await settle();
    expect(screen.getAllByRole("status").map((s) => s.textContent)).toContain(
      "Waiting for output…",
    );
    cleanup();

    const none = stub({
      list: vi.fn().mockResolvedValue([preview("stopped")]),
      output: vi.fn().mockResolvedValue(outputOf({ preview: preview("stopped"), done: true })),
    });
    await show(none);
    click("Output");
    await settle();
    expect(screen.getByText("No output.")).toBeTruthy();
    cleanup();

    const dropped = stub({
      list: vi.fn().mockResolvedValue([preview("listening")]),
      output: vi
        .fn()
        .mockResolvedValue(outputOf({ dropped: true, next: 9, text: "\x1b[32mok\x1b[0m\n" })),
    });
    await show(dropped);
    click("Output");
    await settle();
    expect(screen.getByText("Earlier output was dropped.")).toBeTruthy();
    expect(screen.getByText("ok").tagName).toBe("PRE");
  });

  test("a failed read shows its words and stops", async () => {
    const output = vi.fn().mockRejectedValue(new PreviewsApiError("unknown_preview"));
    const api = stub({ list: vi.fn().mockResolvedValue([preview("listening")]), output });
    await show(api);
    click("Output");
    await settle();
    expect(screen.getAllByRole("status").map((s) => s.textContent)).toContain(
      "The output could not be loaded.",
    );
    await tick(5 * POLL_MS);
    expect(output).toHaveBeenCalledTimes(1);
  });

  test("the output is text", async () => {
    const text = "<script>alert(1)</script>";
    const api = stub({
      list: vi.fn().mockResolvedValue([preview("listening")]),
      output: vi.fn().mockResolvedValue(outputOf({ next: text.length, text })),
    });
    const { container } = await show(api);
    click("Output");
    await settle();
    expect(screen.getByText(text).tagName).toBe("PRE");
    expect(container.querySelector("script")).toBeNull();
  });

  test("Back lists again and returns focus to the row's Output button", async () => {
    const api = stub({ list: vi.fn().mockResolvedValue([preview("listening")]) });
    await show(api);
    click("Output");
    await settle();
    expect(document.activeElement?.textContent).toBe("Back to the list");
    click("Back to the list");
    await settle();
    expect(api.list).toHaveBeenCalledTimes(2);
    expect(document.activeElement?.textContent).toBe("Output");
  });
});
