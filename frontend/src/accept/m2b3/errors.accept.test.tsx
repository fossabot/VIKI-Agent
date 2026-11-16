/**
 * Acceptance (frozen): the error cards and the already-open card. Each one has to say what
 * happened and what to do next, in the user's language, without a stack trace or a path —
 * and the already-open card is the only thing standing between two processes and a
 * corrupted transcript.
 *
 * AMENDED after a live smoke run: the two rate-limit cases below passed
 * `status: "limited"`, a value the CLI has never sent. Its real statuses are `allowed`,
 * `allowed_warning` and `rejected` — confirmed in the bundled binary, whose own wording for
 * `allowed_warning` is "You're close to your …". So the card ignored `status` and announced
 * "Usage limit reached" under a reply that had just arrived intact, and this file
 * could not have caught it: a fixture that invents its input tests the renderer against
 * fiction. The two cases now pass `rejected`, which is what they always meant, and a third
 * pins the warning. Same failure mode as the `"claude-cli"` entrypoint amendment below.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { BlockedCard } from "../../components/Cards/BlockedCard";
import { ErrorCard, RateLimitCard } from "../../components/Cards/ErrorCard";
import type { ErrorCode, SessionBlockedData } from "../../protocol";

afterEach(cleanup);

function showError(code: ErrorCode, message = "Traceback: C:\\Users\\someone\\x.py line 9") {
  const onRetry = vi.fn();
  const { container } = render(
    <ErrorCard data={{ code, message, recoverable: code === "crashed" }} onRetry={onRetry} />,
  );
  const card = container.querySelector(".error-card") as HTMLElement;
  return { container, card, onRetry };
}

// Amended 2026-09-18. The line this pinned promised a resume unconditionally, and
// a crash on a session's first turn cannot deliver one: no transcript exists yet, so the re-armed
// `--resume` answers `transcript_missing`. `MSG_STOP_KILLED` got the same correction. The copy
// is now conditional, in the same "If …" form the rest of the error copy
// uses, and `MSG_CRASHED` says the same thing.
test("a crash says when it can be resumed, and offers the one button that does it", () => {
  const { card, onRetry } = showError("crashed");
  expect(card.querySelector(".chip")?.textContent).toBe("Error");
  expect(card.textContent).toContain("Claude Code ended unexpectedly");
  expect(card.textContent).toContain(
    "If the conversation record is still available, send again to resume from here.",
  );
  fireEvent.click(screen.getByText("Send again"));
  expect(onRetry).toHaveBeenCalledTimes(1);
});

test("not logged in tells him exactly where to go, and offers no button", () => {
  const { card } = showError("not_logged_in");
  expect(card.textContent).toContain("Not signed in to Claude Code");
  expect(card.textContent).toContain("Open claude in a terminal, run /login, then send again.");
  expect(card.querySelector("button")).toBeNull();
});

test("a missing CLI and a billing refusal each say their own thing", () => {
  expect(showError("cli_missing").card.textContent).toContain("Claude Code was not found");
  cleanup();
  expect(showError("billing").card.textContent).toContain(
    "This session could not run on your subscription",
  );
});

test("a code we do not know still says something true", () => {
  const { card } = showError("internal", "asyncio.CancelledError");
  expect(card.textContent).toContain("Reply failed");
  expect(card.textContent).toContain("asyncio.CancelledError");
});

test("the CLI's own message is never the headline, and never markup", () => {
  const { card } = showError("crashed", '<img src=x onerror="window.__pwned=1">');
  expect(card.querySelector("img")).toBeNull();
  const title = card.querySelector(".error-title") as HTMLElement;
  expect(title.textContent).toBe("Claude Code ended unexpectedly");
});

test("a refused turn names the reset time in the owner's own timezone", () => {
  const at = new Date(2026, 8, 12, 21, 0);
  const { container } = render(
    <RateLimitCard
      data={{ status: "rejected", kind: "five_hour", resets_at: at.getTime() / 1000 }}
    />,
  );
  expect(container.querySelector(".chip")?.textContent).toBe("Error");
  expect(container.textContent).toContain("Usage limit reached");
  expect(container.textContent).toMatch(/Usage resets at (?:9\/12 )?21:00\./);
});

test("a refusal with no reset time does not invent one", () => {
  const { container } = render(
    <RateLimitCard data={{ status: "rejected", kind: "seven_day", resets_at: 0 }} />,
  );
  expect(container.textContent).toContain("Wait a while, then send again.");
  expect(container.textContent).not.toContain("Usage resets at");
});

// The live run's case: the reply arrived, and the CLI said only that the window is getting
// close. A card that says "limit reached" here tells the user their subscription is spent when it
// is not, and tells them to resend a turn that already worked — advice to spend the very quota
// being rationed. It must not wear the Error chip either: nothing failed.
test("a warning says the limit is near, not reached, and is not dressed as an error", () => {
  const at = new Date(2026, 8, 12, 21, 0);
  const { container } = render(
    <RateLimitCard
      data={{ status: "allowed_warning", kind: "seven_day", resets_at: at.getTime() / 1000 }}
    />,
  );
  expect(container.querySelector(".chip")?.textContent).toBe("Notice");
  expect(container.querySelector(".error-card")?.className).toContain("notice");
  expect(container.textContent).toContain("Approaching the usage limit");
  expect(container.textContent).not.toContain("limit reached");
  expect(container.textContent).toMatch(/Usage resets at (?:9\/12 )?21:00\./);
});

test("a warning with no reset time never tells the owner to resend what worked", () => {
  const { container } = render(
    <RateLimitCard data={{ status: "allowed_warning", kind: "five_hour", resets_at: 0 }} />,
  );
  expect(container.textContent).toContain("You can continue, but the limit is near.");
  expect(container.textContent).not.toContain("send again");
});

const blocked = (over: Partial<SessionBlockedData["holder"]> = {}): SessionBlockedData => ({
  reason: "live_elsewhere",
  holder: {
    session_id: "s9",
    entrypoint: "claude-vscode",
    pid: 20412,
    title: "retro H1–H4",
    ...over,
  },
});

test("an already-open session says who has it and why we will not join", () => {
  const onFork = vi.fn();
  const onClose = vi.fn();
  const { container } = render(<BlockedCard data={blocked()} onFork={onFork} onClose={onClose} />);
  expect(container.querySelector(".chip")?.textContent).toBe("In use");
  expect(container.textContent).toContain("VS Code · pid 20412");
  expect(container.textContent).toContain("This conversation is still open in VS Code");
  expect(container.textContent).toContain(
    "Writing from both places can corrupt the record, so continue in a fork here.",
  );
  fireEvent.click(screen.getByText("Continue in a fork"));
  expect(onFork).toHaveBeenCalledTimes(1);
  expect(onClose).not.toHaveBeenCalled();
});

test("a fork can only be started once: a double click does not open two sessions", () => {
  const onFork = vi.fn();
  render(<BlockedCard data={blocked()} onFork={onFork} onClose={vi.fn()} />);
  const button = screen.getByText("Continue in a fork");
  fireEvent.click(button);
  fireEvent.click(button);
  expect(onFork).toHaveBeenCalledTimes(1);
});

/* AMENDMENT (2026-09-13). The fixtures below
   said a terminal session's entrypoint is "claude-cli". No such value exists: catalog.py's
   MAIN_ENTRYPOINTS is {"claude-vscode", "cli", "viki-agent"} and the terminal writes `cli`, so
   every one of these tests was proving the rule against a string the server never sends — and a
   real terminal session took the else-branch. What each test asserts is unchanged, and the intent
   was right all along ("a terminal session is not called VS Code"); only the value it was tested
   with moved to the one the server actually writes. An unfamiliar entrypoint is now also checked
   here, because that is the branch the wrong value had been silently exercising. */
test("the CLI's holder is named by the same rule the session rows use", () => {
  const { container } = render(
    <BlockedCard data={blocked({ entrypoint: "cli" })} onFork={vi.fn()} onClose={vi.fn()} />,
  );
  expect(container.textContent).toContain("CLI · pid 20412");
  expect(container.textContent).toContain("This conversation is still open in CLI");
});
