/**
 * The chat header's "+N folder" chip, wired to the real session store. `additional_folders`
 * arrives on `session.started`; this file is the part only a full App render
 * can prove — that a pane's own chip tracks its own session and opens its menu, and that no
 * folders means no chip at all. Opens the real Radix dropdown once.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki, GENERATION } from "../../accept/m3b/fakes";
import type { CodeCopy, Envelope, EventMap } from "../../protocol";
import { fakeDurable } from "../../testDurable";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.sessionStorage.clear();
});

function frame<K extends keyof EventMap>(
  sessionId: string,
  seq: number,
  type: K,
  data: EventMap[K],
): Envelope {
  return {
    v: 2,
    generation: GENERATION,
    request_id: "",
    type,
    session_id: sessionId,
    seq,
    data,
  } as Envelope;
}

function started(
  sessionId: string,
  seq: number,
  additionalFolders?: string[],
  codeCopy?: CodeCopy,
): Envelope {
  return frame(sessionId, seq, "session.started", {
    model: "sonnet",
    mode: "default",
    effort: "low",
    commands: [],
    agents: [],
    models: [],
    cli_version: "2.1.259",
    resumed: true,
    ...(additionalFolders ? { additional_folders: additionalFolders } : {}),
    ...(codeCopy ? { code_copy: codeCopy } : {}),
  });
}

test("a session.started with one folder shows +1 folder and its menu; none shows no chip", async () => {
  const rest = fakeRest();
  const { viki, socket } = fakeViki();
  render(<App durable={fakeDurable()} viki={viki} rest={rest} />);
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));

  fireEvent.click(screen.getByText("New session"));
  const opened = socket.sent.find((sent) => sent.type === "session.open") as {
    session_id: string;
  };
  const sessionId = opened.session_id;

  // No additional_folders on session.started: the chip never renders.
  act(() => socket.receive(started(sessionId, 1)));
  expect(screen.queryByRole("button", { name: /folder/i })).toBeNull();

  // The same session gaining one extra folder shows the chip and its menu.
  act(() => socket.receive(started(sessionId, 2, ["C:\\work\\other"])));
  const trigger = screen.getByRole("button", { name: "+1 folder" });

  fireEvent.click(trigger);
  expect(screen.getByRole("menu").textContent).toContain("other");
});

test("a code copy alone shows the chip for its own session only", async () => {
  const rest = fakeRest();
  const { viki, socket } = fakeViki();
  render(<App durable={fakeDurable()} viki={viki} rest={rest} />);
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));

  fireEvent.click(screen.getByText("New session"));
  const first = (socket.sent.find((sent) => sent.type === "session.open") as { session_id: string })
    .session_id;
  const copy = { path: "C:\\data\\worktrees\\abc", folder: "my-app", branch: "viki/bot-1-abc123" };
  act(() => socket.receive(started(first, 1, undefined, copy)));
  expect(screen.getByRole("button", { name: "+1 folder" })).toBeTruthy();

  // A different selected session has no copy: the first one's copy does not leak into it.
  fireEvent.click(screen.getByText("New session"));
  const second = (
    socket.sent.filter((sent) => sent.type === "session.open").at(-1) as { session_id: string }
  ).session_id;
  expect(second).not.toBe(first);
  act(() => socket.receive(started(second, 1)));
  expect(screen.queryByRole("button", { name: /folder/i })).toBeNull();
});
