import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import type { WorkspaceJson } from "../../clients/app/shellApi";
import {
  type CheckOutputJson,
  type CheckRunJson,
  type ChecksApi,
  ChecksApiError,
  type CodeCheckJson,
} from "../../clients/code/checksApi";
import { runLine } from "../../clients/code/checkWords";
import type { WorktreeJson } from "../../clients/code/rootsApi";
import { noteWorktree, resetRoots } from "../../state/sessionRoots";
import { Checks } from "./Checks";

const W = "00000000-0000-4000-8000-0000000000a1";
const OTHER = "00000000-0000-4000-8000-0000000000a2";
const HEAD = "1".repeat(40);

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

const run = (over: Partial<CheckRunJson> = {}): CheckRunJson => ({
  run_id: `00000000-0000-4000-8000-${String(900 + counter++).padStart(12, "0")}`,
  check_id: "00000000-0000-4000-8000-0000000000c1",
  workspace_id: W,
  worktree_id: null,
  head_oid: HEAD,
  dirty: false,
  outcome: "passed",
  exit_code: 0,
  duration_ms: 4000,
  output_bytes: 10,
  started_at: "2026-10-04T10:00:00.000Z",
  ended_at: "2026-10-04T10:00:04.000Z",
  ...over,
});
const live = (over: Partial<CheckRunJson> = {}) =>
  run({ outcome: "running", exit_code: null, duration_ms: null, ended_at: null, ...over });

const check = (over: Partial<CodeCheckJson> = {}): CodeCheckJson => ({
  check_id: "00000000-0000-4000-8000-0000000000c1",
  name: "build",
  command: "npm run build",
  timeout_s: 600,
  created_at: "2026-10-04T09:00:00.000Z",
  readable: true,
  last_run: null,
  ...over,
});
const id = (n: number) => `00000000-0000-4000-8000-0000000000c${n}`;

const outputOf = (over: Partial<CheckOutputJson> = {}): CheckOutputJson => ({
  run: live(),
  start: 0,
  next: 0,
  text: "",
  dropped: false,
  done: false,
  ...over,
});

const unused = () => Promise.reject(new Error("not stubbed"));
function stub(over: Partial<ChecksApi> = {}) {
  const api = {
    list: vi.fn().mockResolvedValue([]),
    add: vi.fn(unused),
    remove: vi.fn(unused),
    run: vi.fn(unused),
    runs: vi.fn(unused),
    cancel: vi.fn(unused),
    output: vi.fn().mockResolvedValue(outputOf()),
    ...over,
  };
  return api as typeof api & ChecksApi;
}

const settle = () => act(async () => {});
const tick = (ms: number) => act(async () => vi.advanceTimersByTimeAsync(ms));
const click = (name: string | RegExp, scope: HTMLElement | Document = document) =>
  fireEvent.click(within(scope as HTMLElement).getByRole("button", { name }));

async function show(api: ChecksApi, props: { canRun?: boolean; folder?: WorkspaceJson } = {}) {
  const view = render(
    <Checks api={api} folder={props.folder ?? folder()} canRun={props.canRun ?? true} />,
  );
  await settle();
  return view;
}

const row = (name: string) => {
  const found = screen.getByText(name).closest("li");
  if (!found) throw new Error(`no row for ${name}`);
  return found;
};

