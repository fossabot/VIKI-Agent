/**
 * App-level wiring of the rail's files section: App mounts `ChatSources` (`section.chat-sources`)
 * on its own, with no context meter beside it, only once the selected chat has reported its
 * context or touched at least one file. ChatSources.unit.test.tsx pins the section's own rows and
 * words; this file pins only what App itself wires.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki, GENERATION, sessionRow } from "../../accept/m3b/fakes";
import type { Envelope, EventMap } from "../../protocol";
import { fakeDurable } from "../../testDurable";
import {
  capabilitiesFixture,
  fakeShellApi,
  listingFixture,
  workspaceFixture,
} from "../../testShell";

afterEach(cleanup);

const SID = "11111111-1111-1111-1111-111111111111";
const USAGE = { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_creation_tokens: 0 };

function frame<K extends keyof EventMap>(seq: number, type: K, data: EventMap[K]): Envelope {
  return {
    v: 2,
    generation: GENERATION,
    request_id: "",
    session_id: SID,
    seq,
    type,
    data,
  } as Envelope;
}

/** A vault-backed, non-chat-only shell: `usePublishSources`' own gate (state/sources.ts's `vault`
 * flag) for this session's sources to count as read from the vault. */
function vaultShell() {
  const workspace = workspaceFixture();
  return fakeShellApi({
    capabilities: capabilitiesFixture({
      workspace: {
        active: {
          id: workspace.workspace_id,
          kind: "vault",
          name: workspace.name,
          policy: workspace.policy,
          layout: "unknown",
          source: workspace.source,
          aliased: workspace.aliased,
          root_available: workspace.root_available,
        },
        chat_only: false,
      },
    }),
    workspaces: {
      ...listingFixture([workspace]),
      active_id: workspace.workspace_id,
      chat_only: false,
    },
  });
}

/** Renders the app and opens its one live session; with no `shell`, the app runs without shell
 * services (no vault). */
async function openChat(shell?: ReturnType<typeof fakeShellApi>) {
  const rest = fakeRest({ sessions: [sessionRow({ session_id: SID, title: "Notes" })] });
  const { viki, socket } = fakeViki();
  const { container } = render(
    <App durable={fakeDurable()} viki={viki} rest={rest} {...(shell ? { shell } : {})} />,
  );
  act(() => socket.open());
  const row = await waitFor(() => {
    const found = container.querySelector<HTMLElement>(`[data-session-id="${SID}"]`);
    if (!found) throw new Error("the session row has not arrived");
    return found;
  });
  await act(async () => {
    fireEvent.click(row);
  });
  return { container, socket };
}

const filesSection = (root: ParentNode) => root.querySelector("section.chat-sources");

/** The rail draws neither the old This chat group nor its context meter. */
function expectNoThisChat(root: ParentNode): void {
  expect(root.querySelector(".rail-context")).toBeNull();
  expect(screen.queryByText("This chat", { selector: ".lbl" })).toBeNull();
}

test("a selected chat whose turn read a vault note shows the files section before any context arrives", async () => {
  const { container, socket } = await openChat(vaultShell());
  await act(async () => {
    socket.receive(frame(1, "turn.started", { turn_id: "t1" }));
    socket.receive(
      frame(2, "tool.started", {
        block_id: "b1",
        tool: "Read",
        row: { head: "Read", rest: "30-knowledge/claim.md", tail: "" },
        parent_id: "",
        artifact: {
          artifact_id: "b1",
          kind: "file",
          operation: "read",
          path: "30-knowledge/claim.md",
          path_state: "workspace_relative",
        },
      }),
    );
    socket.receive(
      frame(3, "tool.done", {
        block_id: "b1",
        is_error: false,
        summary: "read",
        preview: "",
        tail: "",
        artifact: {
          artifact_id: "b1",
          observation: "result",
          byte_size: 42,
          sha256: "",
          preview: "",
          truncated: false,
          source_revision: "",
          detail_ref: null,
        },
      }),
    );
    socket.receive(
      frame(4, "turn.done", {
        status: "success",
        usage: USAGE,
        context_percent: null,
        duration_ms: 1,
      }),
    );
  });

  await waitFor(() => expect(filesSection(container)).not.toBeNull());
  expectNoThisChat(container);
});

test("a selected chat with context and no files shows the files section and no rail meter", async () => {
  const { container, socket } = await openChat();
  await act(async () => {
    socket.receive(frame(1, "turn.started", { turn_id: "t1" }));
    socket.receive(frame(2, "text.done", { block_id: "txt1", text: "hi there" }));
    socket.receive(
      frame(3, "turn.done", {
        status: "success",
        usage: USAGE,
        context_percent: 42,
        duration_ms: 1,
      }),
    );
  });

  await waitFor(() => expect(filesSection(container)).not.toBeNull());
  expectNoThisChat(container);
});

test("a selected chat with neither context nor a file shows no files section", async () => {
  const { container } = await openChat();
  expect(filesSection(container)).toBeNull();
  expectNoThisChat(container);
});

test("with no chat selected and no context percent, no files section renders", () => {
  const { container } = render(
    <App durable={fakeDurable()} viki={fakeViki().viki} rest={fakeRest()} />,
  );
  expect(filesSection(container)).toBeNull();
  expectNoThisChat(container);
});
