/**
 * App-level coverage for the profile-session wiring — `isProfileSession`,
 * `onProfileSession` and `profileReviewRefreshKey` — which needs `App` rendered with a
 * `profileLaunchApi`. Modeled on App.unit.test.tsx and src/app-tests/chat/durable.unit.test.tsx's App
 * setup: a real `createViki` over a scripted socket, with a fake `profileLaunchApi`.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki, GENERATION, sessionRow } from "../../accept/m3b/fakes";
import {
  type PreparedProfilePreview,
  type ProfileLaunchApi,
  ProfileLaunchApiError,
} from "../../clients/profiles/profileLaunchApi";
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

function chip(container: HTMLElement): HTMLElement | null {
  return container.querySelector(".model-chip");
}

test("the model chip hides for an inspected profile session, and returns for an ordinary one", async () => {
  const inspect = vi.fn<ProfileLaunchApi["inspect"]>(async (sessionId) =>
    sessionId === "profile-session"
      ? preview("profile-session")
      : Promise.reject(new ProfileLaunchApiError(404, "not_found")),
  );
  const rest = fakeRest({
    sessions: [
      sessionRow({ session_id: "profile-session", title: "Profile" }),
      sessionRow({ session_id: "plain-session", title: "Plain" }),
    ],
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
  await waitFor(() => expect(chip(container)).toBeNull());

  await openRow(container, "plain-session");
  act(() => socket.receive(started("plain-session", 1)));
  await waitFor(() => expect(inspect).toHaveBeenCalledWith("plain-session", expect.anything()));
  await waitFor(() => expect(chip(container)).not.toBeNull());
  viki.client.close();
});

// A start refusal (an "error" outside a running turn) must re-inspect once, but a later
// envelope that never touches `error` must not force a second one — the review's own retry
// budget (4 shared permits) is a scarce resource this must not spend for nothing.
test("a start refusal re-inspects once; a later unrelated event does not re-inspect again", async () => {
  const inspect = vi
    .fn<ProfileLaunchApi["inspect"]>()
    .mockRejectedValue(new ProfileLaunchApiError(404, "not_found"));
  const rest = fakeRest({
    sessions: [sessionRow({ session_id: "s-1", title: "Chat" })],
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
  await openRow(container, "s-1");
  act(() => socket.receive(started("s-1", 1)));
  await waitFor(() => expect(inspect).toHaveBeenCalledTimes(1));

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

  act(() =>
    socket.receive(
      frame("s-1", 3, "rate_limit", { status: "allowed_warning", kind: "5h", resets_at: 0 }),
    ),
  );
  await act(async () => {});
  expect(inspect).toHaveBeenCalledTimes(2);
  viki.client.close();
});

// The nit: a failed inspection (429/503, say) proves nothing about whether this session is
// profile-bound, so `onProfileSession` is never called for it. Leaving the chip visible is the
// safer default — hiding it needs proof, and an ordinary session must not lose its picker over a
// transient failure. A stale profile session's next pick still server-refuses (profile_locked);
// that residual is tracked separately, not fixed by this chip decision.
test("a failed inspection leaves the model chip visible", async () => {
  const inspect = vi
    .fn<ProfileLaunchApi["inspect"]>()
    .mockRejectedValue(new ProfileLaunchApiError(503, "unavailable"));
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
  await waitFor(() => expect(screen.getByText("Profile details are unavailable.")).toBeTruthy());
  expect(chip(container)).not.toBeNull();
  viki.client.close();
});
