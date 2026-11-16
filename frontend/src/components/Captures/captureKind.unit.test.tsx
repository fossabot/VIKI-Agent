/**
 * The `capture` kind in Approvals' children slot. DecisionInbox renders `children`
 * below its native rows, where App already mounts the vault and skill sections; this row joins
 * them there. Gated by `rails`, `vaultSurfaces(shellSnapshot)` and a non-zero
 * `rails.counts.inbox`; its only control opens the triage. It contributes no `/api/v2/decisions`
 * item, no badge count and no Approve, Reject or Later action — `approvalKinds.ts` is untouched.
 *
 * Approvals opens only from Needs you's own Open Approvals button now, and that button renders
 * only while Needs you has a row. Every mount here arranges one background tool permission
 * (`arrangeWaitingRow`) purely to give Needs you that row; none of this file's own assertions are
 * about the permission itself.
 */
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import {
  emptyRails,
  failingRest,
  fakeRest,
  fakeViki,
  GENERATION,
  REQUEST,
} from "../../accept/m3b/fakes";
import type { CaptureListing, CapturesApi } from "../../clients/vaults/capturesApi";
import type { Envelope } from "../../protocol";
import { fakeDurable } from "../../testDurable";
import {
  capabilitiesFixture,
  FAKE_WORKSPACE_ID,
  fakeShellApi,
  listingFixture,
  settingsFixture,
  workspaceFixture,
} from "../../testShell";

beforeEach(() => {
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
  vi.restoreAllMocks();
});

const EMPTY_LISTING: CaptureListing = {
  captures: [],
  projects: [],
  partial: false,
  omitted: 0,
  run: null,
};

function fakeCapturesApi(): CapturesApi {
  return {
    list: async () => EMPTY_LISTING,
    run: async () => null,
    async suggest() {
      throw new Error("suggest() is not used in this test");
    },
  };
}

const SCAFFOLD_SESSION = "s-scaffold";
const SCAFFOLD_REQUEST = "99999999-9999-4999-8999-999999999999";

/** One background tool permission, so Needs you has a row and its Open Approvals button exists. */
async function arrangeWaitingRow(
  viki: ReturnType<typeof fakeViki>["viki"],
  socket: ReturnType<typeof fakeViki>["socket"],
): Promise<void> {
  await act(async () => {
    viki.actions.openSession(SCAFFOLD_SESSION);
  });
  await act(async () => {
    socket.synchronize(SCAFFOLD_SESSION, GENERATION);
  });
  await act(async () => {
    socket.receive({
      v: 2,
      generation: GENERATION,
      request_id: "",
      type: "permission.requested",
      session_id: SCAFFOLD_SESSION,
      seq: 1,
      data: {
        tool_use_id: "tu-scaffold",
        kind: "tool",
        tool: "Write",
        summary: "scaffold.md",
        question: "",
        reason: "",
        agent: "",
        plan: "",
      },
      native_request: { runtime_id: GENERATION, request_id: SCAFFOLD_REQUEST, turn_id: REQUEST },
    } as Envelope);
  });
}

/** A real (non-scratch) workspace shell, onboarding already done. `sharedScratch` is the one
 * field this file varies: true keeps `vaultSurfaces(shellSnapshot)` from holding. */
function workspaceShell(sharedScratch: boolean) {
  return fakeShellApi({
    capabilities: capabilitiesFixture({ workspace: { shared_scratch: sharedScratch } }),
    settings: settingsFixture({ "onboarding.completed": true }),
    workspaces: {
      ...listingFixture([workspaceFixture()]),
      active_id: FAKE_WORKSPACE_ID,
      chat_only: false,
    },
  });
}

async function mount(
  opts: {
    inbox?: number;
    shell?: ReturnType<typeof fakeShellApi>;
    capturesApi?: CapturesApi;
    rest?: ReturnType<typeof fakeRest>;
  } = {},
) {
  const { viki, socket } = fakeViki();
  const rest =
    opts.rest ??
    fakeRest({
      rails: { ...emptyRails(), counts: { knowledge: 0, sources: 0, inbox: opts.inbox ?? 0 } },
    });
  const utils = render(
    <App
      viki={viki}
      rest={rest}
      durable={fakeDurable()}
      {...(opts.shell ? { shell: opts.shell } : {})}
      {...(opts.capturesApi ? { capturesApi: opts.capturesApi } : {})}
    />,
  );
  await act(async () => {
    socket.open();
  });
  await arrangeWaitingRow(viki, socket);
  return { ...utils, socket };
}

async function openApprovals(): Promise<HTMLElement> {
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Open Approvals" }));
  });
  return await screen.findByRole("dialog", { name: "Approvals" });
}

function findCaptureRow(scope: HTMLElement): HTMLElement {
  const row = scope.querySelector<HTMLElement>('section[aria-label="Inbox captures"]');
  if (!row) throw new Error("no capture row in Approvals");
  return row;
}

test("shows the capture row, with the server's inbox count, once Approvals is open", async () => {
  await mount({ inbox: 3 });
  const dialog = await openApprovals();
  expect(dialog.querySelectorAll(".decision-inbox-row")).toHaveLength(1);
  const row = findCaptureRow(dialog);
  expect(within(row).getByText("3 files wait in 00-inbox/.")).toBeTruthy();
  expect(within(row).getByRole("button", { name: "Triage inbox" })).toBeTruthy();
});

test("hides the capture row when the inbox count is zero", async () => {
  await mount({ inbox: 0 });
  const dialog = await openApprovals();
  expect(dialog.querySelector('section[aria-label="Inbox captures"]')).toBeNull();
});

test("hides the capture row while the rails are unavailable, instead of crashing", async () => {
  const rest = failingRest(new Error("rails unavailable"));
  await mount({ rest });
  const dialog = await openApprovals();
  expect(dialog.querySelector('section[aria-label="Inbox captures"]')).toBeNull();
});

test("hides the capture row where vaultSurfaces does not hold, even with captures waiting", async () => {
  await mount({ inbox: 3, shell: workspaceShell(true) });
  const dialog = await openApprovals();
  expect(dialog.querySelector('section[aria-label="Inbox captures"]')).toBeNull();
});

test("its only action: Triage inbox closes Approvals and opens the inbox triage", async () => {
  await mount({ inbox: 2, shell: workspaceShell(false), capturesApi: fakeCapturesApi() });
  const dialog = await openApprovals();
  const button = within(findCaptureRow(dialog)).getByRole("button", { name: "Triage inbox" });
  await act(async () => {
    fireEvent.click(button);
  });
  expect(screen.queryByRole("dialog", { name: "Approvals" })).toBeNull();
  expect(await screen.findByRole("dialog", { name: "Inbox triage" })).toBeTruthy();
});

test("contributes no decisions item, badge count or approve/reject/later action", async () => {
  await mount({ inbox: 4 });
  const dialog = await openApprovals();
  const row = findCaptureRow(dialog);
  expect(row.querySelector("[data-approval-row]")).toBeNull();
  expect(within(row).getAllByRole("button")).toHaveLength(1);
});
