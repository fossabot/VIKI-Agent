import { expect, test } from "vitest";
import { createSessionMetadataApi } from "./sessionMetadataApi";

const SESSION = "11111111-1111-4111-8111-111111111111";
const title = String.fromCodePoint(0x1f600).repeat(256);
const row = {
  session_id: SESSION,
  revision: 1,
  title,
  pinned: false,
  archived: true,
  unread: false,
  color: null,
};

test("a legal Unicode title does not make archived session metadata unreadable", async () => {
  const ordinary = { ...row, session_id: "22222222-2222-4222-8222-222222222222", title: "Chat" };
  const api = createSessionMetadataApi("synthetic", {
    fetch: async () => new Response(JSON.stringify({ sessions: [ordinary, row] })),
  });
  await expect(api.list()).resolves.toEqual([ordinary, row]);
});

test("a title accepted by the server remains a valid metadata update response", async () => {
  const api = createSessionMetadataApi("synthetic", {
    fetch: async () => new Response(JSON.stringify(row)),
  });
  await expect(api.patch(SESSION, 0, { title, archived: true })).resolves.toEqual(row);
});

test("archiving an existing long title accepts the committed update response", async () => {
  const updated = { ...row, revision: 2 };
  const api = createSessionMetadataApi("synthetic", {
    fetch: async () => new Response(JSON.stringify(updated)),
  });
  await expect(api.patch(SESSION, 1, { archived: true })).resolves.toEqual(updated);
});

test("Unicode titles still obey the server's 256-character limit", async () => {
  const api = createSessionMetadataApi("synthetic", {
    fetch: async () => new Response(JSON.stringify({ sessions: [{ ...row, title: `${title}x` }] })),
  });
  await expect(api.list()).rejects.toMatchObject({ code: "invalid_response" });
});
