/**
 * App-level wiring for a bound chat's identity. `ProfileSessionReview`'s inspection pins a
 * session's identity in `App`; the chat column (empty state, a reply, the composer placeholder)
 * then renders inside it, while the top bar keeps the app identity throughout. Until the binding
 * is known the column names no assistant; an unbound chat (a 404, or one New session minted)
 * shows the app identity.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki, GENERATION, sessionRow } from "../../accept/m3b/fakes";
import type { ProfileApi } from "../../clients/profiles/profileApi";
import {
  type PreparedProfilePreview,
  type ProfileLaunchApi,
  ProfileLaunchApiError,
  type ProfileSessionIdentity,
} from "../../clients/profiles/profileLaunchApi";
import { DEFAULT_IDENTITY, IdentityContext } from "../../components/Avatar/identity";
import { ConversationHeader } from "../../components/Conversation/ConversationHeader";
import type { Envelope, EventMap, ModelOption } from "../../protocol";
import { fakeDurable } from "../../testDurable";

afterEach(cleanup);

const MODELS: ModelOption[] = [{ value: "sonnet", label: "Sonnet", description: "" }];

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
    effort: "low",
    commands: [],
    agents: [],
    models: MODELS,
    cli_version: "2.1.259",
    resumed: true,
  });
}

function userMessage(sessionId: string, seq: number): Envelope {
  return frame(sessionId, seq, "user.message", {
    text: "hi",
    command: "",
    at: "2026-09-27T10:00:00.000Z",
  });
}

function textDelta(sessionId: string, seq: number): Envelope {
  return frame(sessionId, seq, "text.delta", { block_id: "b1", text: "hello" });
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

function fakeProfileLaunchApi(inspect: ProfileLaunchApi["inspect"]): ProfileLaunchApi {
  return { capabilities: vi.fn(), models: vi.fn(), inspect, prepare: vi.fn() };
}

async function openRow(container: HTMLElement, sessionId: string): Promise<void> {
  const row = await waitFor(() => {
    const found = container.querySelector<HTMLElement>(`[data-session-id="${sessionId}"]`);
    if (!found) throw new Error("no row yet");
    return found;
  });
  fireEvent.click(row);
}

function wordmark(container: HTMLElement): string | null {
  return container.querySelector('[data-region="wordmark"]')?.textContent ?? null;
}

test("a pinned profile identity names the header, reply and composer; the top bar names VIKI Agent", async () => {
  const inspect = vi.fn<ProfileLaunchApi["inspect"]>(async (sessionId) =>
    sessionId === "profile-session"
      ? preview("profile-session")
      : Promise.reject(new ProfileLaunchApiError(404, "not_found")),
  );
  const rest = fakeRest({
    sessions: [sessionRow({ session_id: "profile-session", title: "Bound chat" })],
  });
  const { viki, socket } = fakeViki();
  const { container } = render(
    <App
      viki={viki}
      rest={rest}
      durable={fakeDurable()}
      profileLaunchApi={fakeProfileLaunchApi(inspect)}
    />,
  );
  act(() => socket.open());

  await openRow(container, "profile-session");
  act(() => socket.receive(started("profile-session", 1)));
  await waitFor(() => expect(inspect).toHaveBeenCalledWith("profile-session", expect.anything()));

  // The header: profile name, r1 and the Profile chip.
  await waitFor(() => expect(container.textContent).toContain("Planner"));
  const chipTexts = () =>
    Array.from(container.querySelectorAll(".chip")).map((el) => el.textContent);
  // The header draws the pair.
  expect(chipTexts().filter((t) => t === "Planner · r1")).toHaveLength(1);
  expect(chipTexts().filter((t) => t === "Profile")).toHaveLength(1);
  expect(wordmark(container)).toBe("VIKI Agent");

  // A bound chat's mode is the profile's, not the user's to change here (AGENTS.md: a profile
  // session's picks are always server-refused).
  const modeChip = container.querySelector(".mode-chip") as HTMLButtonElement;
  expect(modeChip.disabled).toBe(true);
  expect(modeChip.getAttribute("title")).toBe("Set by the bound profile");

  // A reply names the profile too, with its own r1/Profile chips.
  act(() => socket.receive(userMessage("profile-session", 2)));
  act(() => socket.receive(textDelta("profile-session", 3)));
  expect(container.querySelector(".reply-name")?.textContent).toBe("Planner");
  expect(chipTexts().filter((t) => t === "Planner · r1")).toHaveLength(1); // the header
  expect(chipTexts().filter((t) => t === "r1")).toHaveLength(1); // the reply
  expect(chipTexts().filter((t) => t === "Profile")).toHaveLength(2); // header and reply

  // The composer placeholder names the profile.
  const textarea = container.querySelector("textarea");
  expect(textarea?.getAttribute("placeholder")).toContain("Planner");

  // The top bar never took on the profile's name.
  expect(wordmark(container)).toBe("VIKI Agent");

  // A brand-new (ordinary) session: the inspection answers 404, so it stays VIKI.
  socket.sent.length = 0;
  fireEvent.click(screen.getByText("New session"));
  const opened = socket.sent[0] as { session_id: string };
  await waitFor(() => expect(inspect).toHaveBeenCalledWith(opened.session_id, expect.anything()));
  await waitFor(() => expect(container.querySelector(".empty-sub")?.textContent).toContain("VIKI"));
  expect(container.querySelector(".empty-sub")?.textContent).not.toContain("Planner");

  viki.client.close();
});

test("re-inspecting the same session keeps the same pinned identity", async () => {
  const inspect = vi.fn<ProfileLaunchApi["inspect"]>().mockResolvedValue(preview("s-1"));
  const rest = fakeRest({ sessions: [sessionRow({ session_id: "s-1", title: "Chat" })] });
  const { viki, socket } = fakeViki();
  const { container } = render(
    <App
      viki={viki}
      rest={rest}
      durable={fakeDurable()}
      profileLaunchApi={fakeProfileLaunchApi(inspect)}
    />,
  );
  act(() => socket.open());
  await openRow(container, "s-1");
  act(() => socket.receive(started("s-1", 1)));
  await waitFor(() => expect(inspect).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(container.textContent).toContain("Planner"));

  act(() =>
    socket.receive(
      frame("s-1", 2, "error", {
        code: "bad_request",
        message: "This session can no longer start from its profile launch.",
        recoverable: false,
      }),
    ),
  );
  await waitFor(() => expect(inspect).toHaveBeenCalledTimes(2));
  expect(container.textContent).toContain("Planner");
  expect(container.textContent).toContain("r1");

  viki.client.close();
});

// A stand-in for the Profiles view: one button that reports a prepared session the way the real
// view does after a successful prepare, so App's handler is driven without the whole form.
vi.mock("../../components/Profiles/ProfilesView", () => ({
  ProfilesView: ({
    onPrepared,
  }: {
    onPrepared?: (sessionId: string, identity: ProfileSessionIdentity | null) => void;
  }) => (
    <button
      type="button"
      onClick={() =>
        onPrepared?.("prepared-session", {
          name: "Planner",
          avatar_asset_id: null,
          accent: "orange",
          shape: "circle",
          profile_revision: 1,
        })
      }
    >
      Prepared
    </button>
  ),
}));

const NEUTRAL_EMPTY = "Start talking. Use / to call a skill.";
const NEUTRAL_PLACEHOLDER = "Talk… / for skills";
const VIKI_EMPTY = "Talk to VIKI. Use / to call a skill.";

/** Every `inspect` stays open until the test answers it by session id. */
function heldInspect() {
  const answers = new Map<
    string,
    { resolve: (value: PreparedProfilePreview) => void; reject: (error: unknown) => void }
  >();
  const inspect = vi.fn<ProfileLaunchApi["inspect"]>(
    (sessionId) =>
      new Promise((resolve, reject) => {
        answers.set(sessionId, { resolve, reject });
      }),
  );
  const answer = async (sessionId: string, value: PreparedProfilePreview | Error) => {
    const pending = answers.get(sessionId);
    if (!pending) throw new Error(`no inspection for ${sessionId}`);
    answers.delete(sessionId);
    await act(async () => {
      if (value instanceof Error) pending.reject(value);
      else pending.resolve(value);
    });
  };
  return { inspect, answer };
}