describe("the gates", () => {
  test("an unsupported platform shows its refusal without fetching", async () => {
    const api = stub();
    await show(api, { canRun: false });
    expect(screen.getByText("Checks need Windows, macOS or Linux.")).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
    expect(api.list).not.toHaveBeenCalled();
  });

  test("an untrusted folder shows the trust words and asks nothing", async () => {
    const api = stub();
    await show(api, { folder: folder({ policy: "existing_readonly" }) });
    expect(screen.getByText("Sessions start only in a trusted folder.")).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
    expect(api.list).not.toHaveBeenCalled();
  });

  test("a check that never ran shows its limit, its state and its actions", async () => {
    const api = stub({ list: vi.fn().mockResolvedValue([check({ last_run: null })]) });
    await show(api);
    expect(screen.getByText("Time limit: 10 min")).toBeTruthy();
    expect(screen.getByText("Not run here yet.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Run" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Earlier runs" })).toBeTruthy();
  });
});

describe("the checkout", () => {
  test("the picker lists this folder and its ready worktrees, and lists per checkout", async () => {
    const one = worktree({ branch: "viki/one" });
    noteWorktree(one);
    noteWorktree(worktree({ branch: "viki/gone", state: "removing" }));
    noteWorktree(worktree({ branch: "viki/elsewhere", workspace_id: OTHER }));
    const api = stub();
    await show(api);
    expect(api.list).toHaveBeenLastCalledWith(W, null, expect.anything());
    const picker = screen.getByRole("combobox", { name: "Run in" });
    expect(
      within(picker)
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual(["This folder", "viki/one (worktree)"]);
    fireEvent.change(picker, { target: { value: one.worktree_id } });
    await settle();
    expect(api.list).toHaveBeenLastCalledWith(W, one.worktree_id, expect.anything());
  });

  test("the note and an empty list are shown", async () => {
    await show(stub());
    expect(screen.getByText(/^A check runs a command you write in this folder/)).toBeTruthy();
    expect(screen.getByText(/cmd\.exe on Windows or \/bin\/sh on macOS and Linux/)).toBeTruthy();
    expect(screen.getByText("No checks yet.")).toBeTruthy();
  });

  test("a failed list shows its refusal", async () => {
    const api = stub({ list: vi.fn().mockRejectedValue(new ChecksApiError("worktree_not_ready")) });
    await show(api);
    expect(screen.getByRole("alert").textContent).toBe("This worktree is not ready.");
  });
});

describe("a check's row", () => {
  test("it shows the name, the command in code, the limit and the last run", async () => {
    const last = run();
    const api = stub({ list: vi.fn().mockResolvedValue([check({ last_run: last })]) });
    await show(api);
    expect(screen.getByText("build").tagName).toBe("STRONG");
    expect(screen.getByText("npm run build").tagName).toBe("CODE");
    expect(screen.getByText("Time limit: 10 min")).toBeTruthy();
    expect(screen.getByText(runLine(last))).toBeTruthy();
  });

  test("a check with no run here says so", async () => {
    await show(stub({ list: vi.fn().mockResolvedValue([check()]) }));
    expect(screen.getByText("Not run here yet.")).toBeTruthy();
  });

  test("a name or command with markup stays text", async () => {
    const name = "<script>alert(1)</script>";
    const command = "<img src=x onerror=alert(1)>";
    const api = stub({ list: vi.fn().mockResolvedValue([check({ name, command })]) });
    const { container } = await show(api);
    expect(screen.getByText(name)).toBeTruthy();
    expect(screen.getByText(command).tagName).toBe("CODE");
    expect(container.querySelector("script, img")).toBeNull();
  });

  test("an unreadable check shows its words and offers Delete only", async () => {
    const api = stub({
      list: vi.fn().mockResolvedValue([check({ readable: false, name: null, command: null })]),
    });
    await show(api);
    expect(
      screen.getByText("This check can't be read on this computer. Delete it and add it again."),
    ).toBeTruthy();
    expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual(["Delete", "Add"]);
  });
});

describe("run and output", () => {
  test("Run opens the run's output", async () => {
    const started = live();
    const api = stub({
      list: vi.fn().mockResolvedValue([check()]),
      run: vi.fn().mockResolvedValue(started),
    });
    await show(api);
    click("Run");
    await settle();
    expect(api.run).toHaveBeenCalledWith(check().check_id, null);
    expect(screen.getByRole("heading", { name: "Output of build" })).toBeTruthy();
    expect(api.output).toHaveBeenCalledWith(started.run_id, 0, expect.anything());
    expect(screen.queryByRole("button", { name: "Run" })).toBeNull();
  });

  test("the output keeps polling and its end is announced and refreshes the list", async () => {
    const started = live();
    const done = run({ run_id: started.run_id, outcome: "failed", exit_code: 3 });
    const list = vi
      .fn()
      .mockResolvedValueOnce([check()])
      .mockResolvedValue([check({ last_run: done })]);
    const output = vi
      .fn()
      .mockResolvedValueOnce(outputOf({ run: started, next: 4, text: "one\n" }))
      .mockResolvedValue(outputOf({ run: done, start: 4, next: 8, text: "two\n", done: true }));
    const api = stub({ list, output, run: vi.fn().mockResolvedValue(started) });
    await show(api);
    click("Run");
    await settle();
    await tick(300);
    expect(output.mock.calls.map((call) => call[1])).toEqual([0, 4]);
    expect(screen.getAllByRole("status").map((s) => s.textContent)).toContain(
      "build — Failed (exit code 3)",
    );
    expect(list.mock.calls.length).toBeGreaterThan(1);
    click("Back to the list");
    await settle();
    expect(screen.getByText(runLine(done))).toBeTruthy();
    expect(document.activeElement?.textContent).toBe("Run");
  });

  test("a refused start shows its words and stays on the list", async () => {
    const api = stub({
      list: vi.fn().mockResolvedValue([check()]),
      run: vi.fn().mockRejectedValue(new ChecksApiError("run_limit")),
    });
    await show(api);
    click("Run");
    await settle();
    expect(screen.getByRole("alert").textContent).toBe(
      "Four checks are running. Wait until one ends.",
    );
    expect(screen.getByRole("button", { name: "Run" })).toBeTruthy();
  });

  test("Run says Starting… and is disabled while the start is asked", async () => {
    let resolve!: (value: CheckRunJson) => void;
    const api = stub({
      list: vi.fn().mockResolvedValue([check()]),
      run: vi.fn(() => new Promise<CheckRunJson>((r) => (resolve = r))),
    });
    await show(api);
    click("Run");
    await settle();
    const button = screen.getByRole("button", { name: "Starting…" }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    await act(async () => resolve(live()));
    expect(screen.getByRole("heading", { name: "Output of build" })).toBeTruthy();
  });

  test("Output shows the last run's output", async () => {
    const last = run();
    const api = stub({ list: vi.fn().mockResolvedValue([check({ last_run: last })]) });
    await show(api);
    click("Output");
    await settle();
    expect(api.output).toHaveBeenCalledWith(last.run_id, 0, expect.anything());
  });
});

describe("a live run", () => {
  test("Run is disabled and Cancel calls api.cancel", async () => {
    const last = live();
    const api = stub({
      list: vi.fn().mockResolvedValue([check({ last_run: last })]),
      cancel: vi.fn().mockResolvedValue(last),
    });
    await show(api);
    expect((screen.getByRole("button", { name: "Run" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Delete" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    click("Cancel");
    await settle();
    expect(api.cancel).toHaveBeenCalledWith(last.run_id);
    expect(api.list).toHaveBeenCalledTimes(2);
  });

  test("a refused Cancel shows its words", async () => {
    const api = stub({
      list: vi.fn().mockResolvedValue([check({ last_run: live() })]),
      cancel: vi.fn().mockRejectedValue(new ChecksApiError("unknown_run")),
    });
    await show(api);
    click("Cancel");
    await settle();
    expect(screen.getByRole("alert").textContent).toBe(
      "This check no longer exists. Refresh the list.",
    );
  });

  test("the list is read again while a run is live, and the polling stops after it", async () => {
    const list = vi
      .fn()
      .mockResolvedValueOnce([check({ last_run: live() })])
      .mockResolvedValue([check({ last_run: run() })]);
    await show(stub({ list }));
    expect(list).toHaveBeenCalledTimes(1);
    await tick(1000);
    expect(list).toHaveBeenCalledTimes(2);
    await tick(5000);
    expect(list).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
  });

  test("a failed read does not stop the re-listing", async () => {
    const list = vi
      .fn()
      .mockResolvedValueOnce([check({ last_run: live() })])
      .mockRejectedValueOnce(new ChecksApiError("overloaded"))
      .mockResolvedValue([check({ last_run: live() })]);
    await show(stub({ list }));
    await tick(1000);
    expect(list).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("alert")).toBeTruthy();
    await tick(1000);
    expect(list).toHaveBeenCalledTimes(3);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  test("with four runs live no other Run starts", async () => {
    const live4 = [1, 2, 3, 4].map((n) =>
      check({ check_id: id(n), name: `live ${n}`, last_run: live({ check_id: id(n) }) }),
    );
    const api = stub({
      list: vi.fn().mockResolvedValue([...live4, check({ check_id: id(5), name: "idle" })]),
    });
    await show(api);
    expect(
      (within(row("idle")).getByRole("button", { name: "Run" }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });
});

describe("earlier runs", () => {
  test("they list the checkout, the run line and Show output", async () => {
    const one = worktree({ branch: "viki/one" });
    noteWorktree(one);
    const here = run({ worktree_id: null });
    const there = run({ worktree_id: one.worktree_id, outcome: "failed", exit_code: 2 });
    const stray = run({ worktree_id: "00000000-0000-4000-8000-0000000000ff" });
    const api = stub({
      list: vi.fn().mockResolvedValue([check({ last_run: here })]),
      runs: vi.fn().mockResolvedValue([here, there, stray]),
    });
    await show(api);
    click("Earlier runs");
    await settle();
    expect(api.runs).toHaveBeenCalledWith(check().check_id, expect.anything());
    expect(screen.getByRole("heading", { name: "Earlier runs of build" })).toBeTruthy();
    const items = screen.getAllByRole("listitem");
    expect(items.map((item) => item.textContent)).toEqual([
      `This folder${runLine(here)}Show output`,
      `viki/one (worktree)${runLine(there)}Show output`,
      `Unknown worktree${runLine(stray)}Show output`,
    ]);
    click("Show output", items[1]);
    await settle();
    expect(api.output).toHaveBeenCalledWith(there.run_id, 0, expect.anything());
    click("Back to the list");
    await settle();
    expect(screen.getByRole("button", { name: "Earlier runs" })).toBeTruthy();
  });

  test("a failed read shows the failure words", async () => {
    const api = stub({
      list: vi.fn().mockResolvedValue([check()]),
      runs: vi.fn().mockRejectedValue(new ChecksApiError("unknown_check")),
    });
    await show(api);
    click("Earlier runs");
    await settle();
    expect(screen.getByRole("alert")).toBeTruthy();
  });
});

describe("delete", () => {
  test("it asks first, Keep changes nothing, and the confirm deletes and lists again", async () => {
    const list = vi.fn().mockResolvedValueOnce([check()]).mockResolvedValue([]);
    const api = stub({ list, remove: vi.fn().mockResolvedValue(undefined) });
    await show(api);
    click("Delete");
    expect(api.remove).not.toHaveBeenCalled();
    expect(screen.getByText("Delete the check build and its run records?")).toBeTruthy();
    expect(document.activeElement?.textContent).toBe("Keep");
    click("Keep");
    expect(api.remove).not.toHaveBeenCalled();
    expect(document.activeElement?.textContent).toBe("Delete");
    click("Delete");
    click(/^Delete$/, screen.getByRole("group"));
    await settle();
    expect(api.remove).toHaveBeenCalledWith(check().check_id);
    expect(screen.getByText("No checks yet.")).toBeTruthy();
  });

  test("a refused delete shows its words", async () => {
    const api = stub({
      list: vi.fn().mockResolvedValue([check()]),
      remove: vi.fn().mockRejectedValue(new ChecksApiError("check_running")),
    });
    await show(api);
    click("Delete");
    click(/^Delete$/, screen.getByRole("group"));
    await settle();
    expect(screen.getByRole("alert").textContent).toBe(
      "This check is running. Cancel it or wait until it ends.",
    );
  });
});

describe("focus after the list changes", () => {
  const two = [
    check({ check_id: id(1), name: "build" }),
    check({ check_id: id(2), name: "lint", command: "npm run lint" }),
  ];
  const confirmDelete = (name: string) => {
    click("Delete", row(name));
    click(/^Delete$/, screen.getByRole("group"));
  };
  const button = (name: string, label: string) =>
    within(row(name)).getByRole("button", { name: label });

  test("a deleted check's neighbour takes focus", async () => {
    const list = vi.fn().mockResolvedValueOnce(two).mockResolvedValue([two[1]]);
    await show(stub({ list, remove: vi.fn().mockResolvedValue(undefined) }));
    confirmDelete("build");
    await settle();
    expect(screen.queryByText("build")).toBeNull();
    expect(document.activeElement).toBe(button("lint", "Run"));
  });

  test("deleting the last check of the list moves focus to the Add a check heading", async () => {
    const list = vi.fn().mockResolvedValueOnce([two[0]]).mockResolvedValue([]);
    await show(stub({ list, remove: vi.fn().mockResolvedValue(undefined) }));
    confirmDelete("build");
    await settle();
    expect(document.activeElement).toBe(screen.getByRole("heading", { name: "Add a check" }));
  });

  test("deleting the last row of several moves focus to the new last row", async () => {
    const list = vi.fn().mockResolvedValueOnce(two).mockResolvedValue([two[0]]);
    await show(stub({ list, remove: vi.fn().mockResolvedValue(undefined) }));
    confirmDelete("lint");
    await settle();
    expect(document.activeElement).toBe(button("build", "Run"));
  });

  test("a refused delete keeps focus on that row's Delete button", async () => {
    const api = stub({
      list: vi.fn().mockResolvedValue(two),
      remove: vi.fn().mockRejectedValue(new ChecksApiError("check_running")),
    });
    await show(api);
    confirmDelete("lint");
    await settle();
    expect(screen.getByRole("alert")).toBeTruthy();
    expect(document.activeElement).toBe(button("lint", "Delete"));
  });

  test("Back while the list shows the run live focuses Cancel, not the disabled Run", async () => {
    const started = live();
    const list = vi
      .fn()
      .mockResolvedValueOnce([check()])
      .mockResolvedValue([check({ last_run: started })]);
    const output = vi.fn().mockResolvedValue(outputOf({ run: started, done: true }));
    await show(stub({ list, output, run: vi.fn().mockResolvedValue(started) }));
    click("Run");
    await settle();
    click("Back to the list");
    expect((screen.getByRole("button", { name: "Run" }) as HTMLButtonElement).disabled).toBe(true);
    expect(document.activeElement?.textContent).toBe("Cancel");
    await settle();
    expect(document.activeElement?.textContent).toBe("Cancel");
  });

  test("Back before the list shows the run live ends on Cancel when the list arrives", async () => {
    const started = live();
    const list = vi
      .fn()
      .mockResolvedValueOnce([check()])
      .mockResolvedValue([check({ last_run: started })]);
    await show(stub({ list, run: vi.fn().mockResolvedValue(started) }));
    click("Run");
    await settle();
    click("Back to the list");
    await settle();
    expect(document.activeElement?.textContent).toBe("Cancel");
  });

  test("focus on Cancel moves to Run when the run settles and Cancel goes away", async () => {
    const list = vi
      .fn()
      .mockResolvedValueOnce([check({ last_run: live() })])
      .mockResolvedValue([check({ last_run: run() })]);
    await show(stub({ list }));
    act(() => screen.getByRole("button", { name: "Cancel" }).focus());
    expect(document.activeElement?.textContent).toBe("Cancel");
    await tick(1000);
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Run" }));
  });

  test("a list change does not take focus the user moved away", async () => {
    // A fresh array per read, as the server's answer is: an equal one would not re-render.
    const list = vi.fn(async () => [
      check({ check_id: id(1), name: "idle" }),
      check({ check_id: id(2), name: "busy", last_run: live({ check_id: id(2) }) }),
    ]);
    await show(stub({ list }));
    act(() => within(row("idle")).getByRole("button", { name: "Earlier runs" }).focus());
    act(() => (document.activeElement as HTMLElement).blur());
    expect(document.activeElement).toBe(document.body);
    await tick(1000);
    expect(list).toHaveBeenCalledTimes(2);
    expect(document.activeElement).toBe(document.body);
    const input = screen.getByLabelText("Name") as HTMLInputElement;
    act(() => input.focus());
    await tick(1000);
    expect(list).toHaveBeenCalledTimes(3);
    expect(document.activeElement).toBe(input);
  });
});

describe("the add form", () => {
  const fill = (name: string, command: string, minutes?: string) => {
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: name } });
    fireEvent.change(screen.getByLabelText("Command"), { target: { value: command } });
    if (minutes !== undefined) {
      fireEvent.change(screen.getByLabelText("Time limit (minutes)"), {
        target: { value: minutes },
      });
    }
  };
  const add = () => screen.getByRole("button", { name: /^Add/ }) as HTMLButtonElement;

  test("Add waits for a name and a command, and sends the limit in seconds", async () => {
    const added = check({ name: "lint", command: "npm run lint" });
    const api = stub({
      list: vi.fn().mockResolvedValueOnce([]).mockResolvedValue([added]),
      add: vi.fn().mockResolvedValue(added),
    });
    await show(api);
    expect((screen.getByLabelText("Time limit (minutes)") as HTMLInputElement).value).toBe("10");
    expect(add().disabled).toBe(true);
    fill("lint", "  ");
    expect(add().disabled).toBe(true);
    fill("lint", "npm run lint");
    expect(add().disabled).toBe(false);
    fireEvent.click(add());
    await settle();
    expect(api.add).toHaveBeenCalledWith(W, {
      name: "lint",
      command: "npm run lint",
      timeout_s: 600,
    });
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("");
    expect(screen.getByText("npm run lint")).toBeTruthy();
  });

  test("a limit in minutes goes out as seconds", async () => {
    const api = stub({ add: vi.fn().mockResolvedValue(check()) });
    await show(api);
    fill("a", "b", "5");
    fireEvent.click(add());
    await settle();
    expect(api.add).toHaveBeenCalledWith(W, { name: "a", command: "b", timeout_s: 300 });
  });

  test("a refusal shows its words and keeps what was typed", async () => {
    const api = stub({ add: vi.fn().mockRejectedValue(new ChecksApiError("invalid_command")) });
    await show(api);
    fill("a", "b");
    fireEvent.click(add());
    await settle();
    expect(screen.getByRole("alert").textContent).toBe(
      "Write the command on one line, up to 8,000 characters.",
    );
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("a");
  });

  test("Adding… shows while the add is asked", async () => {
    const api = stub({ add: vi.fn(() => new Promise<CodeCheckJson>(() => {})) });
    await show(api);
    fill("a", "b");
    fireEvent.click(add());
    await settle();
    expect(screen.getByRole("button", { name: "Adding…" })).toBeTruthy();
  });
});
