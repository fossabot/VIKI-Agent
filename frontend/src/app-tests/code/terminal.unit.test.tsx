/**
 * The terminal client as App wires it: the Workbench (stubbed to the `terminal` prop it receives)
 * gets the client from `terminalApi` on each supported desktop. The
 * terminal client and the Workbench have their own suites.
 */
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki } from "../../accept/m3b/fakes";
import type { CodeApi } from "../../clients/code/codeApi";
import type { TerminalApi } from "../../clients/code/terminalApi";
import type { WorkbenchProps } from "../../components/Workbench";
import type { WorkbenchTerminal } from "../../components/Workbench/Workbench";
import { fakeDurable } from "../../testDurable";
import {
  capabilitiesFixture,
  fakeShellApi,
  listingFixture,
  settingsFixture,
  workspaceFixture,
} from "../../testShell";
import { openWorkbench } from "../../testWorkbench";

const CODE_ID = "55555555-5555-4555-8555-555555555555";

function shellApi() {
  const code = workspaceFixture({ workspace_id: CODE_ID, kind: "code", name: "app code" });
  return fakeShellApi({
    capabilities: capabilitiesFixture({
      workspace: {
        shared_scratch: false,
        chat_only: false,
        active: {
          id: CODE_ID,
          kind: "code",
          name: "app code",
          policy: code.policy,
          layout: "unknown",
          source: "created",
          aliased: false,
          root_available: true,
        },
      },
    }),
    settings: settingsFixture({ "onboarding.completed": true }),
    workspaces: { ...listingFixture([code]), active_id: CODE_ID, chat_only: false },
  });
}

const shown: { terminal: WorkbenchTerminal | undefined } = { terminal: undefined };

vi.mock("../../components/Workbench", () => ({
  Workbench: (props: WorkbenchProps) => {
    shown.terminal = props.terminal;
    return (
      <section>
        <h1>Workbench stub</h1>
        <p data-testid="terminal">{props.terminal ? "terminal prop" : "no terminal prop"}</p>
      </section>
    );
  },
}));

beforeEach(() => {
  window.sessionStorage.clear();
  shown.terminal = undefined;
});

afterEach(() => {
  cleanup();
  delete (window.navigator as { platform?: string }).platform;
  window.sessionStorage.clear();
  vi.restoreAllMocks();
});

function codeApi(): CodeApi {
  const pending = () => new Promise<never>(() => {});
  return {
    tree: pending,
    file: pending,
    search: pending,
    changes: pending,
    diff: pending,
    save: pending,
  };
}

const terminalApi = {} as TerminalApi;

async function mountCode(terminal: TerminalApi | undefined) {
  const fake = fakeViki();
  render(
    <App
      viki={fake.viki}
      rest={fakeRest()}
      durable={fakeDurable()}
      shell={shellApi()}
      codeApi={codeApi()}
      {...(terminal ? { terminalApi: terminal } : {})}
    />,
  );
  act(() => fake.socket.open());
  await openWorkbench();
  return fake;
}

test("the Workbench gets the terminal launcher", async () => {
  await mountCode(terminalApi);
  expect(screen.getByTestId("terminal").textContent).toBe("terminal prop");
  expect(shown.terminal?.api).toBe(terminalApi);
});

test("without a terminal client the Workbench gets no terminal prop", async () => {
  await mountCode(undefined);
  expect(screen.getByTestId("terminal").textContent).toBe("no terminal prop");
});

test.each(["Win32", "MacIntel", "Linux x86_64"])(
  "terminals are available on %s",
  async (platform) => {
    Object.defineProperty(window.navigator, "platform", { value: platform, configurable: true });
    await mountCode(terminalApi);
    expect(shown.terminal?.canOpen).toBe(true);
  },
);