function chatColumn(container: HTMLElement): HTMLElement {
  const column = container.querySelector<HTMLElement>('[data-region="conversation"]');
  if (!column) throw new Error("no chat column");
  return column;
}

function placeholder(container: HTMLElement): string | null {
  return chatColumn(container).querySelector("textarea")?.getAttribute("placeholder") ?? null;
}

/** Answers the opened row's first history page with nothing, so its empty state can show. */
function emptyHistory(socket: ReturnType<typeof fakeViki>["socket"], sessionId: string): void {
  act(() =>
    socket.receive({
      v: 2,
      generation: GENERATION,
      request_id: "",
      type: "history.page",
      session_id: sessionId,
      data: { events: [], next_before_uuid: null },
    }),
  );
}

/** The empty state's sentence, once the opened row's first history page has landed. */
async function emptySub(container: HTMLElement): Promise<string | null> {
  return waitFor(() => {
    const found = chatColumn(container).querySelector(".empty-sub");
    if (!found) throw new Error("no empty state yet");
    return found.textContent;
  });
}

function expectUnresolved(container: HTMLElement): void {
  const column = chatColumn(container);
  expect(placeholder(container)).toBe(NEUTRAL_PLACEHOLDER);
  expect(column.querySelector('img[src^="/api/face/"]')).toBeNull();
  expect(column.querySelector(".reply-name")).toBeNull();
  expect(column.textContent).not.toContain("VIKI");
}

