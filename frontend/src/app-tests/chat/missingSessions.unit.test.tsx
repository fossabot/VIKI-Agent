/**
 * App-level wiring for the missing-source rail: a pinned label this app still holds for a
 * session the current listing no longer has. sessionOrganization.unit.test.ts pins
 * `missingRecords` itself and MissingSessions.unit.test.tsx pins the component's own rendering;
 * this file pins only what App is responsible for supplying (the placement, the two actions,
 * and telling a prepared-but-never-started profile session apart from an actually-gone
 * transcript).
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { emptyRails, fakeRest, fakeViki, GENERATION, sessionRow } from "../../accept/m3b/fakes";
import type { Rest } from "../../api";
import type {
  SessionMetadata,
  SessionMetadataApi,
  SessionMetadataPatch,
} from "../../clients/conversations/sessionMetadataApi";
import {
  type PreparedProfilePreview,
  type ProfileLaunchApi,
  ProfileLaunchApiError,
} from "../../clients/profiles/profileLaunchApi";
import type { SessionsResult } from "../../protocol";
import { fakeDurable } from "../../testDurable";

afterEach(() => {
  cleanup();
  sessionStorage.clear();
});

async function flush(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 5; i++) await Promise.resolve();
  });
}

function preparedPreview(sessionId: string, started = false): PreparedProfilePreview {
  return {
    started,
    launchable: true,
    fields: [],
    blockers: [],
    warnings: [],
    prefs: { mode: "default", effort: "high", model: "sonnet" },
    session_id: sessionId,
    profile_id: "profile",
    profile_revision: 1,
    generation: null,
    workspace_id: "workspace",
    workspace_revision: 1,
    policy: "ordinary",
    policy_revision: 1,
    settings_revision: 1,
    runtime: "claude",
    capability_key: "a".repeat(64),
    draft: { key: sessionId, revision: 1, text: "", saved_at: null, attachments: [] },
  };
}

/** Answers `preparedPreview` for `sessionId`, 404 `not_found` for any other id. */
function fakeProfileLaunchApiFor(sessionId: string): ProfileLaunchApi {
  return {
    capabilities: vi.fn(),
    models: vi.fn(),
    prepare: vi.fn(),
    inspect: vi.fn(async (id) =>
      id === sessionId
        ? preparedPreview(sessionId)
        : Promise.reject(new ProfileLaunchApiError(404, "not_found")),
    ),
  };
}

/** Holds every `inspect` open until `settle` answers the oldest one: a profile session from
 * `started` (true or false by id), 404 `not_found` for any other id. */
function heldProfileLaunchApi(started: ReadonlyMap<string, boolean>): ProfileLaunchApi & {
  calls: string[];
  settle: () => void;
} {
  const calls: string[] = [];
  const answers: (() => void)[] = [];
  return {
    calls,
    capabilities: vi.fn(),
    models: vi.fn(),
    prepare: vi.fn(),
    inspect: (id) => {
      calls.push(id);
      return new Promise<PreparedProfilePreview>((resolve, reject) => {
        answers.push(() => {
          const value = started.get(id);
          if (value === undefined) reject(new ProfileLaunchApiError(404, "not_found"));
          else resolve(preparedPreview(id, value));
        });
      });
    },
    settle: () => answers.shift()?.(),
  };
}

function metadataRow(
  overrides: Partial<SessionMetadata> & { session_id: string },
): SessionMetadata {
  return { revision: 1, title: "", pinned: false, archived: false, ...overrides };
}

/** Resolves `list` immediately; tracks every `patch`/`remove` call for assertions. `order`, when
 * given, also gets each call, so a caller can compare its position against another fake's. */
