/**
 * The review client as App wires it: the Workbench (stubbed to the `review` prop it receives) gets
 * the client from `reviewApi`, whether it may act from the platform, and a link opener that sends
 * `open.url`. The review API client and the Workbench have their own suites.
 */
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki } from "../../accept/m3b/fakes";
import type { CodeApi } from "../../clients/code/codeApi";
import type { ReviewApi } from "../../clients/code/reviewApi";
import type { WorkbenchProps } from "../../components/Workbench";
import type { WorkbenchReview } from "../../components/Workbench/Workbench";
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

const shown: { review: WorkbenchReview | undefined } = { review: undefined };

vi.mock("../../components/Workbench", () => ({
  Workbench: (props: WorkbenchProps) => {
    shown.review = props.review;
    return (
      <section>
        <h1>Workbench stub</h1>
        <p data-testid="review">{props.review ? "review prop" : "no review prop"}</p>
      </section>
    );
  },
}));

beforeEach(() => {
  window.sessionStorage.clear();
  shown.review = undefined;
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

const reviewApi = {} as ReviewApi;

async function mountCode(review: ReviewApi | undefined) {
  const fake = fakeViki();
  render(
    <App
      viki={fake.viki}
      rest={fakeRest()}
      durable={fakeDurable()}
      shell={shellApi()}
      codeApi={codeApi()}
      {...(review ? { reviewApi: review } : {})}
    />,
  );
  act(() => fake.socket.open());
  await openWorkbench();
  return fake;
}

test("the Workbench gets the review client", async () => {
  await mountCode(reviewApi);
  expect(screen.getByTestId("review").textContent).toBe("review prop");
  expect(shown.review?.api).toBe(reviewApi);
});

test("without a review client the Workbench gets no review prop", async () => {
  await mountCode(undefined);
  expect(screen.getByTestId("review").textContent).toBe("no review prop");
});

test("onOpenUrl sends open.url with that URL", async () => {
  const fake = await mountCode(reviewApi);
  act(() => shown.review?.onOpenUrl("https://example.test/pull/1"));
  expect(fake.socket.sent.filter((message) => message.type === "open.url")).toEqual([
    { type: "open.url", url: "https://example.test/pull/1" },
  ]);
});