function renderBound(inspect: ProfileLaunchApi["inspect"], sessionIds: string[]) {
  const rest = fakeRest({
    sessions: sessionIds.map((session_id) => sessionRow({ session_id, title: session_id })),
  });
  const { viki, socket } = fakeViki();
  const { container } = render(
    <App
      viki={viki}
      rest={rest}
      durable={fakeDurable()}
      profileLaunchApi={fakeProfileLaunchApi(inspect)}
    />,
  );
  act(() => socket.open());
  return { container, viki, socket };
}

test("a chat whose binding is not known yet shows no assistant name or face until it is", async () => {
  const held = heldInspect();
  const { container, viki, socket } = renderBound(held.inspect, ["s-1"]);
  await openRow(container, "s-1");
  act(() => socket.receive(started("s-1", 1)));
  emptyHistory(socket, "s-1");
  await waitFor(() => expect(held.inspect).toHaveBeenCalledWith("s-1", expect.anything()));

  expect(await emptySub(container)).toBe(NEUTRAL_EMPTY);
  expectUnresolved(container);
  expect(wordmark(container)).toBe("VIKI Agent");

  // A reply while still unknown: no name in its name row, no face.
  act(() => socket.receive(userMessage("s-1", 2)));
  act(() => socket.receive(textDelta("s-1", 3)));
  expectUnresolved(container);

  await held.answer("s-1", preview("s-1"));
  expect(chatColumn(container).querySelector(".reply-name")?.textContent).toBe("Planner");
  expect(placeholder(container)).toContain("Planner");
  expect(wordmark(container)).toBe("VIKI Agent");
  viki.client.close();
});

test("a 404 answer shows the app identity; a failed read stays unresolved", async () => {
  const held = heldInspect();
  const { container, viki, socket } = renderBound(held.inspect, ["plain", "flaky"]);
  await openRow(container, "plain");
  act(() => socket.receive(started("plain", 1)));
  emptyHistory(socket, "plain");
  await waitFor(() => expect(held.inspect).toHaveBeenCalledWith("plain", expect.anything()));
  await held.answer("plain", new ProfileLaunchApiError(404, "not_found"));
  expect(await emptySub(container)).toBe(VIKI_EMPTY);

  await openRow(container, "flaky");
  act(() => socket.receive(started("flaky", 1)));
  emptyHistory(socket, "flaky");
  await waitFor(() => expect(held.inspect).toHaveBeenCalledWith("flaky", expect.anything()));
  await held.answer("flaky", new ProfileLaunchApiError(503, "unavailable"));
  expect(await screen.findByText("Profile details are unavailable.")).toBeTruthy();
  expect(await emptySub(container)).toBe(NEUTRAL_EMPTY);
  expectUnresolved(container);
  viki.client.close();
});

test("New session shows the app identity at once, before its inspection answers", async () => {
  const held = heldInspect();
  const { container, viki, socket } = renderBound(held.inspect, []);
  socket.sent.length = 0;
  fireEvent.click(screen.getByText("New session"));
  expect(chatColumn(container).querySelector(".empty-sub")?.textContent).toBe(VIKI_EMPTY);
  const opened = socket.sent[0] as { session_id: string };
  await waitFor(() =>
    expect(held.inspect).toHaveBeenCalledWith(opened.session_id, expect.anything()),
  );
  expect(chatColumn(container).querySelector(".empty-sub")?.textContent).toBe(VIKI_EMPTY);
  expect(placeholder(container)).toContain("VIKI");
  viki.client.close();
});

