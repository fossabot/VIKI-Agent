import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { App } from "../../App";
import { emptyRails, fakeRest, fakeViki, GENERATION, sessionRow } from "../../accept/m3b/fakes";
import type { Envelope, EventMap, GitChip, Rails } from "../../protocol";
import { fakeDurable } from "../../testDurable";

afterEach(() => {
  cleanup();
  sessionStorage.clear();
});
const SID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const unknown: GitChip = {
  state: "unknown",
  ref_state: "branch",
  branch: "main",
  head: "abc1234",
  committed_at: "",
  dirty: null,
};

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

async function openWriter(chip: GitChip | undefined) {
  const rails = { ...emptyRails(), git: chip } as Rails;
  const rest = fakeRest({
    rails,
    sessions: [sessionRow({ session_id: SID, title: "Synthetic writer" })],
  });
  const { viki, socket } = fakeViki();
  const view = render(<App viki={viki} durable={fakeDurable()} rest={rest} />);
  await act(async () => {
    socket.open();
  });
  await act(async () => {
    fireEvent.click(screen.getByText("Synthetic writer"));
  });
  await act(async () => {
    socket.receive(frame(1, "turn.started", { turn_id: "t1" }));
    socket.receive(
      frame(2, "tool.started", {
        block_id: "b1",
        tool: "Write",
        row: { head: "", rest: "synthetic.md", tail: "" },
        parent_id: "",
      }),
    );
    socket.receive(
      frame(3, "turn.done", {
        status: "success",
        usage: {
          input_tokens: 0,
          output_tokens: 0,
          cache_read_tokens: 0,
          cache_creation_tokens: 0,
        },
        context_percent: null,
        duration_ms: 1,
      }),
    );
  });
  return { ...view, viki, socket, rest };
}

test.each([
  unknown,
  { ...unknown, state: "not_repository", ref_state: "unknown", branch: "", head: "" } as GitChip,
  { ...unknown, state: "known", ref_state: "branch", dirty: 3 } as GitChip,
  undefined,
])(
  "unknown, nonrepo, a dirty known vault and missing Git facts no longer gate close or unload",
  async (chip) => {
    const { container, viki, socket } = await openWriter(chip);
    const event = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(false);
    expect(screen.queryByText("Uncommitted")).toBeNull();
    const menu = container.querySelector<HTMLButtonElement>(".sess-menu");
    if (!menu) throw new Error("missing row menu");
    fireEvent.click(menu);
    await act(async () => {
      fireEvent.click(screen.getByRole("menuitem", { name: "Close session" }));
    });
    expect(screen.queryByRole("alertdialog")).toBeNull();
    await waitFor(() =>
      expect(socket.sent.filter((x) => x.type === "session.close")).toHaveLength(1),
    );
    viki.client.close();
  },
);

test("a writer's clean evidence is withdrawn during refresh and stays unknown after a failure", async () => {
  const clean: GitChip = { ...unknown, state: "known", ref_state: "branch", dirty: 0 };
  const { container, viki, socket, rest } = await openWriter(clean);
  let rejectRead: (error: Error) => void = () => {
    throw new Error("no pending rails read");
  };
  rest.rails = () =>
    new Promise<Rails>((_resolve, reject) => {
      rejectRead = reject;
    });
  await act(async () => {
    socket.receive(frame(4, "turn.started", { turn_id: "t2" }));
    socket.receive(
      frame(5, "turn.done", {
        status: "success",
        usage: {
          input_tokens: 0,
          output_tokens: 0,
          cache_read_tokens: 0,
          cache_creation_tokens: 0,
        },
        context_percent: null,
        duration_ms: 1,
      }),
    );
  });
  expect(container.querySelector(".git-chip")?.textContent).toContain("checking");
  await act(async () => {
    rejectRead(new Error("synthetic rails failure"));
  });
  const sessionReads = rest.calls.filter((x) => x === "sessions").length;
  await act(async () => {
    viki.store.dispatch({
      v: 2,
      generation: GENERATION,
      request_id: "",
      type: "vault.sessions",
      session_id: "",
      seq: 10,
      data: { others: [], live: [] },
    });
  });
  expect(rest.calls.filter((x) => x === "sessions").length).toBeGreaterThan(sessionReads);
  expect(container.querySelector(".git-chip")?.textContent).toContain("status unknown");
  viki.client.close();
});
