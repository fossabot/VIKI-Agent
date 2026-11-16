/**
 * App-level wiring for the chip before a chat's first `session.started`. prestartChip.unit.test.tsx
 * pins the hook itself; this file proves what only a full App render can: App feeds it the chat's
 * key and binding, a new chat's pick is written before that chat's first `session.open`, a failed
 * write stops the send, and an opened chat with no live process picks over the socket controls,
 * then reads again.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { FakeSocket, fakeRest, GENERATION, sessionRow } from "../../accept/m3b/fakes";
import { ModelConnectionsContext } from "../../clients/app/modelConnectionsApi";
import type { ChatPrefs, SessionPrefsApi } from "../../clients/conversations/sessionPrefsApi";
import {
  type PreparedProfilePreview,
  type ProfileLaunchApi,
  ProfileLaunchApiError,
  type ProviderModelView,
} from "../../clients/profiles/profileLaunchApi";
import type { Envelope, EventMap, ModelOption } from "../../protocol";
import { createViki } from "../../state/viki";
import { fakeDurable } from "../../testDurable";
import { capabilitiesFixture, fakeShellApi, settingsFixture } from "../../testShell";

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
});

/** App.tsx's own sentence, quoted rather than imported: the module keeps it private. */
const PREFS_NOT_SAVED =
  "Couldn't save this chat's model and effort. Check the chip, then send again.";
const DEFAULTS: ChatPrefs = { model: null, effort: "high" };
const ROW = "33333333-3333-4333-8333-333333333333";

/** The provider's last observed list, as `GET /api/v2/providers/claude/models` answers it. */
const CATALOG: ProviderModelView = {
  provider: "claude",
  adapter_version: "viki-claude-1",
  native_version: "2.1.281",
  observed_at: null,
  catalog_key: null,
  status: "supported",
  models: [
    { value: "default", label: "Default (recommended)", description: "Use the default model" },
    { value: "sonnet", label: "Sonnet", description: "Efficient for routine tasks" },
    { value: "opus", label: "Opus", description: "Most capable" },
  ],
};

/** What a started process reports, labelled apart from CATALOG so a label proves its source. */
const LIVE_MODELS: ModelOption[] = [
  { value: "sonnet", label: "Sonnet 5", description: "Efficient for routine tasks" },
];

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

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

function started(sessionId: string, seq: number): Envelope {
  return frame(sessionId, seq, "session.started", {
    model: "sonnet",
    mode: "auto",
    effort: "max",
    commands: [],
    agents: [],
    models: LIVE_MODELS,
    cli_version: "2.1.281",
    resumed: false,
  });
}

const preview = (sessionId: string): PreparedProfilePreview => ({
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
  draft: { key: sessionId, revision: 1, text: "instructions", saved_at: null, attachments: [] },
  identity: {
    name: "Planner",
    avatar_asset_id: null,
    accent: "orange",
    shape: "circle",
    profile_revision: 1,
  },
});

function notFound(): Promise<never> {
  return Promise.reject(new ProfileLaunchApiError(404, "not_found"));
}

