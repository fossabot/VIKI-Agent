/**
 * usePresence: posts the exact `PresenceBody` on change (debounced 1 s) and every 15 s while
 * visible, holds `typing` for 3 s after a document `input` event, posts nothing while hidden
 * (one post right away on becoming visible), keeps at most one request in flight, and swallows
 * failures quietly.
 */
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import type { DecisionLoopApi, PageFacts, PresenceBody } from "../../clients/decisionLoop/types";
import { type UsePresenceOptions, usePresence } from "./usePresence";

const WINDOW_ID = "11111111-1111-4111-8111-111111111111";

function options(overrides: Partial<UsePresenceOptions> = {}): UsePresenceOptions {
  return {
    window: WINDOW_ID,
    focused: true,
    chat: null,
    chatTitle: null,
    note: null,
    launcher: false,
    page: null,
    ...overrides,
  };
}

/** A `presence` that resolves at once and records every call's body. */
function fakeApi(): { api: Pick<DecisionLoopApi, "presence">; calls: PresenceBody[] } {
  const calls: PresenceBody[] = [];
  const api: Pick<DecisionLoopApi, "presence"> = {
    presence: vi.fn(async (body: PresenceBody) => {
      calls.push(body);
    }),
  };
  return { api, calls };
}

/** A `presence` whose promises stay pending until `resolveOldest` is called, oldest first. */
function controlledApi(): {
  api: Pick<DecisionLoopApi, "presence">;
  calls: PresenceBody[];
  resolveOldest: () => void;
} {
  const calls: PresenceBody[] = [];
  const resolvers: Array<() => void> = [];
  const api: Pick<DecisionLoopApi, "presence"> = {
    presence: vi.fn((body: PresenceBody) => {
      calls.push(body);
      return new Promise<void>((resolve) => {
        resolvers.push(resolve);
      });
    }),
  };
  return {
    api,
    calls,
    resolveOldest(): void {
      resolvers.shift()?.();
    },
  };
}

let hidden = false;

function setHidden(value: boolean): void {
  hidden = value;
  act(() => {
    document.dispatchEvent(new Event("visibilitychange"));
  });
}