function instantMetadataApi(
  rows: SessionMetadata[],
  order: string[] = [],
): SessionMetadataApi & {
  patchCalls: { sessionId: string; patch: SessionMetadataPatch }[];
  removeCalls: string[];
} {
  let current = rows;
  const patchCalls: { sessionId: string; patch: SessionMetadataPatch }[] = [];
  const removeCalls: string[] = [];
  return {
    patchCalls,
    removeCalls,
    async list() {
      return current;
    },
    async patch(sessionId, _expectedRevision, patch) {
      patchCalls.push({ sessionId, patch });
      const row = current.find((item) => item.session_id === sessionId);
      if (!row) throw new Error("missing row");
      const updated = { ...row, ...patch };
      current = current.map((item) => (item.session_id === sessionId ? updated : item));
      return updated;
    },
    async remove(sessionId) {
      order.push(`metadata.remove:${sessionId}`);
      removeCalls.push(sessionId);
      current = current.filter((item) => item.session_id !== sessionId);
      return true;
    },
  };
}

/** `/api/sessions` answers `partial` (one transcript omitted) until `turnReady()` is called,
 * then `ready`. */
function partialThenReadyRest(): Rest & { turnReady: () => void } {
  let ready = false;
  return {
    turnReady: () => {
      ready = true;
    },
    sessions: async () => ({
      sessions: [],
      state: ready ? "ready" : "partial",
      reason: ready ? "" : "time_budget",
      omitted: ready ? 0 : 1,
    }),
    search: async () => ({
      hits: [],
      state: "ready",
      reason: "",
      scanned: 0,
      total: 0,
      request_id: "",
    }),
    rails: async () => emptyRails(),
  };
}

test("a partial listing shows no record; the same listing turned ready shows it", async () => {
  vi.useFakeTimers();
  try {
    const metadata = instantMetadataApi([
      metadataRow({ session_id: "missing-2", title: "Partial title", pinned: true }),
    ]);
    const rest = partialThenReadyRest();
    const { viki, socket } = fakeViki();
    render(<App viki={viki} rest={rest} durable={fakeDurable()} organization={metadata} />);
    await act(async () => {
      socket.open();
    });
    await flush();
    // The label is already known; only the listing is partial.
    expect(screen.queryByText("Partial title")).toBeNull();

    rest.turnReady();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000); // the store's own poll interval
    });
    await flush();
    expect(screen.getByText("Partial title")).not.toBeNull();
  } finally {
    vi.useRealTimers();
  }
});

/** Holds every `sessions()` call open until `resolveCall`/`rejectCall` settles that call number
 * (1-based); each call gets its own resolver, so an earlier one can still be answered after a
 * later one has started. */
function holdingRest(): Rest & {
  resolveCall: (n: number, result: SessionsResult) => void;
  rejectCall: (n: number, error: unknown) => void;
} {
  let call = 0;
  const resolvers = new Map<number, (result: SessionsResult) => void>();
  const rejectors = new Map<number, (error: unknown) => void>();
  return {
    resolveCall: (n, result) => resolvers.get(n)?.(result),
    rejectCall: (n, error) => rejectors.get(n)?.(error),
    sessions: () => {
      call += 1;
      const n = call;
      return new Promise<SessionsResult>((resolve, reject) => {
        resolvers.set(n, resolve);
        rejectors.set(n, reject);
      });
    },
    search: async () => ({
      hits: [],
      state: "ready",
      reason: "",
      scanned: 0,
      total: 0,
      request_id: "",
    }),
    rails: async () => emptyRails(),
  };
}

test("a held first listing read shows no section, until it settles ready", async () => {
  const metadata = instantMetadataApi([
    metadataRow({ session_id: "missing-held", title: "Held title", pinned: true }),
  ]);
  const rest = holdingRest();
  const { viki, socket } = fakeViki();
  const { container } = render(
    <App viki={viki} rest={rest} durable={fakeDurable()} organization={metadata} />,
  );
  await act(async () => {
    socket.open();
  });
  await flush();
  // The label is already known; only the first listing read is still in flight.
  expect(container.querySelector(".missing-sessions")).toBeNull();

  await act(async () => {
    rest.resolveCall(1, { sessions: [], state: "ready", reason: "", omitted: 0 });
    await flush();
  });
  expect(screen.getByText("Held title")).not.toBeNull();
});