/** App with both clients; every chat reads DEFAULTS until a test answers otherwise. */
function renderApp({
  inspect = notFound,
  sessions = [],
  customDefault = false,
  billingBlocked = false,
  manual = false,
}: {
  inspect?: ProfileLaunchApi["inspect"];
  sessions?: string[];
  customDefault?: boolean;
  billingBlocked?: boolean;
  manual?: boolean;
} = {}) {
  const ref = "viki-llm/11111111-1111-4111-8111-111111111111/example/model";
  const get = vi.fn<SessionPrefsApi["get"]>(async () =>
    customDefault ? { model: ref, effort: "high" } : DEFAULTS,
  );
  const save = vi.fn<SessionPrefsApi["save"]>();
  const models = vi.fn<ProfileLaunchApi["models"]>(async () =>
    customDefault ? { ...CATALOG, models: [] } : CATALOG,
  );
  const durable = fakeDurable();
  const socket = new FakeSocket(!manual);
  const viki = createViki({
    url: "ws://synthetic",
    token: "synthetic",
    createSocket: () => socket,
  });
  viki.client.connect();
  const { container } = render(
    <ModelConnectionsContext.Provider
      value={
        customDefault
          ? {
              list: async () => ({
                revision: 1,
                default_model: ref,
                connections: [
                  {
                    id: "11111111-1111-4111-8111-111111111111",
                    model_ref: ref,
                    name: "My connection",
                    model: "example/model",
                    base_url: "https://models.example/v1",
                    api_mode: "responses",
                    has_api_key: true,
                    is_default: true,
                  },
                ],
              }),
              save: vi.fn(),
              remove: vi.fn(),
              test: vi.fn(),
              discover: vi.fn(),
              updateKey: vi.fn(),
            }
          : null
      }
    >
      <App
        viki={viki}
        rest={fakeRest({
          sessions: sessions.map((session_id) => sessionRow({ session_id, title: session_id })),
        })}
        durable={durable}
        {...(billingBlocked
          ? {
              shell: fakeShellApi({
                settings: settingsFixture({ "onboarding.completed": true }),
                capabilities: capabilitiesFixture({
                  provider: { billing: "blocked", can_start: false, problems: [] },
                }),
              }),
            }
          : {})}
        profileLaunchApi={{ capabilities: vi.fn(), models, inspect, prepare: vi.fn() }}
        sessionPrefsApi={{ get, save }}
      />
    </ModelConnectionsContext.Provider>,
  );
  act(() => socket.open());
  return { container, viki, socket, durable, get, save, models };
}

function chipText(container: HTMLElement): string | null {
  return container.querySelector(".model-chip")?.textContent ?? null;
}

function pick(container: HTMLElement, label: string): void {
  fireEvent.click(container.querySelector(".model-chip") as HTMLElement);
  fireEvent.click(within(screen.getByRole("menu")).getByText(label));
}

function opens(socket: FakeSocket, sessionId?: string): Record<string, unknown>[] {
  return socket.sent.filter(
    (sent) =>
      sent.type === "session.open" && (sessionId === undefined || sent.session_id === sessionId),
  );
}

function composerBox(container: HTMLElement): HTMLTextAreaElement {
  const box = container.querySelector<HTMLTextAreaElement>('[data-region="conversation"] textarea');
  if (!box) throw new Error("no composer");
  return box;
}

function typeAndSend(container: HTMLElement, text: string): void {
  fireEvent.change(composerBox(container), { target: { value: text } });
  fireEvent.keyDown(composerBox(container), { key: "Enter" });
}

test.each([false, true])(
  "first Send carries the displayed raw model rather than a later default (%s)",
  async (customDefault) => {
    const { container, viki, socket, get, save, durable } = renderApp({
      customDefault,
      manual: true,
    });
    try {
      await act(async () => Promise.resolve());
      const key = get.mock.calls[0]?.[0] as string;
      await waitFor(() => expect(chipText(container)).not.toBeNull(), { timeout: 50 });
      const expected = customDefault
        ? "viki-llm/11111111-1111-4111-8111-111111111111/example/model"
        : null;
      get.mockResolvedValue({ model: "later-default", effort: "high" });
      typeAndSend(container, "keep this prompt");
      expect(opens(socket, key)[0]).toHaveProperty("expected_model", expected);
      expect(save).not.toHaveBeenCalled();
      expect(durable.submissions).toEqual([]); // no generation/barrier, so no durable first prompt
      act(() => socket.synchronize(key));
      await waitFor(() => expect(durable.submissions).toHaveLength(1), { timeout: 50 });
    } finally {
      await act(async () => viki.client.close());
    }
  },
);

test("an effort-only save cannot replace the model snapshot captured before its reply", async () => {
  const { container, viki, socket, get, save } = renderApp({ customDefault: true, manual: true });
  try {
    await act(async () => Promise.resolve());
    await waitFor(() => expect(chipText(container)).not.toBeNull(), { timeout: 50 });
    const key = get.mock.calls[0]?.[0] as string;
    const written = deferred<ChatPrefs>();
    save.mockReturnValue(written.promise);
    pick(container, "max");
    typeAndSend(container, "first prompt");
    await act(async () => Promise.resolve());
    expect(opens(socket)).toEqual([]);
    await act(async () => written.resolve({ model: "later-default", effort: "max" }));
    expect(save).toHaveBeenCalledExactlyOnceWith(key, { effort: "max" });
    expect(opens(socket, key)[0]).toHaveProperty(
      "expected_model",
      "viki-llm/11111111-1111-4111-8111-111111111111/example/model",
    );
  } finally {
    await act(async () => viki.client.close());
  }
});

