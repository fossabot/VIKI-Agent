/**
 * The checks client as App wires it: the Workbench (stubbed to the `checks` prop it receives) gets
 * the client from `checksApi` on each supported desktop. The checks client
 * and the Workbench have their own suites.
 */
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki } from "../../accept/m3b/fakes";
import type { ChecksApi } from "../../clients/code/checksApi";
import type { CodeApi } from "../../clients/code/codeApi";
import type { WorkbenchProps } from "../../components/Workbench";
import type { WorkbenchChecks } from "../../components/Workbench/Workbench";
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

const shown: { checks: WorkbenchChecks | undefined } = { checks: undefined };

vi.mock("../../components/Workbench", () => ({
  Workbench: (props: WorkbenchProps) => {
    shown.checks = props.checks;
    return (
      <section>
        <h1>Workbench stub</h1>
        <p data-testid="checks">{props.checks ? "checks prop" : "no checks prop"}</p>
      </section>
    );
  },
}));

beforeEach(() => {
  window.sessionStorage.clear();
  shown.checks = undefined;
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

const checksApi = {} as ChecksApi;

async function mountCode(checks: ChecksApi | undefined) {
  const fake = fakeViki();
  render(
    <App
      viki={fake.viki}
      rest={fakeRest()}
      durable={fakeDurable()}
      shell={shellApi()}
      codeApi={codeApi()}
      {...(checks ? { checksApi: checks } : {})}
    />,
  );
  act(() => fake.socket.open());
  await openWorkbench();
  return fake;
}

test("the Workbench gets the checks client", async () => {
  await mountCode(checksApi);
  expect(screen.getByTestId("checks").textContent).toBe("checks prop");
  expect(shown.checks?.api).toBe(checksApi);
});

test("without a checks client the Workbench gets no checks prop", async () => {
  await mountCode(undefined);
  expect(screen.getByTestId("checks").textContent).toBe("no checks prop");
});

test.each(["Win32", "MacIntel", "Linux x86_64"])("checks are available on %s", async (platform) => {
  Object.defineProperty(window.navigator, "platform", { value: platform, configurable: true });
  await mountCode(checksApi);
  expect(shown.checks?.canRun).toBe(true);
});
