/**
 * decisionQueue: DecisionQueueProvider, useDecisionQueue, UndoBar and focusNextRow.
 * Most tests render the hook and <UndoBar /> together under one provider (renderHook's `wrapper`
 * mounts both), so one test can drive the DecisionQueue contract through `result.current` and
 * read the bar's real markup — the fake-timer technique RunPeek.unit.test.tsx uses for its own
 * polling hook. focusNextRow is plain DOM and needs neither a provider nor a timer.
 */
import { act, cleanup, fireEvent, renderHook, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import {
  DecisionQueueProvider,
  focusNextRow,
  UNDO_MS,
  UndoBar,
  useDecisionQueue,
} from "./decisionQueue";

function setup(
  delayMs: number = UNDO_MS,
  { onLateFailure = vi.fn(), reactStrictMode = false } = {},
) {
  return renderHook(() => useDecisionQueue(), {
    reactStrictMode,
    wrapper: ({ children }: { children: ReactNode }) => (
      <DecisionQueueProvider delayMs={delayMs} onLateFailure={onLateFailure}>
        {children}
        <UndoBar />
      </DecisionQueueProvider>
    ),
  });
}

async function settle(): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
}

async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

/** A fake sender that holds every call open until the test releases it: `started` lists the calls
 * in the order they began, and `release(i)` lets the i-th one resolve. */
function heldSends() {
  const started: string[] = [];
  const releases: (() => void)[] = [];
  return {
    started,
    release: (index: number) => releases[index]?.(),
    send: (name: string) => () => {
      started.push(name);
      return new Promise<void>((resolve) => {
        releases.push(resolve);
      });
    },
  };
}

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
  vi.useRealTimers();
});

test("UndoBar renders nothing with no pending row and no failure", () => {
  setup();
  expect(screen.queryByRole("status")).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
});

test("sends the decision once the undo window expires, with a custom delayMs", async () => {
  const send = vi.fn().mockResolvedValue(undefined);
  const { result } = setup(1200);

  act(() => result.current.enqueue("k1", "Deny the request", send));
  expect(result.current.pending).toEqual([{ key: "k1", label: "Deny the request" }]);
  expect(document.querySelector(".decision-undo-label")?.textContent).toBe(
    "Deny the request · sends in 5 seconds",
  );

  await advance(1199);
  expect(send).not.toHaveBeenCalled();

  await advance(1);
  expect(send).toHaveBeenCalledTimes(1);
  expect(result.current.pending).toEqual([]);
});

test("defaults the undo window to UNDO_MS when delayMs is omitted", async () => {
  const send = vi.fn().mockResolvedValue(undefined);
  const { result } = renderHook(() => useDecisionQueue(), {
    wrapper: ({ children }: { children: ReactNode }) => (
      <DecisionQueueProvider>{children}</DecisionQueueProvider>
    ),
  });

  act(() => result.current.enqueue("k1", "Approve", send));
  await advance(UNDO_MS - 1);
  expect(send).not.toHaveBeenCalled();
  await advance(1);
  expect(send).toHaveBeenCalledTimes(1);
});

test("the same key replaces the entry: the old send never fires, only the new one does", async () => {
  const sendA = vi.fn().mockResolvedValue(undefined);
  const sendB = vi.fn().mockResolvedValue(undefined);
  const { result } = setup();

  act(() => result.current.enqueue("k1", "Deny", sendA));
  await advance(3000);
  act(() => result.current.enqueue("k1", "Approve", sendB)); // replaces 3s in
  expect(result.current.pending).toEqual([{ key: "k1", label: "Approve" }]);

  await advance(2000); // t=5s from the start: the old timer, had it survived, would fire here
  expect(sendA).not.toHaveBeenCalled();
  expect(sendB).not.toHaveBeenCalled(); // only 2s since the replace; its own window is still open

  await advance(3000); // t=5s from the replace
  expect(sendB).toHaveBeenCalledTimes(1);
  expect(sendA).not.toHaveBeenCalled();
});