test("a shown section hides on a partial poll read, and stays hidden while the next read is in flight", async () => {
  vi.useFakeTimers();
  try {
    const metadata = instantMetadataApi([
      metadataRow({ session_id: "missing-partial", title: "Partial poll title", pinned: true }),
    ]);
    const rest = holdingRest();
    const { viki, socket } = fakeViki();
    const { container } = render(
      <App viki={viki} rest={rest} durable={fakeDurable()} organization={metadata} />,
    );
    await act(async () => {
      socket.open();
    });
    await flush();
    await act(async () => {
      rest.resolveCall(1, { sessions: [], state: "ready", reason: "", omitted: 0 });
      await flush();
    });
    expect(screen.getByText("Partial poll title")).not.toBeNull();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000); // the store's own poll interval, starts call 2
    });
    await act(async () => {
      rest.resolveCall(2, { sessions: [], state: "partial", reason: "time_budget", omitted: 1 });
      await flush();
    });
    expect(container.querySelector(".missing-sessions")).toBeNull();

    // The next poll's read (call 3) is in flight; the section stays hidden through it too.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(container.querySelector(".missing-sessions")).toBeNull();
  } finally {
    vi.useRealTimers();
  }
});

test("a shown section hides on a rejected poll read, and stays hidden while the next read is in flight", async () => {
  vi.useFakeTimers();
  try {
    const metadata = instantMetadataApi([
      metadataRow({ session_id: "missing-rejected", title: "Rejected poll title", pinned: true }),
    ]);
    const rest = holdingRest();
    const { viki, socket } = fakeViki();
    const { container } = render(
      <App viki={viki} rest={rest} durable={fakeDurable()} organization={metadata} />,
    );
    await act(async () => {
      socket.open();
    });
    await flush();
    await act(async () => {
      rest.resolveCall(1, { sessions: [], state: "ready", reason: "", omitted: 0 });
      await flush();
    });
    expect(screen.getByText("Rejected poll title")).not.toBeNull();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000); // the store's own poll interval, starts call 2
    });
    await act(async () => {
      rest.rejectCall(2, new Error("network"));
      await flush();
    });
    expect(container.querySelector(".missing-sessions")).toBeNull();

    // The next poll's read (call 3) is in flight; the section stays hidden through it too.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(container.querySelector(".missing-sessions")).toBeNull();
  } finally {
    vi.useRealTimers();
  }
});

test("Unpin patches the label", async () => {
  const metadata = instantMetadataApi([
    metadataRow({ session_id: "missing-1", title: "Pinned title", pinned: true }),
  ]);
  const { viki, socket } = fakeViki();
  render(<App viki={viki} rest={fakeRest()} durable={fakeDurable()} organization={metadata} />);
  await act(async () => {
    socket.open();
  });
  await flush();
  await act(async () => {
    fireEvent.click(await screen.findByRole("button", { name: "Unpin" }));
  });
  expect(metadata.patchCalls).toEqual([{ sessionId: "missing-1", patch: { pinned: false } }]);
});

test("a held session never shows", async () => {
  const metadata = instantMetadataApi([
    metadataRow({ session_id: "held-1", title: "Held convo", pinned: true }),
  ]);
  const { viki, socket } = fakeViki();
  render(<App viki={viki} rest={fakeRest()} durable={fakeDurable()} organization={metadata} />);
  await act(async () => {
    socket.open();
  });
  act(() => {
    viki.actions.openSession("held-1");
  });
  await flush();
  expect(screen.queryByText("Held convo")).toBeNull();
});

// Restores the coverage a deleted queue-only test once gave: a session this window never opened
// (so `heldIds` alone misses it) still never shows, because the vault's own `vault.sessions`
// report still names it live (App.tsx's `missingHeld`). A pinned label makes it a candidate in
// the label's place, since a queue can no longer do that on its own.
test("a session the vault still reports live never shows, even with a qualifying label", async () => {
  const metadata = instantMetadataApi([
    metadataRow({ session_id: "live-elsewhere-1", title: "Live elsewhere", pinned: true }),
  ]);
  const { viki, socket } = fakeViki();
  render(<App viki={viki} rest={fakeRest()} durable={fakeDurable()} organization={metadata} />);
  await act(async () => {
    socket.open();
  });
  await flush();
  act(() => {
    socket.receive({
      v: 2,
      generation: GENERATION,
      request_id: "",
      type: "vault.sessions",
      session_id: "",
      seq: 1,
      data: { others: [], live: ["live-elsewhere-1"] },
    });
  });
  await flush();
  expect(screen.queryByText("Live elsewhere")).toBeNull();
});

