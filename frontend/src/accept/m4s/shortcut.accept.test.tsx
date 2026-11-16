/**
 * Acceptance (frozen): a new-session shortcut that works in the Edge app (Ctrl+N or
 * Ctrl+Shift+O).
 *
 * TWO chords, and the reason is worth writing down rather than discovering later.
 *
 * Both candidates are Chromium browser accelerators, handled in the browser
 * process before the page ever sees the key: Ctrl+N opens a new window, and Ctrl+Shift+O is
 * Edge's Favourites manager. An `--app` window narrows that set but does not empty it, and
 * nothing this app can pass on the command line changes it. Worse, no test here can settle it:
 * playwright drives input through CDP, which is delivered straight to the renderer and therefore
 * bypasses exactly the layer in question — a green e2e press would be evidence of nothing.
 *
 * So the app binds Ctrl+Shift+O, which is the one to prefer if Edge
 * does deliver it, AND Alt+N, which is not a Chromium accelerator and will arrive. Which of the
 * two the user actually gets is a question only a real key press in Edge answers, so this file
 * does not guess at it. Binding both costs one predicate.
 *
 * What this file pins is the part that is ours: the chord reaches App's own handler and does
 * exactly what the New session button does — including refusing at the session limit, because a
 * shortcut that quietly bypassed the cap the button respects would be a second way to open a
 * fourth session that the user never sees refused.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import type { Rails } from "../../protocol";
import { fakeDurable } from "../../testDurable";
import { fakeRest, fakeViki, localIso } from "../m3b/fakes";

afterEach(cleanup);

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(localIso(2026, 9, 13, 8, 0)));
});

afterEach(() => {
  vi.useRealTimers();
});

function rails(): Rails {
  return {
    agents: [],
    gates: [],
    routines: [],
    counts: { knowledge: 1, sources: 1, inbox: 0 },
    git: {
      state: "known",
      ref_state: "branch",
      branch: "main",
      head: "abc1234",
      committed_at: localIso(2026, 9, 13, 7, 0),
      dirty: 0,
    },
  };
}

function mount() {
  const rest = fakeRest({ sessions: [], rails: rails() });
  const { viki, socket } = fakeViki();
  const durable = fakeDurable();
  const utils = render(<App viki={viki} rest={rest} durable={durable} />);
  act(() => socket.open());
  socket.sent.length = 0;
  return { ...utils, socket };
}

/** What the New session button puts on the wire: one session.open for a brand-new id. */
function opened(socket: { sent: Record<string, unknown>[] }): string[] {
  return socket.sent
    .filter((frame) => frame.type === "session.open")
    .map((frame) => String(frame.session_id));
}

test("Alt+N opens a new session, exactly as the button does", async () => {
  const { socket } = mount();
  fireEvent.keyDown(window, { key: "n", altKey: true });
  await waitFor(() => expect(opened(socket)).toHaveLength(1));
});

test("Ctrl+Shift+O does the same, for the launch where Edge lets it through", async () => {
  const { socket } = mount();
  // Shift resolves event.key to "O"; the chord is matched case-insensitively, like Ctrl+K.
  fireEvent.keyDown(window, { key: "O", ctrlKey: true, shiftKey: true });
  await waitFor(() => expect(opened(socket)).toHaveLength(1));
});

test("the plain letter, and the letter with the wrong modifier, are just typing", () => {
  const { socket } = mount();
  fireEvent.keyDown(window, { key: "n" });
  fireEvent.keyDown(window, { key: "n", ctrlKey: true }); // Ctrl+N belongs to the browser
  fireEvent.keyDown(window, { key: "o", ctrlKey: true }); // without Shift: not the chord
  expect(opened(socket)).toEqual([]);
});

test("the shortcut respects the session limit the button respects", async () => {
  const { socket } = mount();
  for (let i = 0; i < 3; i++) {
    fireEvent.keyDown(window, { key: "n", altKey: true });
    await waitFor(() => expect(opened(socket)).toHaveLength(i + 1));
  }
  // The fourth is refused the same way, with the same toast — never silently opened.
  fireEvent.keyDown(window, { key: "n", altKey: true });
  await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
  expect(opened(socket)).toHaveLength(3);
});