test("undo cancels the send and clears the row; nothing is ever sent", async () => {
  const send = vi.fn().mockResolvedValue(undefined);
  const { result } = setup();

  act(() => result.current.enqueue("k1", "Deny the request", send));
  const undoButton = screen.getByRole("button", { name: "Undo Deny the request" });
  fireEvent.click(undoButton);

  expect(result.current.pending).toEqual([]);
  expect(result.current.isPending("k1")).toBe(false);
  expect(screen.queryByRole("status")).toBeNull();

  await advance(UNDO_MS);
  expect(send).not.toHaveBeenCalled();
});

test("flushes every still-pending send on unmount, one after another in enqueue order; newest first until then", async () => {
  const order: string[] = [];
  const sendA = vi.fn(async () => {
    order.push("a");
  });
  const sendB = vi.fn(async () => {
    order.push("b");
  });
  const { result, unmount } = setup();

  act(() => result.current.enqueue("a", "Approve A", sendA));
  act(() => result.current.enqueue("b", "Approve B", sendB));
  expect(result.current.pending.map((row) => row.key)).toEqual(["b", "a"]); // newest first

  unmount();
  await settle();
  expect(sendA).toHaveBeenCalledTimes(1);
  expect(sendB).toHaveBeenCalledTimes(1);
  expect(order).toEqual(["a", "b"]); // enqueue order, not display order
});

test("six decisions flushed at close reach the sender strictly one after another, in order", async () => {
  const held = heldSends();
  const names = ["a", "b", "c", "d", "e", "f"];
  const { result, unmount } = setup();
  for (const name of names) {
    act(() => result.current.enqueue(name, `Approve ${name}`, held.send(name)));
  }

  unmount();
  for (let done = 0; done < names.length; done += 1) {
    await settle();
    // Only the next one has started: the rest wait until it settles, so no two are ever open.
    expect(held.started).toEqual(names.slice(0, done + 1));
    held.release(done);
  }
});

test("a flushed send that fails mid-line does not stop the ones behind it, and reaches onLateFailure", async () => {
  const onLateFailure = vi.fn();
  const error = Object.assign(new Error("locked"), { ownerText: "The vault is locked." });
  const order: string[] = [];
  const { result, unmount } = setup(UNDO_MS, { onLateFailure });

  act(() =>
    result.current.enqueue("a", "Approve A", async () => {
      order.push("a");
    }),
  );
  act(() =>
    result.current.enqueue("b", "Reject B", async () => {
      order.push("b");
      throw error;
    }),
  );
  act(() =>
    result.current.enqueue("c", "Approve C", async () => {
      order.push("c");
    }),
  );
  unmount();
  await settle();

  expect(order).toEqual(["a", "b", "c"]);
  expect(onLateFailure).toHaveBeenCalledExactlyOnceWith("Reject B", "The vault is locked.");
});

test("sends whose windows ended and the unmount flush wait in one line, in enqueue order", async () => {
  const held = heldSends();
  const { result, unmount } = setup();

  act(() => result.current.enqueue("a", "Approve A", held.send("a")));
  await advance(1000);
  act(() => result.current.enqueue("b", "Approve B", held.send("b")));
  await advance(UNDO_MS); // both windows have ended while mounted
  expect(held.started).toEqual(["a"]); // B waits behind A's send, still open
  expect(result.current.isPending("b")).toBe(true);

  act(() => result.current.enqueue("c", "Approve C", held.send("c")));
  unmount(); // the flush puts C behind B, not beside A
  await settle();
  expect(held.started).toEqual(["a"]);

  held.release(0);
  await settle();
  expect(held.started).toEqual(["a", "b"]);
  held.release(1);
  await settle();
  expect(held.started).toEqual(["a", "b", "c"]);
});

