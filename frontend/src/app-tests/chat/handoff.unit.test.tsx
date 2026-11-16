import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { App } from "../../App";
import { FakeSocket, fakeRest, sessionRow } from "../../accept/m3b/fakes";
import type { CommandInput, CommandReceipt } from "../../clients/conversations/durableApi";
import { createViki } from "../../state/viki";
import { fakeDurable } from "../../testDurable";

afterEach(cleanup);

function manualViki() {
  const socket = new FakeSocket(false);
  const viki = createViki({
    url: "ws://127.0.0.1:8765/ws",
    token: "synthetic",
    createSocket: () => socket,
  });
  viki.client.connect();
  return { socket, viki };
}

function messageBox(): HTMLTextAreaElement {
  return screen.getByRole<HTMLTextAreaElement>("textbox", { name: "Message" });
}

test("a new editor submits its saved revision only after the B4 target synchronizes", async () => {
  const { socket, viki } = manualViki();
  const durable = fakeDurable();
  render(<App viki={viki} rest={fakeRest()} durable={durable} />);
  act(() => socket.open());
  fireEvent.change(messageBox(), { target: { value: "first prompt" } });
  fireEvent.keyDown(messageBox(), { key: "Enter" });
  const open = socket.sent.find((item) => item.type === "session.open");
  expect(open).toBeDefined();
  expect(messageBox().value).toBe("first prompt");
  expect(durable.submissions).toHaveLength(0);

  await act(async () => socket.synchronize(String(open?.session_id)));
  await waitFor(() => expect(durable.submissions).toHaveLength(1));
  const submitted = durable.submissions[0];
  if (!submitted) throw new Error("missing durable submission");
  expect(submitted).toMatchObject({ session_id: open?.session_id, draft_revision: 1 });
  expect(durable.saves).toEqual([
    { key: open?.session_id, expectedRevision: 0, text: "first prompt" },
    { key: open?.session_id, expectedRevision: 1, text: "" },
  ]);
  expect(durable.commands.get(String(submitted?.command_id))?.text).toBe("first prompt");
  expect((await durable.getDraftCommand(String(open?.session_id), 1))?.command_id).toBe(
    submitted?.command_id,
  );
  expect((await durable.getSubmission(String(submitted?.command_id))).text).toBe("first prompt");
  await expect(
    durable.submitCommand({ ...submitted, command_id: crypto.randomUUID() }),
  ).rejects.toMatchObject({ status: 409, code: "draft_already_submitted" });
  expect(durable.commands.size).toBe(1);
  expect(messageBox().value).toBe("");
  expect(socket.sent.filter((item) => item.type === "turn.send")).toEqual([]);
  viki.client.close();
});

test("typing during an unconfirmed durable submission survives with a new draft revision", async () => {
  const { socket, viki } = manualViki();
  const durable = fakeDurable();
  const originalSubmit = durable.submitCommand;
  let started: (input: CommandInput) => void = () => {};
  const submitStarted = new Promise<CommandInput>((resolve) => {
    started = resolve;
  });
  let release: () => void = () => {};
  durable.submitCommand = (input) => {
    started(input);
    return new Promise<CommandReceipt>((resolve, reject) => {
      release = () => {
        void originalSubmit(input).then(resolve, reject);
      };
    });
  };
  render(<App viki={viki} rest={fakeRest()} durable={durable} />);
  act(() => socket.open());
  fireEvent.change(messageBox(), { target: { value: "first prompt" } });
  fireEvent.keyDown(messageBox(), { key: "Enter" });
  const open = socket.sent.find((item) => item.type === "session.open");
  await act(async () => socket.synchronize(String(open?.session_id)));
  const input = await submitStarted;
  expect(messageBox().value).toBe("first prompt");
  fireEvent.change(messageBox(), { target: { value: "new typing" } });
  await act(async () => release());
  await waitFor(() => expect(durable.submissions).toHaveLength(1));
  expect(durable.commands.get(input.command_id)?.text).toBe("first prompt");
  expect(durable.drafts.get(input.session_id)?.text).toBe("new typing");
  expect(durable.drafts.get(input.session_id)?.revision).toBe(2);
  expect(messageBox().value).toBe("new typing");
  expect(socket.sent.filter((item) => item.type === "turn.send")).toEqual([]);
  viki.client.close();
});

test("fork handoff submits the fork editor only after the B4 fork target synchronizes", async () => {
  const { socket, viki } = manualViki();
  const durable = fakeDurable();
  const { container } = render(
    <App
      viki={viki}
      durable={durable}
      rest={fakeRest({
        sessions: [
          sessionRow({ session_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", title: "Fork source" }),
        ],
      })}
    />,
  );
  act(() => socket.open());
  const row = await waitFor(() => {
    const value = container.querySelector<HTMLElement>(
      '[data-session-id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"]',
    );
    if (!value) throw new Error("missing source row");
    return value;
  });
  fireEvent.click(row);
  act(() =>
    socket.synchronize("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", undefined, 0, {
      events: [],
      truncated: false,
      state: {
        live: false,
        info: null,
        mode: "",
        closed: false,
        error: null,
        compacting: false,
        context_percent: null,
        rate_limit: null,
        blocked: {
          reason: "live_elsewhere",
          holder: {
            session_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            entrypoint: "vscode",
            pid: 1,
            title: "Fork source",
          },
        },
      },
    }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Continue in a fork" }));
  fireEvent.change(messageBox(), { target: { value: "fork prompt" } });
  fireEvent.keyDown(messageBox(), { key: "Enter" });
  const fork = await waitFor(() => {
    const sent = socket.sent.find((item) => item.type === "session.fork");
    if (!sent) throw new Error("fork command not sent");
    return sent;
  });
  expect(durable.submissions).toHaveLength(0);
  await act(async () => socket.acknowledge(fork));
  await act(async () => socket.synchronize(String(fork.fork_id)));
  await waitFor(() => expect(durable.submissions).toHaveLength(1));
  const submitted = durable.submissions[0];
  expect(submitted).toMatchObject({ session_id: fork.fork_id, draft_revision: 1 });
  expect(durable.commands.get(String(submitted?.command_id))?.text).toBe("fork prompt");
  expect(durable.drafts.get(String(fork.fork_id))?.text).toBe("");
  expect(messageBox().value).toBe("");
  expect(socket.sent.filter((item) => item.type === "turn.send")).toEqual([]);
  viki.client.close();
});