test("a later picker change does not retarget a first Send already waiting on its preference save", async () => {
  const { container, viki, socket, save, get } = renderApp({ manual: true });
  try {
    await act(async () => Promise.resolve());
    await waitFor(() => expect(chipText(container)).not.toBeNull(), { timeout: 50 });
    const key = get.mock.calls[0]?.[0] as string;
    const written = deferred<ChatPrefs>();
    save.mockReturnValue(written.promise);
    pick(container, "Sonnet");
    typeAndSend(container, "first prompt");
    await act(async () => Promise.resolve());
    pick(container, "Opus");
    await act(async () => written.resolve({ model: "sonnet", effort: "high" }));
    expect(opens(socket, key)[0]).toHaveProperty("expected_model", "sonnet");
  } finally {
    await act(async () => viki.client.close());
  }
});

test("model_review_required keeps the same draft editable and Reopen never sends it automatically", async () => {
  const { container, viki, socket, get, durable } = renderApp({
    customDefault: true,
    manual: true,
  });
  try {
    await act(async () => Promise.resolve());
    await waitFor(() => expect(chipText(container)).not.toBeNull(), { timeout: 50 });
    const key = get.mock.calls[0]?.[0] as string;
    const input = composerBox(container);
    typeAndSend(container, "keep this prompt");
    const request_id = opens(socket, key)[0]?.request_id;
    act(() =>
      socket.receive({
        v: 2,
        type: "session.open.refused",
        session_id: key,
        request_id,
        generation: "",
        seq: 0,
        data: { reason: "model_review_required" },
      }),
    );
    await act(async () => Promise.resolve());
    expect(composerBox(container)).toBe(input);
    expect(input.value).toBe("keep this prompt");
    expect(
      screen.getByText(
        "Review the model before sending. Your message has not been sent. Reopen this chat to check the current model.",
      ),
    ).not.toBeNull();
    expect(screen.queryByText("This conversation is unavailable.")).toBeNull();
    expect(
      (container.querySelector('.model-chip[aria-haspopup="menu"]') as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(durable.submissions).toEqual([]);
    fireEvent.change(input, { target: { value: "keep this newer prompt" } });
    get.mockResolvedValue({ model: "current-model", effort: "high" });
    fireEvent.click(screen.getByRole("button", { name: "Reopen chat" }));
    expect(opens(socket, key)).toHaveLength(2);
    expect(opens(socket, key)[1]).not.toHaveProperty("expected_model");
    expect(durable.submissions).toEqual([]);
    expect(
      (container.querySelector('.model-chip[aria-haspopup="menu"]') as HTMLButtonElement).disabled,
    ).toBe(true);
    act(() => socket.synchronize(key));
    await act(async () => Promise.resolve());
    expect(input.value).toBe("keep this newer prompt");
    expect(durable.submissions).toEqual([]);
    expect(
      (container.querySelector('.model-chip[aria-haspopup="menu"]') as HTMLButtonElement).disabled,
    ).toBe(false);
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(durable.submissions).toHaveLength(1), { timeout: 50 });
    expect(durable.submissions[0]?.session_id).toBe(key);
  } finally {
    await act(async () => viki.client.close());
  }
});

async function openRow(container: HTMLElement, sessionId: string): Promise<void> {
  const row = await waitFor(() => {
    const found = container.querySelector<HTMLElement>(`[data-session-id="${sessionId}"]`);
    if (!found) throw new Error("no row yet");
    return found;
  });
  fireEvent.click(row);
}

test("at start, with nothing selected, the chip reads the defaults before any send", async () => {
  const { container, viki, socket, get, models } = renderApp();
  await waitFor(() => expect(chipText(container)).toBe("Default (recommended) · high ▾"));
  expect(get).toHaveBeenCalledTimes(1);
  expect(models).toHaveBeenCalledWith("claude", expect.anything());
  expect(opens(socket)).toEqual([]);
  viki.client.close();
});

test("an existing uninitialized chat can explicitly select the current custom default with no native catalog", async () => {
  const { container, viki, socket } = renderApp({ sessions: [ROW], customDefault: true });
  try {
    await openRow(container, ROW);
    await waitFor(
      () => expect(chipText(container)).toBe("My connection · example/model · high ▾"),
      { timeout: 50 },
    );
    pick(container, "My connection · example/model");
    await waitFor(
      () =>
        expect(container.querySelector(".chip-note")?.getAttribute("data-kind")).toBe("applied"),
      { timeout: 50 },
    );
    expect(
      socket.sent
        .filter((sent) => sent.type === "session.set_model")
        .map((sent) => [sent.session_id, sent.model]),
    ).toEqual([[ROW, "viki-llm/11111111-1111-4111-8111-111111111111/example/model"]]);
  } finally {
    viki.client.close();
  }
});

test("an existing uninitialized chat can explicitly select the current native default", async () => {
  const { container, viki, socket } = renderApp({ sessions: [ROW] });
  try {
    await openRow(container, ROW);
    await waitFor(() => expect(chipText(container)).toBe("Default (recommended) · high ▾"), {
      timeout: 50,
    });
    pick(container, "Default (recommended)");
    await waitFor(
      () =>
        expect(container.querySelector(".chip-note")?.getAttribute("data-kind")).toBe("applied"),
      { timeout: 50 },
    );
    expect(
      socket.sent.filter((sent) => sent.type === "session.set_model").map((sent) => sent.model),
    ).toEqual(["default"]);
  } finally {
    viki.client.close();
  }
});

test("a native billing refusal leaves New session usable for an explicit custom model pick", async () => {
  const { container, viki, socket } = renderApp({ customDefault: true, billingBlocked: true });
  try {
    await waitFor(
      () => expect(chipText(container)).toBe("My connection · example/model · high ▾"),
      { timeout: 50 },
    );
    const button = container.querySelector<HTMLButtonElement>('[data-region="new-session"]');
    if (!button) throw new Error("no New session button");
    expect(button?.disabled).toBe(false);
    fireEvent.click(button);
    await waitFor(() => expect(opens(socket)).toHaveLength(1), { timeout: 50 });
    pick(container, "My connection · example/model");
    await waitFor(
      () => expect(socket.sent.filter((sent) => sent.type === "session.set_model")).toHaveLength(1),
      { timeout: 50 },
    );
    expect(screen.getByRole("status", { name: "Chat activity" }).textContent).not.toContain(
      "unavailable",
    );
  } finally {
    viki.client.close();
  }
});

test("a new chat's pick moves the label without a socket frame, and Send writes it before the chat's first session.open", async () => {
  const { container, viki, socket, durable, get, save } = renderApp();
  await waitFor(() => expect(chipText(container)).toBe("Default (recommended) · high ▾"));
  const key = get.mock.calls[0]?.[0] as string;
  const written = deferred<ChatPrefs>();
  const opensAtWrite: number[] = [];
  save.mockImplementation((sessionId) => {
    opensAtWrite.push(opens(socket, sessionId).length);
    return written.promise;
  });

  pick(container, "Sonnet");
  pick(container, "max");
  expect(chipText(container)).toBe("Sonnet · max ▾");
  expect(
    socket.sent.filter(
      (sent) => sent.type === "session.set_model" || sent.type === "session.set_effort",
    ),
  ).toEqual([]);

  typeAndSend(container, "hi");
  await waitFor(() =>
    expect(save).toHaveBeenCalledExactlyOnceWith(key, { model: "sonnet", effort: "max" }),
  );
  expect(opensAtWrite).toEqual([0]);
  expect(opens(socket, key)).toEqual([]); // held back while the write is out
  await act(async () => {
    written.resolve({ model: "sonnet", effort: "max" });
  });
  await waitFor(() => expect(opens(socket, key)).toHaveLength(1));
  await waitFor(() => expect(durable.submissions).toHaveLength(1));

  act(() => socket.receive(started(key, 1)));
  expect(chipText(container)).toBe("Sonnet 5 · max ▾"); // the label now reads info
  viki.client.close();
});

test("a failed write sends nothing, keeps the text, says so, and reads the chip again", async () => {
  const { container, viki, socket, get, save } = renderApp();
  save.mockRejectedValue(new Error("unavailable"));
  await waitFor(() => expect(chipText(container)).toBe("Default (recommended) · high ▾"));
  const key = get.mock.calls[0]?.[0] as string;
  pick(container, "Sonnet");
  pick(container, "max");

  typeAndSend(container, "hi");
  await waitFor(() =>
    expect(container.querySelector(".composer-refusal")?.textContent).toBe(PREFS_NOT_SAVED),
  );
  expect(save).toHaveBeenCalledExactlyOnceWith(key, { model: "sonnet", effort: "max" });
  expect(opens(socket)).toEqual([]);
  expect(composerBox(container).value).toBe("hi");
  await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
  expect(get).toHaveBeenLastCalledWith(key, expect.anything());
  expect(chipText(container)).toBe("Default (recommended) · high ▾");
  viki.client.close();
});

test("an opened chat with no live process shows its saved choice; a pick goes over the socket, then reads again", async () => {
  const { container, viki, socket, get } = renderApp({ sessions: [ROW] });
  let stored: ChatPrefs = { model: "sonnet", effort: "max" };
  const readsAfterControl: boolean[] = [];
  get.mockImplementation(async (sessionId) => {
    if (sessionId !== ROW) return DEFAULTS;
    readsAfterControl.push(socket.sent.some((sent) => sent.type === "session.set_model"));
    return stored;
  });

  await openRow(container, ROW);
  await waitFor(() => expect(chipText(container)).toBe("Sonnet · max ▾"));

  stored = { model: "opus", effort: "max" }; // what the server holds once the control lands
  pick(container, "Opus");
  expect(
    socket.sent
      .filter((sent) => sent.type === "session.set_model")
      .map((sent) => [sent.session_id, sent.model]),
  ).toEqual([[ROW, "opus"]]);
  expect(chipText(container)).toBe("Sonnet · max ▾"); // the label waits for the saved value
  await waitFor(() => {
    expect(chipText(container)).toBe("Opus · max ▾");
    expect(container.querySelector(".chip-note")?.textContent).toBe("Now using Opus");
  });
  expect(readsAfterControl).toEqual([false, true]);
  viki.client.close();
});

test("a profile-bound chat shows no chip before its first session.started and is never read", async () => {
  const inspected = deferred<PreparedProfilePreview>();
  const inspect = vi.fn<ProfileLaunchApi["inspect"]>((sessionId) =>
    sessionId === ROW ? inspected.promise : notFound(),
  );
  const { container, viki, get } = renderApp({ inspect, sessions: [ROW] });
  await waitFor(() => expect(chipText(container)).toBe("Default (recommended) · high ▾"));

  await openRow(container, ROW);
  await waitFor(() => expect(inspect).toHaveBeenCalledWith(ROW, expect.anything()));
  expect(chipText(container)).toBeNull(); // its binding is not known yet
  await act(async () => {
    inspected.resolve(preview(ROW));
  });
  await waitFor(() => expect(composerBox(container).placeholder).toContain("Planner"));
  expect(chipText(container)).toBeNull();
  expect(get).not.toHaveBeenCalledWith(ROW, expect.anything());
  viki.client.close();
});

test("with nothing held, Send opens the chat at once and writes nothing", async () => {
  const { container, viki, socket, get, save } = renderApp();
  await waitFor(() => expect(chipText(container)).toBe("Default (recommended) · high ▾"));
  const key = get.mock.calls[0]?.[0] as string;
  typeAndSend(container, "hi");
  expect(opens(socket, key)).toHaveLength(1); // no await stood between Send and the open
  expect(save).not.toHaveBeenCalled();
  viki.client.close();
});

/** A closed chat picked from "Other drafts in this window" must not become a held-pick new chat —
 * this window never minted it as new, and its commit could only ever be refused (the server answers 409
 * for an id that already has a transcript). */
test("a closed chat's key picked from the drafts list offers no chip, and Send opens it at once", async () => {
  const ROW2 = "44444444-4444-4444-8444-444444444444";
  const stored: ChatPrefs = { model: "sonnet", effort: "max" };
  const { container, viki, socket, get, save } = renderApp({ sessions: [ROW, ROW2] });
  get.mockImplementation(async (sessionId) => (sessionId === ROW ? stored : DEFAULTS));

  await openRow(container, ROW);
  await waitFor(() => expect(chipText(container)).toBe("Sonnet · max ▾"));
  fireEvent.change(composerBox(container), { target: { value: "draft for the closed chat" } });
  await act(async () => {
    await viki.actions.closeSession(ROW);
  });
  await openRow(container, ROW2);

  const draftButton = await waitFor(() => {
    const found = [...container.querySelectorAll<HTMLButtonElement>(".local-drafts button")].find(
      (b) => b.textContent?.includes(ROW.slice(0, 8)),
    );
    if (!found) throw new Error("no drafts-list entry yet");
    return found;
  });
  fireEvent.click(draftButton);

  // The closed chat is now the new-chat key, but this window never minted it as new: HEAD's
  // behaviour applies instead — no pre-start chip, and Send just reopens it on its stored prefs.
  await waitFor(() => expect(composerBox(container).value).toBe("draft for the closed chat"));
  expect(chipText(container)).toBeNull();

  const opensBeforeSend = opens(socket, ROW).length; // 1: the original openRow, before it closed
  fireEvent.keyDown(composerBox(container), { key: "Enter" });
  expect(opens(socket, ROW)).toHaveLength(opensBeforeSend + 1); // no await stood before the reopen
  expect(save).not.toHaveBeenCalled();
  viki.client.close();
});

/** A key this window minted as new and never sent keeps the held-pick chip even after the user
 * moves on to a different chat and picks it again from the drafts list. */
test("a never-sent key this window minted still shows the pre-start chip when picked again", async () => {
  const { container, viki, get } = renderApp();
  await waitFor(() => expect(chipText(container)).toBe("Default (recommended) · high ▾"));
  const firstKey = get.mock.calls[0]?.[0] as string;
  fireEvent.change(composerBox(container), { target: { value: "never sent" } });

  const newSessionButton = container.querySelector<HTMLButtonElement>(
    '[data-region="new-session"]',
  );
  if (!newSessionButton) throw new Error("no New session button");
  fireEvent.click(newSessionButton); // opens a second, live chat; the first stays a draft

  const draftButton = await waitFor(() => {
    const found = [...container.querySelectorAll<HTMLButtonElement>(".local-drafts button")].find(
      (b) => b.textContent?.includes(firstKey.slice(0, 8)),
    );
    if (!found) throw new Error("no drafts-list entry yet");
    return found;
  });
  fireEvent.click(draftButton); // back to the first, never-sent key

  await waitFor(() => expect(composerBox(container).value).toBe("never sent"));
  await waitFor(() => expect(chipText(container)).toBe("Default (recommended) · high ▾"));
  viki.client.close();
});

/** The other half of the fix above: a key this window minted and then started (handleSend drops
 * it from mintedRef on send) must not regain the held-pick chip if its draft is later picked
 * again from the drafts list — it already has a transcript, so a commit could only be refused. */
test("a chat this window started gets no held-pick chip when its draft is picked from the drafts list", async () => {
  const ROW2 = "44444444-4444-4444-8444-444444444444";
  const { container, viki, get } = renderApp({ sessions: [ROW2] });
  await waitFor(() => expect(chipText(container)).toBe("Default (recommended) · high ▾"));
  const key = get.mock.calls[0]?.[0] as string;

  typeAndSend(container, "hi");
  await waitFor(() => expect(composerBox(container).value).toBe(""));
  fireEvent.change(composerBox(container), { target: { value: "second draft" } });
  await act(async () => {
    await viki.actions.closeSession(key);
  });
  await openRow(container, ROW2);

  const draftButton = await waitFor(() => {
    const found = [...container.querySelectorAll<HTMLButtonElement>(".local-drafts button")].find(
      (b) => b.textContent?.includes(key.slice(0, 8)),
    );
    if (!found) throw new Error("no drafts-list entry yet");
    return found;
  });
  fireEvent.click(draftButton);
  await waitFor(() => expect(composerBox(container).value).toBe("second draft"));

  // A regression that left `key` in mintedRef would re-read its prefs on this tick: flush before
  // reading the chip so that extra, asynchronous read has its chance to land.
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(chipText(container)).toBeNull();
  viki.client.close();
});

/** restoreText mints a fresh key and marks it held-pick eligible (mintedRef.add): reaching it
 * through the app's own draft-recovery UI must show the pre-start chip on that fresh key. */
test("a draft restored from Drafts and send history gets a freshly minted key with the pre-start chip", async () => {
  const { container, viki, durable } = renderApp();
  await waitFor(() => expect(chipText(container)).toBe("Default (recommended) · high ▾"));

  durable.drafts.set("recovered-draft", {
    key: "recovered-draft",
    revision: 1,
    text: "recovered text",
    saved_at: "2026-09-14T00:00:00.000Z",
  });
  fireEvent.click(screen.getByText("Drafts and send history"));
  await waitFor(() => expect(screen.getByLabelText("Saved drafts")).toBeTruthy());
  const restoreButton = await screen.findByLabelText("Restore text from draft recovered-draft");
  fireEvent.click(restoreButton);

  await waitFor(() => expect(composerBox(container).value).toBe("recovered text"));
  await waitFor(() => expect(chipText(container)).toBe("Default (recommended) · high ▾"));
  viki.client.close();
});

test.each([true, false])(
  "a delayed first-send preference result (%s) preserves a newer chat and its draft",
  async (saved) => {
    const { container, viki, socket, durable, get, save } = renderApp({ sessions: [ROW] });
    try {
      await act(async () => Promise.resolve());
      const key = get.mock.calls[0]?.[0] as string;
      const written = deferred<ChatPrefs>();
      save.mockImplementation(async () => {
        const prefs = await written.promise;
        if (!saved) throw new Error("unavailable");
        return prefs;
      });
      pick(container, "Sonnet");
      typeAndSend(container, "message A");
      await act(async () => Promise.resolve());
      expect(save).toHaveBeenCalledTimes(1);
      await openRow(container, ROW);
      const current = composerBox(container);
      fireEvent.change(current, { target: { value: "newer draft for B" } });
      current.focus();
      current.setSelectionRange(2, 8);
      await act(async () => written.resolve({ model: "sonnet", effort: "high" }));
      expect(window.sessionStorage.getItem("viki.selected")).toBe(ROW);
      expect(composerBox(container)).toBe(current);
      expect(current.value).toBe("newer draft for B");
      expect([current.selectionStart, current.selectionEnd]).toEqual([2, 8]);
      expect(document.activeElement).toBe(current);
      expect(container.querySelector(".composer-refusal")).toBeNull();
      expect(opens(socket, key)).toHaveLength(0);
      expect(durable.submissions).toHaveLength(0);
    } finally {
      viki.client.close();
    }
  },
);

test("a delayed first send preserves a freshly restored new draft", async () => {
  const { container, viki, durable, save } = renderApp();
  try {
    await act(async () => Promise.resolve());
    const written = deferred<ChatPrefs>();
    save.mockImplementation(() => written.promise);
    pick(container, "Sonnet");
    typeAndSend(container, "message A");
    await act(async () => Promise.resolve());
    durable.drafts.set("recovered-draft", {
      key: "recovered-draft",
      revision: 1,
      text: "fresh new draft",
      saved_at: "2026-09-14T00:00:00.000Z",
    });
    await act(async () => fireEvent.click(screen.getByText("Drafts and send history")));
    const restore = await screen.findByLabelText(
      "Restore text from draft recovered-draft",
      {},
      { timeout: 50 },
    );
    await act(async () => fireEvent.click(restore));
    const current = composerBox(container);
    expect(current.value).toBe("fresh new draft");
    current.focus();
    await act(async () => written.resolve({ model: "sonnet", effort: "high" }));
    expect(window.sessionStorage.getItem("viki.selected")).toBeNull();
    expect(composerBox(container)).toBe(current);
    expect(current.value).toBe("fresh new draft");
    expect(document.activeElement).toBe(current);
    expect(durable.submissions).toHaveLength(0);
  } finally {
    viki.client.close();
  }
});
