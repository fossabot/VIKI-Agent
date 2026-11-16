/**
 * Acceptance: Wrap up and the other-VIKI banner.
 *
 * This is the one place the app puts words in the user's mouth, and the one place a refusal
 * protects the vault rather than the app: CLAUDE.md's session-end commit stages with
 * `git add -A`, so running it while another claude session is working in the vault would sweep
 * that session's half-finished edits into the user's commit.
 */
import { expect, test } from "vitest";
import { vaultBannerText, WRAP_UP_PROMPT, wrapUpState } from "../../components/Header/wrapUp";
import type { OtherSession } from "../../protocol";

const other = (over: Partial<OtherSession> = {}): OtherSession => ({
  session_id: "s9",
  entrypoint: "claude-vscode",
  pid: 20412,
  title: "retro H1–H4",
  ...over,
});

test("the prompt asks for exactly what CLAUDE.md's session protocol requires", () => {
  expect(WRAP_UP_PROMPT).toContain("system/log.md");
  expect(WRAP_UP_PROMPT).toContain("git add -A && git commit");
  // The convention lives in the vault, not here: the prompt points at it rather than restating
  // a commit-message format this app would then have to keep in sync.
  expect(WRAP_UP_PROMPT).toContain("CLAUDE.md");
});

test("with the vault to ourselves, Wrap up is offered with no caveat", () => {
  expect(wrapUpState([])).toEqual({ enabled: true, tooltip: "" });
  expect(vaultBannerText([])).toBe("");
});

test("another session in the vault disables it and the tooltip names who", () => {
  const state = wrapUpState([other()]);
  expect(state.enabled).toBe(false);
  expect(state.tooltip).toContain("VS Code: retro H1–H4");
  expect(state.tooltip).toContain("Use this after they finish");
});

/* AMENDMENT (2026-09-13). The fixtures below
   said a terminal session's entrypoint is "claude-cli". No such value exists: catalog.py's
   MAIN_ENTRYPOINTS is {"claude-vscode", "cli", "viki-agent"} and the terminal writes `cli`, so
   every one of these tests was proving the rule against a string the server never sends — and a
   real terminal session took the else-branch. What each test asserts is unchanged, and the intent
   was right all along ("a terminal session is not called VS Code"); only the value it was tested
   with moved to the one the server actually writes. An unfamiliar entrypoint is now also checked
   here, because that is the branch the wrong value had been silently exercising. */
test("the tooltip names every one of them, and words an unfamiliar entrypoint as it came", () => {
  const state = wrapUpState([
    other(),
    other({ session_id: "s8", entrypoint: "cli", title: "" }),
    other({ session_id: "s7", entrypoint: "sdk-py", title: "capture" }),
  ]);
  expect(state.tooltip).toContain("VS Code: retro H1–H4");
  expect(state.tooltip).toContain("CLI: Untitled session");
  expect(state.tooltip).toContain("sdk-py: capture");
});

test("the banner says where and which conversation, in the approved words", () => {
  const text = vaultBannerText([other()]);
  expect(text).toBe(
    "VIKI is also running in VS Code (retro H1–H4). Wrap up (log + commit) is available after it finishes.",
  );
});

test("a terminal session is not called VS Code, and a second one is counted", () => {
  expect(vaultBannerText([other({ entrypoint: "cli" })])).toContain(
    "VIKI is also running in the terminal",
  );
  // Nor is anything else: this sentence is the only one that asserts a program, so an entrypoint
  // it does not know must not be dressed as one.
  expect(vaultBannerText([other({ entrypoint: "sdk-py" })])).toContain(
    "VIKI is also running in another session",
  );
  expect(vaultBannerText([other(), other({ session_id: "s8" })])).toContain(
    "(retro H1–H4, 1 more)",
  );
});
