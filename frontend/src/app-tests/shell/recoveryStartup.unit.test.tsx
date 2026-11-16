import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App, type AppProps } from "../../App";
import { fakeRest, fakeViki, GENERATION, sessionRow } from "../../accept/m3b/fakes";
import { ShellApiError } from "../../clients/app/shellApi";
import { RootsApiError } from "../../clients/code/rootsApi";
import { resetRoots } from "../../state/sessionRoots";
import { fakeDurable } from "../../testDurable";
import {
  capabilitiesFixture,
  deferredShellApi,
  fakeShellApi,
  settingsFixture,
} from "../../testShell";

beforeEach(() => {
  window.sessionStorage.clear();
  resetRoots();
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  window.sessionStorage.clear();
  resetRoots();
});

function mount(withShell = true) {
  const unused = async (): Promise<never> => {
    throw new Error("unused");
  };
  const organization = {
    list: vi.fn(async () => [
      {
        session_id: "saved-session",
        revision: 1,
        title: "Retained label",
        pinned: false,
        archived: false,
      },
    ]),
    patch: unused,
    remove: unused,
  };
  const clients = {
    organization,
    routinesApi: {
      list: vi.fn(async () => ({
        routines: [],
        scheduler: { state: "running" as const, attended: true, paused: false },
      })),
      get: unused,
      runs: unused,
      preview: unused,
      create: unused,
      revise: unused,
      setState: unused,
      run: unused,
    },
    rootsApi: {
      roots: vi.fn(async () => ({ roots: [], truncated: false })),
      worktrees: vi.fn(async () => ({ worktrees: [], truncated: false })),
      record: unused,
      create: unused,
      remove: unused,
      archives: vi.fn(async () => {
        throw new RootsApiError("unavailable");
      }),
      cleanup: vi.fn(async () => {
        throw new RootsApiError("unavailable");
      }),
      restore: vi.fn(async () => {
        throw new RootsApiError("unavailable");
      }),
    },
    limitsApi: { get: vi.fn(async () => null) },
  } satisfies Partial<AppProps>;
  const deferred = deferredShellApi();
  const shell = fakeShellApi({ settings: settingsFixture({ "onboarding.completed": true }) });
  shell.capabilities = deferred.capabilities;
  const fake = fakeViki();
  const rendered = render(
    <App
      {...fake}
      rest={fakeRest({ sessions: [sessionRow({ session_id: "saved-session" })] })}
      durable={fakeDurable()}
      {...clients}
      {...(withShell ? { shell } : {})}
    />,
  );
  const reads = [
    organization.list,
    clients.routinesApi.list,
    clients.rootsApi.roots,
    clients.rootsApi.worktrees,
    clients.limitsApi.get,
  ];
  return { ...rendered, ...fake, ...deferred, shell, clients, reads };
}

async function settled(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 8; i++) await Promise.resolve();
  });
}

function retry(): HTMLButtonElement {
  return screen.getByRole("button", { name: "Retry app readiness" });
}

function capabilityRead(app: ReturnType<typeof mount>, index: number) {
  const read = app.pending[index];
  if (!read) throw new Error(`missing capabilities read ${index}`);
  return read;
}

test("optional clients wait through unknown mode, initial failure and manual retry into recovery", async () => {
  const app = mount();
  const quiet = () => {
    for (const read of app.reads) expect(read).not.toHaveBeenCalled();
  };
  quiet();
  expect(screen.getByRole("status", { name: "App readiness" }).textContent).toContain(
    "Checking app readiness",
  );
  expect(retry().disabled).toBe(true);
  fireEvent.click(retry());
  expect(app.pending).toHaveLength(1);
  act(() =>
    app.viki.store.dispatch({
      v: 2,
      type: "vault.sessions",
      generation: GENERATION,
      request_id: "",
      session_id: "",
      seq: 1,
      data: { others: [], live: ["a-live-session"] },
    }),
  );
  await act(async () => capabilityRead(app, 0).reject(new ShellApiError(503, "busy")));
  quiet();
  expect(screen.getByRole("alert", { name: "App readiness" }).textContent).toContain(
    "App readiness could not be checked.",
  );
  await act(async () => fireEvent.click(retry()));
  fireEvent.click(retry());
  expect(retry().disabled).toBe(true);
  expect(app.pending).toHaveLength(2);
  await act(async () =>
    capabilityRead(app, 1).resolve(
      capabilitiesFixture({
        app: { mode: "recovery" },
        storage: { state: "unavailable", reason: "busy" },
      }),
    ),
  );
  expect(app.container.querySelector(".recovery-view")).not.toBeNull();
  expect(screen.queryByRole("button", { name: "Retry app readiness" })).toBeNull();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(120_000);
  });
  quiet();

  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Check Git" })));
  expect(app.pending).toHaveLength(3);
  expect(retry().disabled).toBe(true);
  await act(async () => capabilityRead(app, 2).reject(new ShellApiError(503, "busy")));
  expect(app.container.querySelector(".recovery-view")).not.toBeNull();
  const view = app.container.querySelector<HTMLElement>('[data-region="view"]');
  if (!view) throw new Error("no whole-window view");
  expect(within(view).getByRole("alert", { name: "App readiness" })).toBeTruthy();
  quiet();
});

test("manual normal retry starts clients and a later failure keeps normal data with visible feedback", async () => {
  const app = mount();
  await act(async () => capabilityRead(app, 0).reject(new ShellApiError(503, "busy")));
  await act(async () => fireEvent.click(retry()));
  await act(async () =>
    capabilityRead(app, 1).resolve(capabilitiesFixture({ app: { version: "retained-version" } })),
  );
  await settled();
  for (const read of app.reads) expect(read).toHaveBeenCalled();
  expect(screen.getByText("Retained label")).toBeTruthy();
  expect(
    app.shell.calls
      .filter((call) => call.method === "settings" || call.method === "workspaces")
      .map((call) => call.method),
  ).toEqual(["settings", "workspaces"]);

  fireEvent.click(screen.getByRole("button", { name: "App menu" }));
  await act(async () => fireEvent.click(screen.getByRole("menuitem", { name: "Diagnostics" })));
  act(() => app.socket.open());
  expect(retry().disabled).toBe(true);
  await act(async () => capabilityRead(app, 2).reject(new ShellApiError(503, "busy")));
  const view = app.container.querySelector<HTMLElement>('[data-region="view"]');
  if (!view) throw new Error("no whole-window view");
  expect(within(view).getByRole("alert", { name: "App readiness" })).toBeTruthy();
  expect(within(view).getByText("normal")).toBeTruthy();
  expect(within(view).getByText("retained-version")).toBeTruthy();
  expect(app.container.querySelector(".recovery-view")).toBeNull();
  await act(async () => fireEvent.click(retry()));
  expect(retry().disabled).toBe(true);
  await act(async () => capabilityRead(app, 3).resolve(capabilitiesFixture()));
  expect(screen.queryByRole("button", { name: "Retry app readiness" })).toBeNull();
});

test("shell-absent harnesses retain their optional client startup", async () => {
  const app = mount(false);
  await settled();
  for (const read of app.reads) expect(read).toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: "Retry app readiness" })).toBeNull();
});
