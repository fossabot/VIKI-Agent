/**
 * Acceptance (frozen): the newer-transcript notice, the status bar's third notice.
 *
 * Both halves of it are on the wire. `catalog` reads each
 * transcript's own `version` field onto its row, and `session.started` reports the CLI that
 * actually answered; the status bar has a `notice` slot that the other
 * two notices use. This file pins the line that compares them.
 *
 * What the notice is for: a transcript written by a newer Claude Code can hold entry shapes this
 * build has never seen. It is deliberately a notice and not a refusal — a newer transcript
 * version is only a status-bar notice — so it is said quietly, in the chrome's
 * own English, and nothing is blocked.
 *
 * The comparison is field-by-field numeric, because the one thing a string compare gets wrong
 * here is exactly the case that matters: "2.1.9" > "2.1.10" alphabetically, and these versions
 * are already past .259. An unrecognisable version makes it silent rather than guessing — a
 * notice claiming a staleness nobody established is worse than no notice.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import type { Envelope, EventMap, Rails } from "../../protocol";
import { isNewerCli } from "../../state/rows";
import { fakeDurable } from "../../testDurable";
import { fakeRest, fakeViki, localIso, sessionRow } from "../m3b/fakes";

afterEach(cleanup);

const day = (h: number, m: number) => localIso(2026, 9, 13, h, m);

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(day(20, 0)));
  window.sessionStorage.clear();
});

afterEach(() => {
  vi.useRealTimers();
  window.sessionStorage.clear();
});

test("a newer transcript is newer; an older or equal one is not", () => {
  expect(isNewerCli("2.1.266", "2.1.259")).toBe(true);
  expect(isNewerCli("2.2.0", "2.1.999")).toBe(true);
  expect(isNewerCli("3.0", "2.1.259")).toBe(true);
  expect(isNewerCli("2.1.259", "2.1.259")).toBe(false);
  expect(isNewerCli("2.1.99", "2.1.259")).toBe(false);
  // The case a string compare gets backwards, which is the whole reason this is not a >.
  expect(isNewerCli("2.1.10", "2.1.9")).toBe(true);
  expect(isNewerCli("2.1.9", "2.1.10")).toBe(false);
  // A longer version is newer only if it actually differs: 2.1.259.1 > 2.1.259, 2.1 == 2.1.0.
  expect(isNewerCli("2.1.259.1", "2.1.259")).toBe(true);
  expect(isNewerCli("2.1", "2.1.0")).toBe(false);
});

test("anything it cannot read is silent, never a guess", () => {
  expect(isNewerCli(undefined, "2.1.259")).toBe(false);
  expect(isNewerCli("", "2.1.259")).toBe(false);
  expect(isNewerCli("2.2.0-rc1", "2.1.259")).toBe(false);
  expect(isNewerCli("next", "2.1.259")).toBe(false);
  expect(isNewerCli("2.1.266", "")).toBe(false);
});

// --- through the app ----------------------------------------------------------------------------

function rails(): Rails {
  return {
    agents: [],
    gates: [],
    routines: [],
    counts: { knowledge: 132, sources: 202, inbox: 1 },
    git: {
      state: "known",
      ref_state: "branch",
      branch: "main",
      head: "c8868e6",
      committed_at: day(19, 16),
      dirty: 0,
    },
  };
}

function frame<K extends keyof EventMap>(
  session: string,
  seq: number,
  type: K,
  data: EventMap[K],
): Envelope {
  return {
    v: 2,
    generation: "11111111-1111-4111-8111-111111111111",
    request_id: "",
    type,
    session_id: session,
    seq,
    data,
  } as Envelope;
}

function mount(transcriptVersion: string) {
  const rest = fakeRest({
    sessions: [
      sessionRow({
        session_id: "s-1",
        title: "retro H1–H4",
        modified: day(17, 20),
        cli_version: transcriptVersion,
      }),
    ],
    rails: rails(),
  });
  const { viki, socket } = fakeViki();
  const durable = fakeDurable();
  const utils = render(<App viki={viki} rest={rest} durable={durable} />);
  act(() => socket.open());
  return { ...utils, socket };
}

/** Open the row and let the session report the CLI that actually answered. */
async function open(container: HTMLElement, socket: ReturnType<typeof mount>["socket"]) {
  const row = await waitFor(() => {
    const found = container.querySelector<HTMLElement>('[data-session-id="s-1"]');
    if (!found) throw new Error("no row");
    return found;
  });
  fireEvent.click(row);
  act(() =>
    socket.receive(
      frame("s-1", 1, "session.started", {
        model: "sonnet",
        mode: "auto",
        effort: "low",
        commands: [],
        agents: [],
        models: [],
        cli_version: "2.1.259",
        resumed: true,
      }),
    ),
  );
}

function statusBar(): HTMLElement {
  return screen.getByText((_, node) => node?.getAttribute("data-region") === "statusbar", {
    selector: "footer",
  });
}

test("the status bar says so when the transcript is newer than the CLI answering it", async () => {
  const { container, socket } = mount("2.1.266");
  await open(container, socket);
  await waitFor(() => expect(statusBar().textContent).toContain("newer transcript"));
  // It names the version, so the notice is checkable rather than merely alarming.
  expect(statusBar().textContent).toContain("2.1.266");
});

/* AMENDMENT (2026-10-20). Dropped the status
   bar's CLI version text: the status bar no longer renders
   the session's CLI version at all, so this test's settle signal no longer waits on "2.1.259"
   there. It waits instead for the header's mode chip, which the same session.started frame that
   reports the CLI version also sets. */

test("the same CLI on both sides says nothing at all", async () => {
  const { container, socket } = mount("2.1.259");
  await open(container, socket);
  await screen.findByRole("button", { name: /^Mode: Auto/ });
  expect(statusBar().textContent).not.toContain("newer transcript");
});
