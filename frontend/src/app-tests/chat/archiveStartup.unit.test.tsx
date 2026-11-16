import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki, sessionRow } from "../../accept/m3b/fakes";
import type {
  SessionMetadata,
  SessionMetadataApi,
} from "../../clients/conversations/sessionMetadataApi";
import { serializeLayout } from "../../state/paneFormat";
import { openTab, singlePane } from "../../state/panes";
import { LAYOUT_SETTING, PANES_PENDING_KEY, PANES_STORAGE_KEY } from "../../state/usePanes";
import { fakeDurable } from "../../testDurable";
import { fakeShellApi, settingsFixture } from "../../testShell";

const ARCHIVED = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const SECOND = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

afterEach(() => {
  cleanup();
  sessionStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test.each(["local", "settings"] as const)(
  "a restored %s pane waits for metadata and leaves archived chats closed",
  async (source) => {
    const layout = serializeLayout(openTab(singlePane(), ARCHIVED));
    sessionStorage.setItem(
      PANES_STORAGE_KEY,
      source === "local" ? layout : serializeLayout(singlePane()),
    );
    if (source === "local") sessionStorage.setItem("viki.selected", ARCHIVED);
    else sessionStorage.setItem(PANES_PENDING_KEY, "1");
    let resolveMetadata!: (rows: readonly SessionMetadata[]) => void;
    const pending = new Promise<readonly SessionMetadata[]>((resolve) => {
      resolveMetadata = resolve;
    });
    const organization: SessionMetadataApi = {
      list: vi.fn(() => pending),
      patch: async () => {
        throw new Error("unused");
      },
      remove: async () => true,
    };
    const settings = settingsFixture({ "onboarding.completed": true });
    const shell = fakeShellApi({
      settings: {
        ...settings,
        effective: { ...settings.effective, [LAYOUT_SETTING]: layout },
      },
    });
    const { viki, socket } = fakeViki();
    const opened = vi.spyOn(viki.actions, "openExistingSession");
    const { container } = render(
      <App
        viki={viki}
        rest={fakeRest({
          sessions: [sessionRow({ session_id: ARCHIVED, title: "Archived conversation" })],
        })}
        durable={fakeDurable()}
        organization={organization}
        {...(source === "settings" ? { shell } : {})}
      />,
    );
    await act(async () => {
      socket.open();
      for (let step = 0; step < 30; step++) await Promise.resolve();
    });
    const openedBeforeMetadata = opened.mock.calls.map(([id]) => id);
    await act(async () => {
      resolveMetadata([
        { session_id: ARCHIVED, revision: 1, title: "", pinned: false, archived: true },
      ]);
    });
    expect(openedBeforeMetadata).not.toContain(ARCHIVED);
    expect(opened.mock.calls.map(([id]) => id)).not.toContain(ARCHIVED);
    expect(container.querySelector(`#session-tab-${ARCHIVED}`)).toBeNull();
    expect(sessionStorage.getItem("viki.selected")).not.toBe(ARCHIVED);
  },
);

test.each(["unarchived", "explicit", "failed"] as const)(
  "startup metadata %s preserves deliberate navigation and controls automatic opens",
  async (outcome) => {
    sessionStorage.setItem(
      PANES_STORAGE_KEY,
      serializeLayout(openTab(openTab(singlePane(), ARCHIVED), SECOND)),
    );
    sessionStorage.setItem("viki.selected", ARCHIVED);
    let resolveMetadata!: (rows: readonly SessionMetadata[]) => void;
    let rejectMetadata!: () => void;
    const pending = new Promise<readonly SessionMetadata[]>((resolve, reject) => {
      resolveMetadata = resolve;
      rejectMetadata = () => reject(new Error("unavailable"));
    });
    const organization: SessionMetadataApi = {
      list: vi.fn(() => pending),
      patch: async () => {
        throw new Error("unused");
      },
      remove: async () => true,
    };
    const { viki, socket } = fakeViki();
    const opened = vi.spyOn(viki.actions, "openExistingSession");
    const visited = vi.spyOn(viki.actions, "openSession");
    const { container } = render(
      <App
        viki={viki}
        rest={fakeRest({
          sessions: [ARCHIVED, SECOND].map((session_id) => sessionRow({ session_id })),
        })}
        durable={fakeDurable()}
        organization={organization}
        shell={fakeShellApi({ settings: settingsFixture({ "onboarding.completed": true }) })}
      />,
    );
    await act(async () => {
      socket.open();
      for (let step = 0; step < 30; step++) await Promise.resolve();
    });
    expect(opened).not.toHaveBeenCalled();
    if (outcome === "explicit") {
      for (const id of [ARCHIVED, SECOND]) {
        await act(async () => {
          fireEvent.click(
            container.querySelector<HTMLElement>(`.sess[data-session-id="${id}"]`) as HTMLElement,
          );
          socket.synchronize(id);
        });
      }
    }
    await act(async () => {
      if (outcome === "failed") rejectMetadata();
      else
        resolveMetadata(
          [ARCHIVED, SECOND].map((session_id) => ({
            session_id,
            revision: 1,
            title: "",
            pinned: false,
            archived: outcome === "explicit",
          })),
        );
    });
    if (outcome === "failed") expect(opened).not.toHaveBeenCalled();
    else if (outcome === "explicit") {
      expect(opened).not.toHaveBeenCalled();
      expect(visited.mock.calls.map(([id]) => id)).toEqual([ARCHIVED, SECOND]);
      for (const id of [ARCHIVED, SECOND])
        expect(container.querySelector(`#session-tab-${id}`)).not.toBeNull();
      expect(sessionStorage.getItem("viki.selected")).toBe(SECOND);
    } else {
      expect(opened).toHaveBeenCalledWith(SECOND);
      expect(sessionStorage.getItem("viki.selected")).toBe(ARCHIVED);
      expect(container.querySelector(`#session-tab-${ARCHIVED}`)).not.toBeNull();
    }
  },
);