test("the unmount flush sends each decision exactly once, though its window would end later", async () => {
  const send = vi.fn().mockResolvedValue(undefined);
  const { result, unmount } = setup();

  act(() => result.current.enqueue("k1", "Approve", send));
  unmount();
  await settle();
  expect(send).toHaveBeenCalledTimes(1);

  await advance(UNDO_MS); // the flushed entry's own timer must be gone, not fire a second send
  expect(send).toHaveBeenCalledTimes(1);
});

test("a send the unmount flushed that then fails reaches onLateFailure, named as the bar names it", async () => {
  const onLateFailure = vi.fn();
  const error = Object.assign(new Error("full"), { ownerText: "No room for the draft." });
  const { result, unmount } = setup(UNDO_MS, { onLateFailure });

  act(() =>
    result.current.enqueue("k1", "Reject · notes/one.md", vi.fn().mockRejectedValue(error)),
  );
  act(() => result.current.enqueue("k2", "Approve · H1", vi.fn().mockResolvedValue(undefined)));
  unmount();
  await settle();

  // Only the failed one, with its own label and text; the one that went through says nothing.
  expect(onLateFailure).toHaveBeenCalledExactlyOnceWith(
    "Reject · notes/one.md",
    "No room for the draft.",
  );
});

test("a send still in flight at unmount that then fails reaches onLateFailure with the generic line", async () => {
  const onLateFailure = vi.fn();
  let fail: (error: unknown) => void = () => {};
  const send = vi.fn(
    () =>
      new Promise<void>((_resolve, reject) => {
        fail = reject;
      }),
  );
  const { result, unmount } = setup(UNDO_MS, { onLateFailure });

  act(() => result.current.enqueue("k1", "Archive the chat", send));
  await advance(UNDO_MS); // the window ended while mounted: the send is in flight
  expect(send).toHaveBeenCalledTimes(1);
  unmount();
  fail(new Error("boom"));
  await settle();

  expect(onLateFailure).toHaveBeenCalledExactlyOnceWith(
    "Archive the chat",
    "Nothing was recorded. Try again.",
  );
});

test("isPending stays true from expiry through the send settling, then clears", async () => {
  let resolveSend: () => void = () => {};
  const send = vi.fn(
    () =>
      new Promise<void>((resolve) => {
        resolveSend = resolve;
      }),
  );
  const { result } = setup();

  act(() => result.current.enqueue("k1", "Approve the change", send));
  expect(result.current.isPending("k1")).toBe(true); // queued, inside the undo window

  await advance(UNDO_MS);
  expect(send).toHaveBeenCalledTimes(1);
  expect(result.current.isPending("k1")).toBe(true); // in flight, not yet settled

  resolveSend();
  await settle();
  expect(result.current.isPending("k1")).toBe(false);
});

test("a rejected send names the decision and the error's ownerText", async () => {
  const error = Object.assign(new Error("locked"), { ownerText: "The vault is locked." });
  const send = vi.fn().mockRejectedValue(error);
  const { result } = setup();

  act(() => result.current.enqueue("k1", "Deny the request", send));
  await advance(UNDO_MS);
  await settle();

  const alert = screen.getByRole("alert");
  expect(alert.textContent).toBe("Could not finish “Deny the request”. The vault is locked.");
  expect(result.current.isPending("k1")).toBe(false);
});

test("when the first of two windows ends and its send rejects, the second still reaches the sender; the alert names the first", async () => {
  const error = Object.assign(new Error("locked"), { ownerText: "The vault is locked." });
  const sendA = vi.fn().mockRejectedValue(error);
  const sendB = vi.fn().mockResolvedValue(undefined);
  const { result } = setup();

  act(() => result.current.enqueue("a", "Deny A", sendA));
  await advance(1000);
  act(() => result.current.enqueue("b", "Approve B", sendB)); // b's window ends later, in turn

  await advance(4000); // t=5000: a's window ends; its send rejects
  await settle();
  expect(sendA).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("alert").textContent).toBe(
    "Could not finish “Deny A”. The vault is locked.",
  );

  await advance(1000); // t=6000: b's window ends; a's rejection must not have poisoned the line
  await settle();
  expect(sendB).toHaveBeenCalledTimes(1);

  // b went through cleanly; the alert still names a, the one that actually failed.
  expect(screen.getByRole("alert").textContent).toBe(
    "Could not finish “Deny A”. The vault is locked.",
  );
});

