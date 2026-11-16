import { expect, test, vi } from "vitest";
import { parseReadOnlyArchive } from "../../context/conversationTools";
import { createChatArchivesApi } from "./chatArchivesApi";

const id = "11111111-1111-4111-8111-111111111111";
const wire = {
  format_version: 1,
  kind: "viki-conversation-archive",
  read_only: true,
  session: { session_id: id, title: "Notes" },
  turns: [{ turn_id: "h-start-0", role: "user", text: "Tokyo" }],
  media: [],
};

test("archive client unwraps a saved archive and sends only the archive route", async () => {
  const request = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    expect(String(url)).toContain("/api/v2/conversation-archives");
    expect(init?.headers).toMatchObject({ authorization: "Bearer secret" });
    if (init?.method === "DELETE") return new Response(null, { status: 204 });
    if (init?.method === "POST") {
      expect(JSON.parse(String(init.body))).toEqual({ archive_id: id, archive: wire });
      return Response.json({ archive_id: id });
    }
    return Response.json({
      archive_id: id,
      archive: wire,
      byte_size: 200,
      created_at: "2026-09-19T00:00:00.000Z",
    });
  });
  const api = createChatArchivesApi("secret", request);
  await api.save(id, parseReadOnlyArchive(wire));
  expect((await api.get(id)).turns[0]?.text).toBe("Tokyo");
  await api.remove(id);
  expect(request).toHaveBeenCalledTimes(3);
});

test("archive save failure is not retried and does not dispatch a session", async () => {
  const request = vi.fn(async () => new Response(null, { status: 409 }));
  await expect(
    createChatArchivesApi("secret", request).save(id, parseReadOnlyArchive(wire)),
  ).rejects.toThrow("conflict");
  expect(request).toHaveBeenCalledOnce();
});
