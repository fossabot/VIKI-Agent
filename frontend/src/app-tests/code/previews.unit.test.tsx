/**
 * The previews client as App wires it: the Workbench (stubbed to the `previews` prop it receives) gets
 * the client from `previewsApi` and whether it may run previews from the platform. The previews client
 * and the Workbench have their own suites.
 */
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki } from "../../accept/m3b/fakes";
import type { CodeApi } from "../../clients/code/codeApi";
import type { PreviewsApi } from "../../clients/code/previewsApi";
import type { WorkbenchProps } from "../../components/Workbench";
import type { WorkbenchPreviews } from "../../components/Workbench/Workbench";
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

const shown: { previews: WorkbenchPreviews | undefined } = { previews: undefined };

vi.mock("../../components/Workbench", () => ({
  Workbench: (props: WorkbenchProps) => {
    shown.previews = props.previews;
    return (
      <section>
        <h1>Workbench stub</h1>
        <p data-testid="previews">{props.previews ? "previews prop" : "no previews prop"}</p>
      </section>
    );
  },
}));

beforeEach(() => {
  window.sessionStorage.clear();
  shown.previews = undefined;
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

const previewsApi = {} as PreviewsApi;

async function mountCode(previews: PreviewsApi | undefined) {
  const fake = fakeViki();
  render(
    <App
      viki={fake.viki}
      rest={fakeRest()}
      durable={fakeDurable()}
      shell={shellApi()}
      codeApi={codeApi()}
      {...(previews ? { previewsApi: previews } : {})}
    />,
  );
  act(() => fake.socket.open());
  await openWorkbench();
  return fake;
}

test("the Workbench gets the previews client", async () => {
  await mountCode(previewsApi);
  expect(screen.getByTestId("previews").textContent).toBe("previews prop");
  expect(shown.previews?.api).toBe(previewsApi);
});

test("without a previews client the Workbench gets no previews prop", async () => {
  await mountCode(undefined);
  expect(screen.getByTestId("previews").textContent).toBe("no previews prop");
});

test("canRun follows the platform", async () => {
  Object.defineProperty(window.navigator, "platform", { value: "Win32", configurable: true });
  await mountCode(previewsApi);
  expect(shown.previews?.canRun).toBe(true);
  cleanup();
  Object.defineProperty(window.navigator, "platform", { value: "MacIntel", configurable: true });
  await mountCode(previewsApi);
  expect(shown.previews?.canRun).toBe(false);
});