function typeSomewhere(): void {
  act(() => {
    document.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  hidden = false;
  vi.useFakeTimers();
  vi.spyOn(document, "visibilityState", "get").mockImplementation(() =>
    hidden ? "hidden" : "visible",
  );
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

// ---- the debounced first post and later changes -------------------------------------------

test("posts once, 1 s after mount, with the initial facts", async () => {
  const { api, calls } = fakeApi();
  renderHook(() => usePresence(api, options({ focused: true, launcher: true })));
  await advance(999);
  expect(calls.length).toBe(0);
  await advance(1);
  expect(calls.length).toBe(1);
  expect(calls[0]).toEqual({
    window: WINDOW_ID,
    focused: true,
    typing: false,
    chat: null,
    chat_title: null,
    note: null,
    launcher: true,
    page: null,
  });
});

test("a prop change re-debounces and posts once more with the latest value", async () => {
  const { api, calls } = fakeApi();
  const { rerender } = renderHook((props: UsePresenceOptions) => usePresence(api, props), {
    initialProps: options(),
  });
  await advance(1000);
  expect(calls.length).toBe(1);

  rerender(options({ focused: false }));
  await advance(999);
  expect(calls.length).toBe(1);
  await advance(1);
  expect(calls.length).toBe(2);
  expect(calls[1]?.focused).toBe(false);
});

test("rapid changes within the debounce window collapse into one post, with the latest value", async () => {
  const { api, calls } = fakeApi();
  const { rerender } = renderHook((props: UsePresenceOptions) => usePresence(api, props), {
    initialProps: options(),
  });
  await advance(1000);
  expect(calls.length).toBe(1);

  rerender(options({ launcher: true }));
  await advance(500);
  rerender(options({ launcher: true, focused: false }));
  await advance(500); // 1 s since the first change, but only 500 ms since the second
  expect(calls.length).toBe(1);
  await advance(500); // 1 s since the second change
  expect(calls.length).toBe(2);
  expect(calls[1]).toMatchObject({ launcher: true, focused: false });
});

test("an unrelated re-render with equal values never restarts the debounce", async () => {
  const { api, calls } = fakeApi();
  const { rerender } = renderHook((props: UsePresenceOptions) => usePresence(api, props), {
    initialProps: options(),
  });
  await advance(1000);
  expect(calls.length).toBe(1);

  // A fresh object with the same values: compared by value, not by reference.
  rerender(options());
  await advance(1000);
  expect(calls.length).toBe(1);
});

// ---- the 15 s heartbeat ----------------------------------------------------------------------

test("posts again on the 15 s heartbeat while visible, even with no change", async () => {
  const { api, calls } = fakeApi();
  renderHook(() => usePresence(api, options()));
  await advance(1000);
  expect(calls.length).toBe(1);
  await advance(14_000); // total 15_000 since mount
  expect(calls.length).toBe(2);
  await advance(15_000); // total 30_000
  expect(calls.length).toBe(3);
});

// ---- hidden then visible ---------------------------------------------------------------------

test("posts nothing while hidden, then once right away on becoming visible", async () => {
  const { api, calls } = fakeApi();
  renderHook(() => usePresence(api, options()));
  await advance(1000);
  expect(calls.length).toBe(1);

  setHidden(true);
  await advance(15_000); // a full heartbeat interval, skipped while hidden
  expect(calls.length).toBe(1);

  setHidden(false);
  expect(calls.length).toBe(2); // immediate; no timer advance needed

  await advance(15_000);
  expect(calls.length).toBe(3); // the heartbeat resumes on its own schedule
});

// ---- the typing window -----------------------------------------------------------------------

test("typing is true within 3 s of an input event, then false again", async () => {
  const { api, calls } = fakeApi();
  renderHook(() => usePresence(api, options()));
  await advance(1000);
  expect(calls.length).toBe(1);
  expect(calls[0]?.typing).toBe(false);

  typeSomewhere();
  await advance(999);
  expect(calls.length).toBe(1); // the typing:true change is still debouncing
  await advance(1);
  expect(calls.length).toBe(2);
  expect(calls[1]?.typing).toBe(true);

  await advance(1999); // typing's 3 s hold (from the input) ends at +3000, not yet
  expect(calls.length).toBe(2);
  await advance(1); // the hold ends: typing flips to false, debouncing starts
  expect(calls.length).toBe(2);
  await advance(999);
  expect(calls.length).toBe(2);
  await advance(1); // 1 s after the flip
  expect(calls.length).toBe(3);
  expect(calls[2]?.typing).toBe(false);
});

test("typing restarts its 3 s hold on each new input event", async () => {
  const { api, calls } = fakeApi();
  renderHook(() => usePresence(api, options()));
  await advance(1000);
  typeSomewhere(); // input #1
  await advance(2000); // 2 s later: still within input #1's 3 s hold (ends at +3 s)
  typeSomewhere(); // input #2 restarts the hold for another 3 s

  await advance(1500); // 1.5 s after input #2: past input #1's original deadline, but held
  expect(calls.at(-1)?.typing).toBe(true);

  // The hold's end (a state change from a bare timer callback, not a DOM event) only commits
  // once its own `act()` returns, so it needs its own `advance` call to land exactly on it
  // before the debounce it then schedules gets a window to fire in.
  await advance(1500); // input #2's restarted 3 s hold ends here
  await advance(1000); // the debounce that schedules fires here
  expect(calls.at(-1)?.typing).toBe(false);
});

test("returns lastInputMs, null before the first input and updated on each one", () => {
  const { api } = fakeApi();
  vi.setSystemTime(new Date("2026-10-22T00:00:00.000Z"));
  const { result } = renderHook(() => usePresence(api, options()));
  expect(result.current.lastInputMs).toBeNull();

  typeSomewhere();
  expect(result.current.lastInputMs).toBe(Date.now());
});

// ---- the in-flight guard ----------------------------------------------------------------------

test("a slow answer delays the next post, and only the latest body stays queued behind it", async () => {
  const { api, calls, resolveOldest } = controlledApi();
  const { rerender } = renderHook((props: UsePresenceOptions) => usePresence(api, props), {
    initialProps: options({ focused: true }),
  });
  await advance(1000);
  expect(calls.length).toBe(1); // in flight, unresolved
  expect(calls[0]).toMatchObject({ focused: true, launcher: false });

  rerender(options({ focused: false }));
  await advance(1000); // this change's debounce fires while the first answer is still out
  expect(calls.length).toBe(1); // queued behind it, not sent

  rerender(options({ focused: false, launcher: true }));
  await advance(1000); // overwrites the queued body; still just one network call so far
  expect(calls.length).toBe(1);

  await act(async () => {
    resolveOldest();
    await vi.advanceTimersByTimeAsync(0); // flush the settled request's `.finally()`
  });
  expect(calls.length).toBe(2); // the delayed next post goes out, not stacked
  expect(calls[1]).toMatchObject({ focused: false, launcher: true }); // the latest, not the middle one
});

test("a rejected post is swallowed quietly, and the next cycle still runs", async () => {
  const calls: PresenceBody[] = [];
  const presence = vi.fn((body: PresenceBody) => {
    calls.push(body);
    return calls.length === 1 ? Promise.reject(new Error("boom")) : Promise.resolve();
  });
  const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
  renderHook(() => usePresence({ presence }, options()));
  await advance(1000);
  expect(calls.length).toBe(1);
  await advance(14_000); // the next heartbeat, after the rejection settled quietly
  expect(calls.length).toBe(2);
  expect(errorSpy).not.toHaveBeenCalled();
});

// ---- the exact wire body: trimming and the null fallbacks -------------------------------------

describe("the exact wire body", () => {
  test("strips control characters from chat_title without otherwise changing it", async () => {
    const { api, calls } = fakeApi();
    renderHook(() => usePresence(api, options({ chatTitle: "Plan\u0007 the\tlaunch" })));
    await advance(1000);
    expect(calls[0]?.chat_title).toBe("Plan thelaunch");
  });

  test("clips chat_title to 120 characters, keeping the first 120", async () => {
    const { api, calls } = fakeApi();
    const long = "x".repeat(130);
    renderHook(() => usePresence(api, options({ chatTitle: long })));
    await advance(1000);
    expect(calls[0]?.chat_title).toBe("x".repeat(120));
  });

  test("keeps a valid surrogate pair (an emoji) intact", async () => {
    const { api, calls } = fakeApi();
    const title = "Hi \u{1F600}";
    renderHook(() => usePresence(api, options({ chatTitle: title })));
    await advance(1000);
    expect(calls[0]?.chat_title).toBe(title);
  });

  test("passes a null chat_title through as null", async () => {
    const { api, calls } = fakeApi();
    renderHook(() => usePresence(api, options({ chatTitle: null })));
    await advance(1000);
    expect(calls[0]?.chat_title).toBeNull();
  });

  test("sends chat verbatim when it is a canonical UUID", async () => {
    const { api, calls } = fakeApi();
    const uuid = "22222222-2222-4222-8222-222222222222";
    renderHook(() => usePresence(api, options({ chat: uuid })));
    await advance(1000);
    expect(calls[0]?.chat).toBe(uuid);
  });

  test("falls back chat to null when it is not a canonical UUID", async () => {
    const { api, calls } = fakeApi();
    renderHook(() => usePresence(api, options({ chat: "not-a-uuid" })));
    await advance(1000);
    expect(calls[0]?.chat).toBeNull();
  });

  test("falls back the whole note to null when workspaceId is not a canonical UUID", async () => {
    const { api, calls } = fakeApi();
    renderHook(() =>
      usePresence(api, options({ note: { workspaceId: "nope", ref: "30-knowledge/x.md" } })),
    );
    await advance(1000);
    expect(calls[0]?.note).toBeNull();
  });

  test("falls back the whole note to null when ref is over 512 characters", async () => {
    const { api, calls } = fakeApi();
    const uuid = "33333333-3333-4333-8333-333333333333";
    renderHook(() =>
      usePresence(api, options({ note: { workspaceId: uuid, ref: "x".repeat(513) } })),
    );
    await advance(1000);
    expect(calls[0]?.note).toBeNull();
  });

  test("falls back the whole note to null when ref is empty", async () => {
    const { api, calls } = fakeApi();
    const uuid = "44444444-4444-4444-8444-444444444444";
    renderHook(() => usePresence(api, options({ note: { workspaceId: uuid, ref: "" } })));
    await advance(1000);
    expect(calls[0]?.note).toBeNull();
  });

  test("sends a valid note as workspace_id and ref", async () => {
    const { api, calls } = fakeApi();
    const uuid = "55555555-5555-4555-8555-555555555555";
    renderHook(() =>
      usePresence(api, options({ note: { workspaceId: uuid, ref: "30-knowledge/x.md" } })),
    );
    await advance(1000);
    expect(calls[0]?.note).toEqual({ workspace_id: uuid, ref: "30-knowledge/x.md" });
  });

  test("sends a note whose ref is exactly 512 characters, the server's own limit", async () => {
    const { api, calls } = fakeApi();
    const uuid = "77777777-7777-4777-8777-777777777777";
    const ref = "x".repeat(512);
    renderHook(() => usePresence(api, options({ note: { workspaceId: uuid, ref } })));
    await advance(1000);
    expect(calls[0]?.note).toEqual({ workspace_id: uuid, ref });
  });

  test("passes page through exactly", async () => {
    const { api, calls } = fakeApi();
    const page: PageFacts = {
      native_waiting: 2,
      runs: { working: 1, review: 0 },
      git: { state: "known", dirty: 3 },
      routine: {
        routine_id: "66666666-6666-4666-8666-666666666666",
        next_run: "2026-10-22T00:00:00.000Z",
        paused: false,
      },
      skill: null,
      suggest: null,
      second_look: null,
      context: null,
      sources: null,
    };
    renderHook(() => usePresence(api, options({ page })));
    await advance(1000);
    expect(calls[0]?.page).toEqual(page);
  });

  describe("second look and the body cap", () => {
    const SESSION = "22222222-2222-4222-8222-222222222222";
    const TURN = "33333333-3333-4333-8333-333333333333";
    const basePage: PageFacts = {
      native_waiting: 0,
      runs: { working: 0, review: 0 },
      git: { state: "unknown", dirty: null },
      routine: null,
      skill: null,
      suggest: null,
      second_look: null,
      context: null,
      sources: null,
    };
    function pageWith(refs: string[], over: Partial<PageFacts> = {}): PageFacts {
      return {
        ...basePage,
        ...over,
        second_look: { session: SESSION, turn: TURN, trigger: "correction", title: "t", refs },
      };
    }
    const encoded = (body: PresenceBody) => new TextEncoder().encode(JSON.stringify(body)).length;

    test("keeps a worst-case second look whole while the body fits 4,096 bytes", async () => {
      const { api, calls } = fakeApi();
      const refs = Array.from({ length: 10 }, (_, i) => `${"x".repeat(97)}-${i}.md`);
      const page = pageWith(refs);
      if (page.second_look) page.second_look.title = "\u3042".repeat(120);
      renderHook(() => usePresence(api, options({ page, chatTitle: "\u3042".repeat(120) })));
      await advance(1000);
      expect(calls[0]?.page?.second_look?.refs).toEqual(refs);
      expect(encoded(calls[0] as PresenceBody)).toBeLessThanOrEqual(4_096);
    });

    test("drops refs from the end rather than pass the cap", async () => {
      const { api, calls } = fakeApi();
      const refs = ["a", "b", "c"].map((c) => `${c.repeat(200)}.md`);
      const padded = (n: number, count: number) =>
        pageWith(refs.slice(0, count), {
          skill: { trigger: "long_session", sessions: ["s"], titles: ["x".repeat(n)] },
        });
      const bodyOf = (page: PageFacts): PresenceBody => ({
        window: WINDOW_ID,
        focused: true,
        typing: false,
        chat: null,
        chat_title: null,
        note: null,
        launcher: false,
        page,
      });
      // Pad so that two refs fit with 5 spare bytes and the third does not.
      const pad = 4_096 - encoded(bodyOf(padded(0, 2))) - 5;
      renderHook(() => usePresence(api, options({ page: padded(pad, 3) })));
      await advance(1000);
      expect(calls[0]?.page?.second_look?.refs).toEqual(refs.slice(0, 2));
      expect(encoded(calls[0] as PresenceBody)).toBeLessThanOrEqual(4_096);
      expect(encoded(bodyOf(padded(pad, 3)))).toBeGreaterThan(4_096);
    });

    test("sends the block as null, not refused, when no ref fits", async () => {
      const { api, calls } = fakeApi();
      const page = pageWith(["a.md"], {
        skill: { trigger: "long_session", sessions: ["s"], titles: ["x".repeat(4_000)] },
      });
      renderHook(() => usePresence(api, options({ page })));
      await advance(1000);
      expect(calls[0]?.page?.second_look).toBeNull();
      expect(calls[0]?.page?.skill?.titles[0]).toHaveLength(4_000);
    });

    test("drops a second look whose chat or turn id is not a canonical UUID", async () => {
      const { api, calls } = fakeApi();
      const page = pageWith(["a.md"]);
      if (page.second_look) page.second_look.turn = "local-1";
      renderHook(() => usePresence(api, options({ page })));
      await advance(1000);
      expect(calls[0]?.page?.second_look).toBeNull();
    });

    describe("context and sources", () => {
      const parts: Partial<PageFacts> = {
        context: { session: SESSION, percent: 75 },
        sources: { session: SESSION, turn: TURN, notes: false },
      };
      const withPad = (n: number, over: Partial<PageFacts> = parts): PageFacts => ({
        ...basePage,
        ...over,
        skill: { trigger: "long_session", sessions: ["s"], titles: ["x".repeat(n)] },
      });
      const bodyOf = (page: PageFacts): PresenceBody => ({
        window: WINDOW_ID,
        focused: true,
        typing: false,
        chat: null,
        chat_title: null,
        note: null,
        launcher: false,
        page,
      });
      /** Pad so the body with `fits` parts is `spare` bytes under the cap. */
      const padFor = (fits: Partial<PageFacts>, spare: number) =>
        4_096 - encoded(bodyOf(withPad(0, fits))) - spare;

      test("a body at 4,000 bytes with both parts drops sources only, then both", async () => {
        const { api, calls } = fakeApi();
        // `context` fits with 5 spare bytes; `sources` on top of it does not.
        const pad = padFor({ context: parts.context ?? null }, 5);
        expect(encoded(bodyOf(withPad(pad)))).toBeGreaterThan(4_096);
        renderHook(() => usePresence(api, options({ page: withPad(pad) })));
        await advance(1000);
        expect(calls[0]?.page?.sources).toBeNull();
        expect(calls[0]?.page?.context).toEqual(parts.context);
        expect(encoded(calls[0] as PresenceBody)).toBeLessThanOrEqual(4_096);

        // Neither part fits: both go.
        const tight = padFor({}, 5);
        renderHook(() => usePresence(api, options({ page: withPad(tight) })));
        await advance(1000);
        expect(calls[1]?.page?.sources).toBeNull();
        expect(calls[1]?.page?.context).toBeNull();
        expect(encoded(calls[1] as PresenceBody)).toBeLessThanOrEqual(4_096);
      });

      test("keeps both parts while the body fits", async () => {
        const { api, calls } = fakeApi();
        renderHook(() => usePresence(api, options({ page: withPad(10) })));
        await advance(1000);
        expect(calls[0]?.page?.context).toEqual(parts.context);
        expect(calls[0]?.page?.sources).toEqual(parts.sources);
      });

      test("drops a part whose chat or turn id is not a canonical UUID", async () => {
        const { api, calls } = fakeApi();
        const page = withPad(10, {
          context: { session: "local-1", percent: 75 },
          sources: { session: SESSION, turn: "local-2", notes: true },
        });
        renderHook(() => usePresence(api, options({ page })));
        await advance(1000);
        expect(calls[0]?.page?.context).toBeNull();
        expect(calls[0]?.page?.sources).toBeNull();
      });

      test("the second look's refs keep what they had without the parts", async () => {
        const refs = ["a", "b", "c"].map((c) => `${c.repeat(200)}.md`);
        const look = (count: number, over: Partial<PageFacts>) =>
          pageWith(refs.slice(0, count), {
            skill: { trigger: "long_session", sessions: ["s"], titles: [""] },
            ...over,
          });
        const bare = look(3, {});
        const lone = (page: PageFacts) => ({ ...page, ...parts });
        // Pad so two refs fit without the parts and the third does not.
        const pad = 4_096 - encoded(bodyOf(look(2, {}))) - 5;
        const padded = (page: PageFacts): PageFacts => ({
          ...page,
          skill: { trigger: "long_session", sessions: ["s"], titles: ["x".repeat(pad)] },
        });
        const { api, calls } = fakeApi();
        renderHook(() => usePresence(api, options({ page: padded(bare) })));
        await advance(1000);
        renderHook(() => usePresence(api, options({ page: padded(lone(bare)) })));
        await advance(1000);
        expect(calls[0]?.page?.second_look?.refs).toEqual(refs.slice(0, 2));
        expect(calls[1]?.page?.second_look?.refs).toEqual(refs.slice(0, 2));
        expect(calls[1]?.page?.context).toBeNull();
        expect(calls[1]?.page?.sources).toBeNull();
      });
    });
  });

  test("passes a null page through as null", async () => {
    const { api, calls } = fakeApi();
    renderHook(() => usePresence(api, options({ page: null })));
    await advance(1000);
    expect(calls[0]?.page).toBeNull();
  });
});

// ---- unmount cleanup --------------------------------------------------------------------------

test("removes its input and visibilitychange listeners, and its timers, on unmount", async () => {
  const { api, calls } = fakeApi();
  const addSpy = vi.spyOn(document, "addEventListener");
  const removeSpy = vi.spyOn(document, "removeEventListener");
  const { unmount } = renderHook(() => usePresence(api, options()));
  await advance(1000);
  expect(calls.length).toBe(1);

  const addedInput = addSpy.mock.calls.find(([name]) => name === "input")?.[1];
  const addedVisibility = addSpy.mock.calls.find(([name]) => name === "visibilitychange")?.[1];
  expect(addedInput).toBeDefined();
  expect(addedVisibility).toBeDefined();

  unmount();
  expect(removeSpy).toHaveBeenCalledWith("input", addedInput);
  expect(removeSpy).toHaveBeenCalledWith("visibilitychange", addedVisibility);

  // The cleared debounce and heartbeat timers, and the removed listeners, mean nothing more is
  // ever posted after unmount.
  typeSomewhere();
  setHidden(true);
  setHidden(false);
  await advance(30_000);
  expect(calls.length).toBe(1);
});