test("under StrictMode a rejected send still shows its alert, not a late failure", async () => {
  const onLateFailure = vi.fn();
  const error = Object.assign(new Error("locked"), { ownerText: "The vault is locked." });
  const { result } = setup(UNDO_MS, { onLateFailure, reactStrictMode: true });

  act(() => result.current.enqueue("k1", "Deny the request", vi.fn().mockRejectedValue(error)));
  await advance(UNDO_MS);
  await settle();

  expect(screen.getByRole("alert").textContent).toBe(
    "Could not finish “Deny the request”. The vault is locked.",
  );
  expect(onLateFailure).not.toHaveBeenCalled();
});

test("falls back to a generic line when the error has no ownerText", async () => {
  const send = vi.fn().mockRejectedValue(new Error("boom"));
  const { result } = setup();

  act(() => result.current.enqueue("k1", "Archive the chat", send));
  await advance(UNDO_MS);
  await settle();

  const alert = screen.getByRole("alert");
  expect(alert.textContent).toBe(
    "Could not finish “Archive the chat”. Nothing was recorded. Try again.",
  );
});

test("a new enqueue clears a previous failure line", async () => {
  const failing = vi.fn().mockRejectedValue(new Error("boom"));
  const { result } = setup();

  act(() => result.current.enqueue("k1", "Deny", failing));
  await advance(UNDO_MS);
  await settle();
  expect(screen.queryByRole("alert")).not.toBeNull();

  const send = vi.fn().mockResolvedValue(undefined);
  act(() => result.current.enqueue("k2", "Approve", send));
  expect(screen.queryByRole("alert")).toBeNull();
});

test("useDecisionQueue throws when rendered outside a provider", () => {
  const spy = vi.spyOn(console, "error").mockImplementation(() => {});
  expect(() => renderHook(() => useDecisionQueue())).toThrow(/DecisionQueueProvider/);
  spy.mockRestore();
});

function mountDialog(): { row1: HTMLElement; row2: HTMLElement; row3: HTMLElement } {
  document.body.innerHTML = `
    <div role="dialog">
      <div data-approval-row tabindex="-1" id="row1"></div>
      <div data-approval-row tabindex="-1" id="row2"></div>
      <div data-approval-row tabindex="-1" id="row3"></div>
    </div>`;
  return {
    row1: document.getElementById("row1") as HTMLElement,
    row2: document.getElementById("row2") as HTMLElement,
    row3: document.getElementById("row3") as HTMLElement,
  };
}

test("focusNextRow moves to the next row, and back with step -1", () => {
  const { row1, row2 } = mountDialog();
  row1.focus();
  expect(focusNextRow(row1)).toBe(true);
  expect(document.activeElement).toBe(row2);
  expect(focusNextRow(row2, -1)).toBe(true);
  expect(document.activeElement).toBe(row1);
});

test("focusNextRow returns false, and leaves focus alone, past the last row", () => {
  const { row3 } = mountDialog();
  row3.focus();
  expect(focusNextRow(row3)).toBe(false);
  expect(document.activeElement).toBe(row3);
});

test("focusNextRow returns false outside any [role=dialog]", () => {
  document.body.innerHTML = '<div data-approval-row tabindex="-1" id="lone"></div>';
  const lone = document.getElementById("lone") as HTMLElement;
  expect(focusNextRow(lone)).toBe(false);
});
