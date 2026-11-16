import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki, GENERATION, sessionRow } from "../../accept/m3b/fakes";
import { DurableApiError } from "../../clients/conversations/durableApi";
import { fakeDurable } from "../../testDurable";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  sessionStorage.clear();
});

const SOURCE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
async function forkEditor() {
  const api = fakeDurable();
  const { viki, socket } = fakeViki();
  render(
    <App
      viki={viki}
      rest={fakeRest({ sessions: [sessionRow({ session_id: SOURCE, title: "Source" })] })}
      durable={api}
    />,
  );
  await act(async () => {
    socket.open();
  });
  await act(async () => {
    fireEvent.click(screen.getByText("Source"));
  });
  act(() =>
    socket.receive({
      v: 2,
      type: "session.blocked",
      session_id: SOURCE,
      generation: GENERATION,
      request_id: "",
      seq: 2,
      data: {
        reason: "live_elsewhere",
        holder: { session_id: SOURCE, entrypoint: "cli", pid: 123, title: "Source" },
      },
    }),
  );
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Continue in a fork" }));
  });
  fireEvent.change(screen.getByRole("textbox", { name: "Message" }), {
    target: { value: "fork draft" },
  });
  return { api, viki, socket };
}

test("source closure leaves the independent fork draft visible", async () => {
  const { viki } = await forkEditor();
  act(() => viki.store.remove(SOURCE));
  expect((screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement).value).toBe(
    "fork draft",
  );
  viki.client.close();
});

test("a lost fork receipt can adopt the already-created target on the next explicit send", async () => {
  const { api, viki } = await forkEditor();
  const create = viki.actions.createFork;
  const spy = vi.spyOn(viki.actions, "createFork").mockImplementationOnce(async (source, key) => {
    await create(source, key);
    return null;
  });
  await act(async () => {
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "Enter" });
  });
  expect(api.submissions).toHaveLength(0);
  await act(async () => {
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "Enter" });
  });
  expect(api.submissions).toHaveLength(1);
  expect(spy).toHaveBeenCalledTimes(1);
  viki.client.close();
});

test("an unsaved deletion still guards closing the window", async () => {
  vi.useFakeTimers();
  const api = fakeDurable();
  const { viki, socket } = fakeViki();
  render(<App viki={viki} rest={fakeRest()} durable={api} />);
  await act(async () => {
    socket.open();
  });
  const box = screen.getByRole("textbox", { name: "Message" });
  fireEvent.change(box, { target: { value: "saved draft" } });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(401);
  });
  expect([...api.drafts.values()][0]?.text).toBe("saved draft");
  fireEvent.change(box, { target: { value: "" } });
  const event = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(event);
  expect(event.defaultPrevented).toBe(true);
  viki.client.close();
});

test.each([
  [
    "unsupported_attachment",
    "Can't send because the attached image doesn't meet the size or format requirements. Remove the attachment, or replace it with an image that meets them.",
  ],
  [
    "attachment_unavailable",
    "Can't send because the attached image is no longer on this PC. Remove the attachment, or replace the image.",
  ],
] as const)(
  "a %s dispatch refusal keeps the draft and shows its own removal/replacement copy",
  async (code, sentence) => {
    // unsupported_attachment/attachment_unavailable are dispatch refusals like turn_running
    // -- the draft PUT already committed, so the status region must say why the send did not
    // happen, with the sentence specific to the refusal code.
    const rest = fakeRest({ sessions: [sessionRow({ session_id: "s1", title: "vault" })] });
    const { viki, socket } = fakeViki();
    const durable = fakeDurable();
    durable.submitCommand = async (input) => {
      durable.submissions.push(input);
      throw new DurableApiError(400, code);
    };
    render(<App durable={durable} viki={viki} rest={rest} />);
    await act(async () => {
      socket.open();
    });
    await act(async () => {
      fireEvent.click(screen.getByText("vault"));
    });
    const box = screen.getByRole("textbox", { name: "Message" });
    fireEvent.change(box, { target: { value: "Send with an attachment" } });
    await act(async () => {
      fireEvent.keyDown(box, { key: "Enter" });
    });
    expect(document.querySelector(".draft-status")?.textContent).toContain(sentence);
    expect((box as HTMLTextAreaElement).value).toBe("Send with an attachment");
    viki.client.close();
  },
);

test("a draft save refused as attachment_unavailable shows removal/replacement copy, not the generic one", async () => {
  // The draft PUT can answer 409 attachment_unavailable for a save whose image
  // expired. The editor keeps its buffer; only the copy is specific.
  vi.useFakeTimers();
  const durable = fakeDurable();
  durable.saveDraft = async () => {
    throw new DurableApiError(409, "attachment_unavailable");
  };
  const { viki, socket } = fakeViki();
  render(<App viki={viki} rest={fakeRest()} durable={durable} />);
  await act(async () => {
    socket.open();
  });
  const box = screen.getByRole("textbox", { name: "Message" });
  fireEvent.change(box, { target: { value: "saved draft" } });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(401);
  });
  expect(document.querySelector(".draft-status")?.textContent).toContain(
    "The attached image is no longer on this PC",
  );
  expect(document.querySelector(".draft-status")?.textContent).not.toContain(
    "Can't save the draft. The text stays in this window.",
  );
  viki.client.close();
});

test("wrap-up preserves an existing editor instead of overwriting it", async () => {
  const api = fakeDurable();
  const { viki, socket } = fakeViki();
  render(<App viki={viki} rest={fakeRest()} durable={api} />);
  await act(async () => {
    socket.open();
  });
  fireEvent.change(screen.getByRole("textbox", { name: "Message" }), {
    target: { value: "my unsent work" },
  });
  fireEvent.click(screen.getByLabelText("Session actions"));
  await act(async () => {
    fireEvent.click(screen.getByRole("menuitem"));
  });
  expect((screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement).value).toBe(
    "my unsent work",
  );
  expect(api.submissions).toHaveLength(0);
  viki.client.close();
});