test("a bound chat keeps its profile identity when a later inspection answers 404 or fails", async () => {
  const held = heldInspect();
  const { container, viki, socket } = renderBound(held.inspect, ["s-1"]);
  await openRow(container, "s-1");
  act(() => socket.receive(started("s-1", 1)));
  await waitFor(() => expect(held.inspect).toHaveBeenCalledTimes(1));
  await held.answer("s-1", preview("s-1"));
  expect(placeholder(container)).toContain("Planner");

  const refusal = (seq: number) =>
    frame("s-1", seq, "error", {
      code: "bad_request",
      message: "This session can no longer start from its profile launch.",
      recoverable: false,
    });
  act(() => socket.receive(refusal(2)));
  await waitFor(() => expect(held.inspect).toHaveBeenCalledTimes(2));
  await held.answer("s-1", new ProfileLaunchApiError(404, "not_found"));
  expect(placeholder(container)).toContain("Planner");

  act(() => socket.receive(refusal(3)));
  await waitFor(() => expect(held.inspect).toHaveBeenCalledTimes(3));
  await held.answer("s-1", new ProfileLaunchApiError(503, "unavailable"));
  expect(await screen.findByText("Profile details are unavailable.")).toBeTruthy();
  expect(placeholder(container)).toContain("Planner");
  expect(chatColumn(container).textContent).toContain("r1");
  viki.client.close();
});

/** No profiles are ever read: the Profiles view is the stand-in above. */
function idleProfileApi(): ProfileApi {
  const pending = () => new Promise<never>(() => {});
  return {
    list: async () => [],
    get: pending,
    create: pending,
    revise: pending,
    archive: pending,
    duplicate: pending,
    compare: pending,
  };
}

test("a chat the user just prepared is bound from its first render", async () => {
  const held = heldInspect();
  const { viki, socket } = fakeViki();
  const { container } = render(
    <App
      viki={viki}
      rest={fakeRest()}
      durable={fakeDurable()}
      profileApi={idleProfileApi()}
      profileLaunchApi={fakeProfileLaunchApi(held.inspect)}
    />,
  );
  act(() => socket.open());
  fireEvent.click(
    within(screen.getByRole("navigation", { name: "Navigation and sessions" })).getByRole(
      "button",
      { name: "Agents" },
    ),
  );
  fireEvent.click(await screen.findByRole("button", { name: "Prepared" }));
  expect(placeholder(container)).toContain("Planner");
  expect(chatColumn(container).textContent).toContain("Planner · r1");
  viki.client.close();
});

test("with no profile launch client a selected session shows the app identity", async () => {
  const rest = fakeRest({ sessions: [sessionRow({ session_id: "s-1", title: "Chat" })] });
  const { viki, socket } = fakeViki();
  const { container } = render(<App viki={viki} rest={rest} durable={fakeDurable()} />);
  act(() => socket.open());
  await openRow(container, "s-1");
  act(() => socket.receive(started("s-1", 1)));
  emptyHistory(socket, "s-1");
  expect(await emptySub(container)).toBe(VIKI_EMPTY);
  viki.client.close();
});

function composerBox(container: HTMLElement): HTMLTextAreaElement {
  const box = chatColumn(container).querySelector("textarea");
  if (!box) throw new Error("no composer");
  return box;
}

function typeAndSend(container: HTMLElement, text: string): void {
  fireEvent.change(composerBox(container), { target: { value: text } });
  fireEvent.keyDown(composerBox(container), { key: "Enter" });
}

function closed(sessionId: string, seq: number): Envelope {
  return frame(sessionId, seq, "session.closed", { reason: "requested" });
}

test("with a profile client the launch greeting and the placeholder name the app", async () => {
  const held = heldInspect();
  const { container, viki } = renderBound(held.inspect, []);
  expect(await emptySub(container)).toBe(VIKI_EMPTY);
  expect(placeholder(container)).toContain("VIKI");
  viki.client.close();
});

test("the first send from the launch composer keeps the app identity while its read is held", async () => {
  const held = heldInspect();
  const { container, viki } = renderBound(held.inspect, []);
  typeAndSend(container, "hello");
  await waitFor(() => expect(held.inspect).toHaveBeenCalledTimes(1));
  expect(placeholder(container)).toContain("VIKI");
  viki.client.close();
});

