import { expect, test, vi } from "vitest";
import { DurableApiError } from "../clients/conversations/durableApi";
import { FAKE_GENERATION, fakeDurable } from "../testDurable";
import { createEditorStore } from "./editors";

test("a failed clear cannot turn an already accepted prompt into a fresh command", async () => {
  const api = fakeDurable();
  vi.spyOn(api, "clearDraft").mockRejectedValue(new DurableApiError(503, "unavailable"));
  const store = createEditorStore(api, { storage: null });
  const key = crypto.randomUUID();
  store.edit(key, "accepted text");
  await store.ensure(key);
  expect(await store.submit(key, FAKE_GENERATION)).not.toBeNull();
  expect(store.getSnapshot().editors[key]?.text).toBe("accepted text");
  await store.submit(key, FAKE_GENERATION);
  expect(api.submissions).toHaveLength(1);
});