test("nothing in the section opens a session", async () => {
  const metadata = instantMetadataApi([
    metadataRow({ session_id: "missing-1", title: "Pinned title", pinned: true }),
  ]);
  const { viki, socket } = fakeViki();
  render(
    <App
      viki={viki}
      rest={fakeRest({ sessions: [sessionRow({ session_id: "other", title: "Other" })] })}
      durable={fakeDurable()}
      organization={metadata}
    />,
  );
  await act(async () => {
    socket.open();
  });
  await flush();
  const openSpy = vi.spyOn(viki.actions, "openSession");
  fireEvent.click(await screen.findByText("Pinned title"));
  expect(openSpy).not.toHaveBeenCalled();
});

test("a prepared, never-started profile session shows as a profile draft with Open and no Remove, and Open selects it", async () => {
  const metadata = instantMetadataApi([
    metadataRow({ session_id: "profile-draft-1", title: "Draft convo", pinned: true, revision: 1 }),
  ]);
  const { viki, socket } = fakeViki();
  const openSpy = vi.spyOn(viki.actions, "openSession");
  render(
    <App
      viki={viki}
      rest={fakeRest()}
      durable={fakeDurable()}
      organization={metadata}
      profileLaunchApi={fakeProfileLaunchApiFor("profile-draft-1")}
    />,
  );
  await act(async () => {
    socket.open();
  });
  await flush();
  await waitFor(() => expect(screen.getByText("Profile draft, not started")).not.toBeNull());
  expect(screen.queryByText("Source missing")).toBeNull();
  expect(screen.queryByRole("button", { name: "Remove" })).toBeNull();

  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
  });
  expect(openSpy).toHaveBeenCalledWith("profile-draft-1");
});

test("missing records are inspected one at a time, at most 8, and only a never-started profile draft opens", async () => {
  // Sorted by id: the two profile sessions first, then the gone records; the ninth is never read.
  const rows: [string, string][] = [
    ["a-draft", "Draft convo"],
    ["a-started", "Started convo"],
    ...Array.from({ length: 7 }, (_, i): [string, string] => [`m-${i}`, `Gone convo ${i}`]),
  ];
  const metadata = instantMetadataApi(
    rows.map(([session_id, title]) => metadataRow({ session_id, title })),
  );
  const launch = heldProfileLaunchApi(
    new Map([
      ["a-draft", false],
      ["a-started", true],
    ]),
  );
  const { viki, socket } = fakeViki();
  const { container } = render(
    <App
      viki={viki}
      rest={fakeRest()}
      durable={fakeDurable()}
      organization={metadata}
      profileLaunchApi={launch}
    />,
  );
  await act(async () => {
    socket.open();
  });
  await waitFor(() => expect(launch.calls).toHaveLength(1));
  for (let settled = 1; settled <= 8; settled++) {
    // The next inspection starts only once the previous one has settled.
    expect(launch.calls).toHaveLength(settled);
    await act(async () => {
      launch.settle();
    });
    await flush();
  }
  expect(launch.calls).toEqual(rows.slice(0, 8).map(([id]) => id));

  const row = (title: string) => within(screen.getByText(title).closest("li") as HTMLElement);
  expect(row("Draft convo").getByText("Profile draft, not started")).not.toBeNull();
  for (const title of ["Started convo", "Gone convo 0", "Gone convo 6"]) {
    expect(row(title).getByText("Source missing")).not.toBeNull();
    expect(row(title).queryByRole("button", { name: "Open" })).toBeNull();
  }

  await act(async () => {
    fireEvent.click(row("Draft convo").getByRole("button", { name: "Open" }));
  });
  await flush();
  expect(container.querySelector(".conversation-title")?.textContent).toBe("Draft convo");
});
