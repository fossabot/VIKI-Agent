/**
 * App-level wiring for the not-signed-in card's Sign in strip:
 * whether `providerLoginApi` and `platformKind()` gate `SignInStrip` onto a `not_logged_in`
 * `view.error`, right after the `ErrorCard`, and that a click sends nothing over the session
 * socket. SignInStrip.unit.test.tsx and src/clients/app/providerLoginApi.unit.test.ts already pin the strip's
 * and the client's own behaviour; this file pins only what App itself is responsible for: the
 * render gate and the wiring.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki, GENERATION, sessionRow } from "../../accept/m3b/fakes";
import type { ProviderLoginApi } from "../../clients/app/providerLoginApi";
import { fakeDurable } from "../../testDurable";

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
  delete (window.navigator as { platform?: string }).platform;
  vi.restoreAllMocks();
});

const AT = "2026-10-10T00:00:00.000Z";
const USAGE = { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_creation_tokens: 0 };

type FakeSocket = ReturnType<typeof fakeViki>["socket"];

function fakeProviderLogin(
  open: ProviderLoginApi["open"] = vi.fn().mockResolvedValue(undefined),
): ProviderLoginApi {
  return { open };
}

async function openRow(container: HTMLElement, id: string): Promise<void> {
  const row = await waitFor(() => {
    const found = container.querySelector<HTMLElement>(`[data-session-id="${id}"]`);
    if (!found) throw new Error(`no row ${id}`);
    return found;
  });
  await act(async () => {
    fireEvent.click(row);
  });
}

/** Ends session `sessionId`'s one turn with a failure: the `turn.done error` half of the
 * sequence the not-signed-in card arrives by (sessionReducer.ts `applyEnvelope`'s "error" case —
 * only once a turn is no longer "running" does an `error` envelope become `view.error` rather
 * than a notice row on the turn itself). */
function endTurn(socket: FakeSocket, sessionId: string): void {
  act(() =>
    socket.receive({
      v: 2,
      generation: GENERATION,
      request_id: "",
      type: "user.message",
      session_id: sessionId,
      seq: 1,
      data: { text: "hi", command: "", at: AT },
    }),
  );
  act(() =>
    socket.receive({
      v: 2,
      generation: GENERATION,
      request_id: "",
      type: "turn.done",
      session_id: sessionId,
      seq: 2,
      data: { status: "error", usage: USAGE, context_percent: null, duration_ms: 1 },
    }),
  );
}

/** The `error` half: raised only after `endTurn`, so it lands as `view.error`. */
function raiseError(socket: FakeSocket, sessionId: string, code: string): void {
  act(() =>
    socket.receive({
      v: 2,
      generation: GENERATION,
      request_id: "",
      type: "error",
      session_id: sessionId,
      seq: 3,
      data: { code, message: "", recoverable: code === "crashed" },
    }),
  );
}

test("Windows with the client: a not_logged_in card gets the Sign in strip; the click opens once and sends nothing", async () => {
  const rest = fakeRest({ sessions: [sessionRow({ session_id: "s1", title: "Foo" })] });
  const { viki, socket } = fakeViki();
  const durable = fakeDurable();
  const open = vi.fn().mockResolvedValue(undefined);
  const { container } = render(
    <App durable={durable} viki={viki} rest={rest} providerLoginApi={fakeProviderLogin(open)} />,
  );
  act(() => socket.open());
  await openRow(container, "s1");
  endTurn(socket, "s1");
  raiseError(socket, "s1", "not_logged_in");

  expect(screen.getByText("Not signed in to Claude Code")).toBeTruthy();
  const strip = container.querySelector(".signin-strip");
  expect(strip).toBeTruthy();
  expect(strip?.previousElementSibling?.classList.contains("error-card")).toBe(true);
  const button = strip?.querySelector("button") as HTMLButtonElement;
  expect(button.textContent).toBe("Sign in");
  const sentBefore = socket.sent.length;

  fireEvent.click(button);
  await act(async () => {});

  expect(open).toHaveBeenCalledTimes(1);
  expect(strip?.querySelector('[role="status"]')?.textContent).toBe(
    "Finish signing in in your browser, then send your message again.",
  );
  expect(socket.sent).toHaveLength(sentBefore);
  expect(durable.submissions).toHaveLength(0);
});

test("a crashed error gets no Sign in strip", async () => {
  const rest = fakeRest({ sessions: [sessionRow({ session_id: "s1", title: "Foo" })] });
  const { viki, socket } = fakeViki();
  const { container } = render(
    <App durable={fakeDurable()} viki={viki} rest={rest} providerLoginApi={fakeProviderLogin()} />,
  );
  act(() => socket.open());
  await openRow(container, "s1");
  endTurn(socket, "s1");
  raiseError(socket, "s1", "crashed");

  expect(screen.getByText("Claude Code ended unexpectedly")).toBeTruthy();
  expect(container.querySelector(".signin-strip")).toBeNull();
});

test("macOS (MacIntel): a not_logged_in card gets the Sign in strip", async () => {
  Object.defineProperty(window.navigator, "platform", { value: "MacIntel", configurable: true });
  const rest = fakeRest({ sessions: [sessionRow({ session_id: "s1", title: "Foo" })] });
  const { viki, socket } = fakeViki();
  const { container } = render(
    <App durable={fakeDurable()} viki={viki} rest={rest} providerLoginApi={fakeProviderLogin()} />,
  );
  act(() => socket.open());
  await openRow(container, "s1");
  endTurn(socket, "s1");
  raiseError(socket, "s1", "not_logged_in");

  expect(screen.getByText("Not signed in to Claude Code")).toBeTruthy();
  expect(container.querySelector(".signin-strip")).not.toBeNull();
});

test("Linux (Linux x86_64): a not_logged_in card gets the Sign in strip", async () => {
  Object.defineProperty(window.navigator, "platform", {
    value: "Linux x86_64",
    configurable: true,
  });
  const rest = fakeRest({ sessions: [sessionRow({ session_id: "s1", title: "Foo" })] });
  const { viki, socket } = fakeViki();
  const { container } = render(
    <App durable={fakeDurable()} viki={viki} rest={rest} providerLoginApi={fakeProviderLogin()} />,
  );
  act(() => socket.open());
  await openRow(container, "s1");
  endTurn(socket, "s1");
  raiseError(socket, "s1", "not_logged_in");

  expect(screen.getByText("Not signed in to Claude Code")).toBeTruthy();
  expect(container.querySelector(".signin-strip")).not.toBeNull();
});

test("without a providerLoginApi client a not_logged_in card gets no Sign in strip", async () => {
  const rest = fakeRest({ sessions: [sessionRow({ session_id: "s1", title: "Foo" })] });
  const { viki, socket } = fakeViki();
  const { container } = render(<App durable={fakeDurable()} viki={viki} rest={rest} />);
  act(() => socket.open());
  await openRow(container, "s1");
  endTurn(socket, "s1");
  raiseError(socket, "s1", "not_logged_in");

  expect(screen.getByText("Not signed in to Claude Code")).toBeTruthy();
  expect(container.querySelector(".signin-strip")).toBeNull();
});
