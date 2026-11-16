// @ts-expect-error no @types/node in this browser-focused tsconfig; Node itself is present
// under Vitest, which runs tests through Node, not a bundled browser.
import { createHash } from "node:crypto";
import { expect, test, vi } from "vitest";
import { emptyView, type Turn } from "../state/view";
import {
  ArchiveParseError,
  deriveArchiveId,
  exportConversationArchive,
  exportConversationMarkdown,
  parseReadOnlyArchiveJson,
  quoteTurn,
  type ReadOnlyConversationArchive,
  selectedArchive,
  withArchiveMedia,
} from "./conversationTools";

const archive: ReadOnlyConversationArchive = {
  formatVersion: 1,
  kind: "viki-conversation-archive",
  readOnly: true,
  media: [],
  session: { sessionId: "11111111-1111-4111-8111-111111111111", title: "A chat" },
  turns: [
    {
      sessionId: "11111111-1111-4111-8111-111111111111",
      turnId: "22222222-2222-4222-8222-222222222222",
      role: "user",
      text: "hello\nworld",
    },
  ],
};

test("quote preserves line boundaries and stable source target", () => {
  const first = archive.turns[0];
  if (!first) throw new Error("fixture");
  const target = { sessionId: archive.session.sessionId, turnId: first.turnId };
  expect(quoteTurn(target, "a\r\nb")).toEqual({ text: "> a\n> b", source: target });
});

test("selected export excludes unselected turns, thoughts and tool output", () => {
  const make = (id: string): Turn => ({
    id,
    user: { text: "A question", at: "", command: "" },
    blocks: [
      { kind: "text", id: "answer", text: "An answer", done: true },
      { kind: "thinking", id: "private", text: "private reasoning", done: true, durationMs: 1 },
    ],
    status: "success",
    history: true,
    usage: null,
    durationMs: 0,
  });
  const view = {
    ...emptyView(archive.session.sessionId),
    turns: [make("h-start-0"), make("h-start-1")],
  };
  const exported = selectedArchive(view, new Set(["h-start-0"]), "A chat");
  expect(exported.turns).toHaveLength(2);
  expect(exportConversationArchive(exported)).not.toContain("private reasoning");
  expect(
    parseReadOnlyArchiveJson(exportConversationArchive(exported)).turns.map((turn) => turn.turnId),
  ).toEqual(["h-start-0", "h-start-0"]);
});
function promptTurn(id: string, userCut: boolean): Turn {
  return {
    id,
    user: { text: "src/app.py", at: "", command: "/review" },
    blocks: [{ kind: "text", id: "answer", text: "reviewed", done: true }],
    status: "success",
    history: false,
    usage: null,
    durationMs: 0,
    ...(userCut ? { userCut: true } : {}),
  };
}

test("a selected whole prompt is exported rejoined with its slash command", () => {
  const view = { ...emptyView(archive.session.sessionId), turns: [promptTurn("t1", false)] };
  const exported = selectedArchive(view, new Set(["t1"]), "A chat");
  expect(exported.turns[0]).toMatchObject({ role: "user", text: "/review src/app.py" });
  expect(parseReadOnlyArchiveJson(exportConversationArchive(exported)).turns[0]?.text).toBe(
    "/review src/app.py",
  );
  expect(exportConversationMarkdown(exported)).toContain("/review src/app.py");
});

test("a selection holding a cut prompt is refused by both exports, never written as whole", async () => {
  const view = {
    ...emptyView(archive.session.sessionId),
    turns: [promptTurn("t1", false), promptTurn("t2", true)],
  };
  const exported = selectedArchive(view, new Set(["t1", "t2"]), "A chat");
  expect(() => exportConversationArchive(exported)).toThrow(ArchiveParseError);
  expect(() => exportConversationMarkdown(exported)).toThrow(ArchiveParseError);
  await expect(withArchiveMedia(exported, { upload: vi.fn(), read: vi.fn() })).rejects.toThrow(
    ArchiveParseError,
  );
});

test("a cut prompt left unselected does not refuse the selection", () => {
  const view = {
    ...emptyView(archive.session.sessionId),
    turns: [promptTurn("t1", false), promptTurn("t2", true)],
  };
  const exported = selectedArchive(view, new Set(["t1"]), "A chat");
  expect(() => exportConversationArchive(exported)).not.toThrow();
  expect(() => exportConversationMarkdown(exported)).not.toThrow();
});

test("JSON and Markdown export preserve read-only provenance", () => {
  const parsed = parseReadOnlyArchiveJson(exportConversationArchive(archive));
  expect(parsed).toEqual(archive);
  expect(exportConversationMarkdown(archive)).toContain("> Read-only archive");
});
test("archive parser rejects duplicate IDs, executable fields are not interpreted, and byte bound applies", () => {
  const first = archive.turns[0];
  if (!first) throw new Error("fixture");
  const wireTurn = { turn_id: first.turnId, role: "user", text: "hello" };
  expect(() =>
    parseReadOnlyArchiveJson(
      JSON.stringify({
        format_version: 1,
        kind: "viki-conversation-archive",
        read_only: true,
        session: { session_id: archive.session.sessionId, title: archive.session.title },
        turns: [wireTurn, wireTurn],
      }),
    ),
  ).toThrow(/duplicate/);
  expect(() =>
    parseReadOnlyArchiveJson(
      JSON.stringify({ ...archive, turns: [{ ...archive.turns[0], text: "x".repeat(1_100_000) }] }),
    ),
  ).toThrow();
  expect(
    parseReadOnlyArchiveJson(
      JSON.stringify({ ...JSON.parse(exportConversationArchive(archive)), command: "send" }),
    ).readOnly,
  ).toBe(true);
});
test("deriveArchiveId matches an independently computed vector and stays stable across calls", async () => {
  const bytes = new TextEncoder().encode("archive-fixture-bytes").buffer;
  const digest = createHash("sha256").update(new Uint8Array(bytes)).digest();
  const id16 = digest.subarray(0, 16);
  id16[6] = (id16[6] & 0x0f) | 0x40;
  id16[8] = (id16[8] & 0x3f) | 0x80;
  const hex = id16.toString("hex");
  const expected = `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  const id = await deriveArchiveId(bytes);
  expect(id).toBe(expected);
  expect(id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  await expect(deriveArchiveId(bytes)).resolves.toBe(id);
});
test("deriveArchiveId matches a hard-coded expected UUID for a fixed input", async () => {
  const id = await deriveArchiveId(new TextEncoder().encode("archive-fixture-bytes").buffer);
  expect(id).toBe("d7321539-c4cf-4544-8de6-6bec58c3f0bc");
});
test("deriveArchiveId differs for different bytes", async () => {
  const a = await deriveArchiveId(new TextEncoder().encode("one").buffer);
  const b = await deriveArchiveId(new TextEncoder().encode("two").buffer);
  expect(a).not.toBe(b);
});
