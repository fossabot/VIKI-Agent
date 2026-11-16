/**
 * Unit tests for the error codes "forced_stop" and "provider_disconnected": both are
 * additive members of ErrorCode (protocol.ts), so the frozen `m2b3/errors.accept.test.tsx`,
 * written before either existed, never renders them and cannot pin them. This file does five
 * things that suite cannot: proves neither new code ever grows a button — the render rule stays
 * `code === "crashed" && recoverable && onRetry`, and forced_stop in particular carries
 * recoverable=true in real payloads (events.py), the one value that could make a code-blind
 * version of that rule fire by accident; proves `message` stays out of the DOM for every
 * fixed-copy code, not just the four the frozen suite already covers; proves the one branch that
 * DOES render `message` — the unknown-code fallback — never renders it as markup; proves a code
 * this build has never heard of at all (not merely one with no fixed copy) still renders
 * something true, per this file's header comment; and pins "transcript_missing stays on the
 * fallback", a contract clause nothing else in the repo renders.
 */
import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { ErrorCode } from "../../protocol";
import { ErrorCard } from "./ErrorCard";

afterEach(cleanup);

function showError(
  code: ErrorCode,
  recoverable: boolean,
  onRetry?: () => void,
  message = "stderr tail: only ever shown for a code with no fixed copy",
): HTMLElement {
  // Spread rather than `onRetry={onRetry}`: under exactOptionalPropertyTypes, passing the
  // literal value `undefined` for an optional prop is a type error distinct from omitting the
  // prop — and omitting it is what every real caller does when there is nothing to retry.
  const { container } = render(
    <ErrorCard data={{ code, message, recoverable }} {...(onRetry ? { onRetry } : {})} />,
  );
  return container.querySelector(".error-card") as HTMLElement;
}

test("forced_stop names the kill and what to check, with no button even though it is recoverable and a retry is offered", () => {
  const card = showError("forced_stop", true, vi.fn());
  expect(card.querySelector(".chip")?.textContent).toBe("Error");
  expect(card.textContent).toContain("Claude Code was terminated because it could not be stopped");
  expect(card.textContent).toContain(
    "Any work already done remains in place. Check the conversation record and changes before sending your next message.",
  );
  expect(card.querySelector("button")).toBeNull();
});

test("provider_disconnected says the send is unconfirmed, with no button even if recoverable were true", () => {
  // Real payloads always carry recoverable=false for this code (session.py); it is forced true
  // here to prove the button rule keys off `code`, not `recoverable` alone.
  const card = showError("provider_disconnected", true, vi.fn());
  expect(card.textContent).toContain("Delivery to Claude Code is unconfirmed");
  expect(card.textContent).toContain(
    "Check the send record before deciding what to send next. It will not be sent again automatically.",
  );
  expect(card.querySelector("button")).toBeNull();
});

test("crashed keeps its one button exactly as the frozen contract pins it", () => {
  const onRetry = vi.fn();
  expect(showError("crashed", true, onRetry).querySelector("button")?.textContent).toBe(
    "Send again",
  );
  cleanup();
  expect(showError("crashed", true, undefined).querySelector("button")).toBeNull();
});

test("the crashed line keeps the frozen sentence and adds a follow-up about side effects", () => {
  // Not pinned anywhere else: the frozen assertion is `toContain` on the first sentence alone,
  // so it would still pass with or without this second one.
  const card = showError("crashed", true, vi.fn());
  expect(card.textContent).toContain(
    "If the conversation record is still available, send again to resume from here.",
  );
  expect(card.textContent).toContain("Any work already done remains in place.");
});

test("message is never rendered for a fixed-copy code, old or new", () => {
  const secret = "Traceback (most recent call last): __SECRET_STDERR__";
  for (const code of ["crashed", "forced_stop", "provider_disconnected"] as const) {
    const card = showError(code, true, vi.fn(), secret);
    expect(card.textContent).not.toContain(secret);
    cleanup();
  }
});

test("an unknown code's message renders as literal text, never as markup", () => {
  // Every fixed-copy code above skips `message` outright, so markup inside it could never reach
  // the DOM through them — that would prove nothing about escaping (an earlier version of this
  // check used "forced_stop", a fixed-copy code, and so could not fail). The only branch that
  // renders the server's `message` at all is the unknown-code fallback; this proves THAT branch
  // treats it as literal text rather than passing it through dangerouslySetInnerHTML.
  const markup = '<img src=x onerror="window.__pwned=1">';
  const card = showError("provider_timeout_not_yet_invented" as ErrorCode, true, undefined, markup);
  expect(card.querySelector(".error-line")?.textContent).toBe(markup);
  expect(card.querySelector("img")).toBeNull();
});

test("a code this build has never heard of still says something true, not nothing", () => {
  // "constructor" is inherited from Object.prototype, so ERROR_COPY["constructor"] is NOT
  // undefined the way a known ErrorCode absent from ERROR_COPY is (that case is the frozen
  // suite's own "internal" test) — it looks up the Object constructor function. A merely-unlisted
  // string like "provider_timeout_not_yet_invented" would look up the same as "internal" (both
  // undefined) and prove nothing this file does not already cover; "constructor" instead proves
  // the fallback also holds when the lookup itself is truthy but has no usable title/line.
  const card = showError("constructor" as ErrorCode, true, undefined, "a future stderr tail");
  expect(card.querySelector(".error-title")?.textContent).toBe("Reply failed");
  expect(card.querySelector(".error-line")?.textContent).toBe("a future stderr tail");
  expect(card.querySelector("button")).toBeNull();
});

test("transcript_missing stays on the fallback rather than getting fixed copy", () => {
  // This code is deliberately NOT in ERROR_COPY — its message is already
  // user-facing, and what matters is that it never offers Send again, because there is
  // nothing left to resume. recoverable=true and a real onRetry are supplied anyway, to prove the
  // fallback holds even under the most permissive inputs, not merely because a caller omitted them.
  const message = "This conversation's record could not be found.";
  const card = showError("transcript_missing", true, vi.fn(), message);
  expect(card.querySelector(".error-title")?.textContent).toBe("Reply failed");
  expect(card.querySelector(".error-line")?.textContent).toBe(message);
  expect(card.querySelector("button")).toBeNull();
});

test("the billing refusal names the Settings switch that lets the session run", () => {
  // The line points at the switch added with chat.other_sign_in, so a user who sees this card
  // knows where to look. The title stays frozen by m2b3/errors.accept.test.tsx.
  const card = showError("billing", false);
  expect(card.querySelector(".error-line")?.textContent).toContain(
    "Let Claude Code use its other sign-in methods",
  );
  expect(card.querySelector(".error-line")?.textContent).toContain("Then restart VIKI Agent.");
  // A listed setting also blocks a model-connection chat, and the switch does not help it.
  expect(card.querySelector(".error-line")?.textContent).toContain(
    "A setting listed there also blocks chats on a model connection.",
  );
});
