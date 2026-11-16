import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki, sessionRow } from "../../accept/m3b/fakes";
import type {
  SessionMetadata,
  SessionMetadataApi,
} from "../../clients/conversations/sessionMetadataApi";
import { serializeLayout } from "../../state/paneFormat";
import { focusGroup, moveTab, openTab, singlePane } from "../../state/panes";
import { PANES_STORAGE_KEY } from "../../state/usePanes";
import { fakeDurable } from "../../testDurable";

const ARCHIVED = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const SECOND = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

afterEach(() => {
  cleanup();
  sessionStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("late archive metadata preserves a draft restored beside an independently tracked chat", async () => {
  const layout = focusGroup(
    moveTab(openTab(singlePane(), ARCHIVED), SECOND, { group: "g1", zone: "right" }),
    "g1",
  );
  sessionStorage.setItem(PANES_STORAGE_KEY, serializeLayout(layout));
  let resolveMetadata!: (rows: readonly SessionMetadata[]) => void;
  const metadata = new Promise<readonly SessionMetadata[]>((resolve) => {
    resolveMetadata = resolve;
  });
  const organization: SessionMetadataApi = {
    list: () => metadata,
    patch: async () => {
      throw new Error("unused");
    },
    remove: async () => true,
  };
  const durable = fakeDurable();
  durable.drafts.set("recovered-draft", {
    key: "recovered-draft",
    revision: 1,
    text: "recovered text",
    saved_at: "2026-09-14T00:00:00.000Z",
  });
  const { viki, socket } = fakeViki();
  const opened = vi.spyOn(viki.actions, "openExistingSession");
  const { container } = render(
    <App
      viki={viki}
      rest={fakeRest({
        sessions: [ARCHIVED, SECOND].map((session_id) => sessionRow({ session_id })),
      })}
      durable={durable}
      organization={organization}
    />,
  );
  await act(async () => {
    socket.open();
    socket.receive({
      v: 2,
      type: "vault.sessions",
      session_id: "",
      generation: "",
      request_id: "",
      seq: 1,
      data: { live: [ARCHIVED], others: [] },
    });
  });
  expect(opened).not.toHaveBeenCalled();
  expect(sessionStorage.getItem("viki.selected")).toBeNull();
  const drafts = screen.getByText("Drafts and send history").closest("details");
  expect(drafts).not.toBeNull();
  if (!drafts) throw new Error("Draft recovery is unavailable");
  drafts.open = true;
  await act(async () => {
    fireEvent(drafts, new Event("toggle"));
  });
  await act(async () => {
    fireEvent.click(screen.getByLabelText("Restore text from draft recovered-draft"));
  });
  expect(screen.getByLabelText<HTMLTextAreaElement>("Message").value).toBe("recovered text");
  await act(async () => {
    resolveMetadata([
      { session_id: ARCHIVED, revision: 1, title: "", pinned: false, archived: true },
      { session_id: SECOND, revision: 1, title: "", pinned: false, archived: false },
    ]);
  });
  expect(container.querySelector(`#session-tab-${ARCHIVED}`)).toBeNull();
  expect(screen.getByLabelText<HTMLTextAreaElement>("Message").value).toBe("recovered text");
  expect(sessionStorage.getItem("viki.selected")).toBeNull();
});
