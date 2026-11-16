import { expect, test } from "vitest";
import type { ProposalDetail } from "../../state/decisionsApi";
import { fenceFor, proposalHandoffText, proposalRejectText } from "./handoff";

test.each([
  ["", "```"],
  ["plain", "```"],
  ["a `b` c", "```"],
  ["a ``b`` c", "```"],
  ["```diff\nx\n```", "````"],
  ["`` and ````` and `", "``````"],
])("fenceFor(%j) is %j", (text, fence) => {
  expect(fenceFor(text)).toBe(fence);
});

function detail(over: Partial<ProposalDetail> = {}): ProposalDetail {
  return {
    kind: "vault_proposal",
    id: "p1",
    origin: { workspace_id: "w1", session_id: null },
    revision: `${"b".repeat(12)}${"0".repeat(52)}`,
    state: "open",
    attention: false,
    title: "notes/a.md",
    ref: "notes/a.md",
    origin_kind: "note_edit",
    base_revision: `${"a".repeat(12)}${"0".repeat(52)}`,
    text_bytes: 10,
    handed_at: null,
    created_at: "2026-10-01T00:00:00.000Z",
    current_revision: `${"a".repeat(12)}${"0".repeat(52)}`,
    proposed_text: "new text",
    diff: "--- a/notes/a.md\n+++ b/notes/a.md\n-old\n+new",
    diff_reason: null,
    ...over,
  };
}

test("a proposal hand-off names the note and both hash prefixes, then fences the diff", () => {
  expect(proposalHandoffText(detail())).toBe(
    [
      "Apply the following change to the vault note notes/a.md. Its current sha256 starts with aaaaaaaaaaaa and should start with bbbbbbbbbbbb afterwards. Change nothing outside the diff.",
      "",
      "```diff",
      "--- a/notes/a.md\n+++ b/notes/a.md\n-old\n+new",
      "```",
    ].join("\n"),
  );
});

test("a diff holding three backticks gets a four-backtick fence", () => {
  const text = proposalHandoffText(detail({ diff: "+```js\n+x\n+```" }));
  expect(text.endsWith("+```js\n+x\n+```\n````")).toBe(true);
  expect(text).toContain("\n````diff\n");
});

test("without a diff the hand-off carries the whole proposed text instead of an empty block", () => {
  const text = proposalHandoffText(detail({ diff: "", diff_reason: "too_large" }));
  expect(text).toContain("Replace the vault note notes/a.md with the following content.");
  expect(text.endsWith("\n```\nnew text\n```")).toBe(true);
});

test("a create-only hand-off asks for a new note, names no base and fences the whole text", () => {
  const created = detail({
    origin_kind: "memory_promotion",
    base_revision: null,
    current_revision: null,
    diff: "",
    diff_reason: "no_source",
  });
  expect(proposalHandoffText(created)).toBe(
    [
      "Create the vault note notes/a.md with exactly the following content, and only if no note exists at notes/a.md. Its sha256 should start with bbbbbbbbbbbb afterwards.",
      "",
      "```",
      "new text",
      "```",
    ].join("\n"),
  );
});

test("a proposal reject with a note names the note-edit origin and the reason clause", () => {
  expect(proposalRejectText({ ref: "notes/a.md", origin_kind: "note_edit" }, "stale info")).toBe(
    [
      "Reject the note edit proposal for notes/a.md: stale info",
      "",
      "The app withdrew it, so the note is unchanged. Record this rejection and its reason where /retro will see it.",
    ].join("\n"),
  );
});

test("a proposal reject without a note ends with a period and drops the reason clause", () => {
  expect(proposalRejectText({ ref: "notes/b.md", origin_kind: "memory_promotion" }, "")).toBe(
    [
      "Reject the memory proposal for notes/b.md.",
      "",
      "The app withdrew it, so the note is unchanged. Record this rejection where /retro will see it.",
    ].join("\n"),
  );
});

test("an unrecognized origin kind falls back to '<kind> proposal'", () => {
  expect(proposalRejectText({ ref: "notes/c.md", origin_kind: "custom_origin" }, "")).toBe(
    [
      "Reject the custom_origin proposal for notes/c.md.",
      "",
      "The app withdrew it, so the note is unchanged. Record this rejection where /retro will see it.",
    ].join("\n"),
  );
});