test("a closed chat shows the app identity, and so does its reopened draft's first send", async () => {
  const held = heldInspect();
  const { container, viki, socket } = renderBound(held.inspect, ["s-1"]);
  await openRow(container, "s-1");
  act(() => socket.receive(started("s-1", 1)));
  await waitFor(() => expect(held.inspect).toHaveBeenCalledWith("s-1", expect.anything()));
  act(() => socket.receive(closed("s-1", 2)));
  // Its read never answered, yet a closed selection has no editor key of its own.
  expect(await emptySub(container)).toBe(VIKI_EMPTY);

  fireEvent.click(screen.getByRole("button", { name: "Open in a new message box" }));
  expect(placeholder(container)).toContain("VIKI");
  typeAndSend(container, "again");
  await waitFor(() => expect(held.inspect).toHaveBeenCalledTimes(2));
  expect(placeholder(container)).toContain("VIKI");
  viki.client.close();
});

test("with nothing selected the column follows the new-chat editor's key, a closed bound chat's draft included", async () => {
  const held = heldInspect();
  const { container, viki, socket } = renderBound(held.inspect, ["s-1"]);
  await openRow(container, "s-1");
  act(() => socket.receive(started("s-1", 1)));
  await waitFor(() => expect(held.inspect).toHaveBeenCalledWith("s-1", expect.anything()));
  await held.answer("s-1", preview("s-1"));
  fireEvent.change(composerBox(container), { target: { value: "unsent" } });
  act(() => socket.receive(closed("s-1", 2)));
  fireEvent.click(screen.getByText("New session"));
  expect(placeholder(container)).toContain("VIKI");

  fireEvent.click(await screen.findByRole("button", { name: /Draft s-1/ }));
  expect(composerBox(container).value).toBe("unsent");
  expect(placeholder(container)).toContain("Planner");
  expect(chatColumn(container).querySelector(".empty-sub")?.textContent).toContain("Planner");
  viki.client.close();
});

test("a fork keeps the app identity while its own read is held", async () => {
  const held = heldInspect();
  const { container, viki, socket } = renderBound(held.inspect, ["s-1"]);
  await openRow(container, "s-1");
  act(() => socket.receive(started("s-1", 1)));
  await waitFor(() => expect(held.inspect).toHaveBeenCalledWith("s-1", expect.anything()));
  await held.answer("s-1", new ProfileLaunchApiError(404, "not_found"));
  fireEvent.click(container.querySelector('[data-session-id="s-1"] .sess-menu') as HTMLElement);
  fireEvent.click(await screen.findByRole("menuitem", { name: "Fork" }));
  typeAndSend(container, "fork prompt");
  await waitFor(() => expect(held.inspect).toHaveBeenCalledTimes(2));
  const forkId = held.inspect.mock.calls[1]?.[0];
  expect(forkId).not.toBe("s-1");
  expect(placeholder(container)).toContain("VIKI");
  viki.client.close();
});

test("Refresh review after a failed read resolves the chat", async () => {
  const held = heldInspect();
  const { container, viki, socket } = renderBound(held.inspect, ["s-1"]);
  await openRow(container, "s-1");
  act(() => socket.receive(started("s-1", 1)));
  await waitFor(() => expect(held.inspect).toHaveBeenCalledTimes(1));
  await held.answer("s-1", new ProfileLaunchApiError(503, "unavailable"));
  expect(placeholder(container)).toBe(NEUTRAL_PLACEHOLDER);

  fireEvent.click(screen.getByRole("button", { name: "Refresh review" }));
  await waitFor(() => expect(held.inspect).toHaveBeenCalledTimes(2));
  await held.answer("s-1", preview("s-1"));
  expect(placeholder(container)).toContain("Planner");
  viki.client.close();
});

test("a stale window's notice omits the old default portrait and the chat's avatar", async () => {
  const held = heldInspect();
  const { container, viki, socket } = renderBound(held.inspect, ["s-1"]);
  await openRow(container, "s-1");
  act(() => socket.receive(started("s-1", 1)));
  await waitFor(() => expect(held.inspect).toHaveBeenCalledTimes(1));
  act(() => socket.onclose?.({ code: 4401 }));
  const stale = await waitFor(() => {
    const found = container.querySelector(".stale");
    if (!found) throw new Error("no stale notice yet");
    return found;
  });
  expect(stale.querySelector("img")).toBeNull();
  viki.client.close();
});

test("an unresolved header draws no profile chips, whatever `profile` holds", () => {
  render(
    <IdentityContext.Provider
      value={{
        ...DEFAULT_IDENTITY,
        unresolved: true,
        profile: {
          name: "Planner",
          avatarId: null,
          accent: "orange",
          shape: "circle",
          revision: 1,
        },
      }}
    >
      <ConversationHeader title="Chat" />
    </IdentityContext.Provider>,
  );
  expect(screen.queryByText("Planner · r1")).toBeNull();
  expect(screen.queryByText("Profile")).toBeNull();
});
