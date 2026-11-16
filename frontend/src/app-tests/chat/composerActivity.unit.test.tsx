import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki, GENERATION } from "../../accept/m3b/fakes";
import type { Envelope, EventMap } from "../../protocol";
import { fakeDurable } from "../../testDurable";

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
});

test("composer activity follows observed selected-session work, questions for the user, terminal events and connection", async () => {
  const rest = fakeRest();
  const { viki, socket } = fakeViki();
  const { container } = render(<App viki={viki} rest={rest} durable={fakeDurable()} />);
  const state = () => container.querySelector(".composer-wrap")?.getAttribute("data-activity");
  const label = () => screen.getByRole("status", { name: "Chat activity" }).textContent;
  expect(state()).toBe("offline");
  act(() => socket.open());
  await waitFor(() => expect(rest.calls).toContain("rails"));
  expect(state()).toBe("idle");
  fireEvent.click(screen.getByText("New session"));
  const opened = socket.sent.findLast((item) => item.type === "session.open") as {
    session_id: string;
  };
  let seq = 0;
  function emit<K extends keyof EventMap>(type: K, data: EventMap[K]) {
    act(() =>
      socket.receive({
        v: 2,
        generation: GENERATION,
        request_id: "",
        session_id: opened.session_id,
        seq: ++seq,
        type,
        data,
      } as Envelope),
    );
  }
  emit("turn.started", { turn_id: "t1" });
  expect(state()).toBe("active");
  expect(label()).toContain("Writing");
  emit("tool.started", {
    block_id: "tool1",
    tool: "Grep",
    row: { head: "Grep", rest: "loading-kit", tail: "" },
    parent_id: "",
  });
  expect(label()).toContain("Using Grep");
  emit("tool.done", { block_id: "tool1", is_error: false, summary: "", preview: "", tail: "" });
  expect(label()).toContain("Writing");
  emit("subagent.started", {
    block_id: "agent1",
    agent: "researcher",
    model: "sonnet",
    description: "",
    reason: "",
  });
  expect(label()).toContain("Working with an agent");
  emit("subagent.done", { block_id: "agent1", status: "completed" });
  expect(label()).toContain("Writing");
  emit("thinking.delta", { block_id: "thinking", text: "Synthetic thinking" });
  expect(label()).toContain("Thinking");
  emit("question.asked", { tool_use_id: "question", questions: [] });
  expect(state()).toBe("waiting");
  expect(label()).toContain("Waiting for your answer");
  emit("turn.done", {
    status: "success",
    usage: { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_creation_tokens: 0 },
    context_percent: null,
    duration_ms: 10,
  });
  expect(state()).toBe("idle");
  emit("permission.requested", {
    tool_use_id: "plan1",
    kind: "plan",
    tool: "ExitPlanMode",
    summary: "",
    question: "",
    reason: "",
    agent: "",
    plan: "the plan",
  });
  expect(state()).toBe("waiting");
  expect(label()).toContain("Waiting for plan approval");
  emit("turn.started", { turn_id: "t2" });
  emit("turn.done", {
    status: "error",
    usage: { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_creation_tokens: 0 },
    context_percent: null,
    duration_ms: 10,
  });
  expect(state()).toBe("error");
  expect(label()).toContain("Reply failed");
  emit("turn.started", { turn_id: "t3" });
  expect(state()).toBe("active");
  act(() => viki.store.setStatus("reconnecting"));
  expect(state()).toBe("offline");
  expect(label()).toContain("Reconnecting");
  act(() => viki.store.setStatus("open"));
  fireEvent.click(screen.getByText("New session"));
  expect(state()).toBe("idle");
  expect(label()).not.toContain("Reply in progress");
  expect(container.querySelector(".reply .activity-orb")).toBeNull();
  viki.client.close();
});
