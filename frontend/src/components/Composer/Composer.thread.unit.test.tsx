/**
 * Pins ComposerProps' five host-shaping props (P5, slice bot-thread-identity): placeholder,
 * maxLength, sendLabel, activityLead and recipient. Each is optional and inert when omitted —
 * Send stays "Send", the ActivityOrb renders, and the placeholder keeps its identity-driven
 * default. App's own chat path never passes them yet; this file is the only place that
 * exercises them.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { Composer } from "./Composer";

afterEach(cleanup);

test("the five host props are inert when omitted: Send stays Send, the orb renders, and the placeholder keeps its old text", () => {
  const { container } = render(
    <Composer running={false} onSend={vi.fn(async () => true)} onStop={vi.fn()} />,
  );
  expect(container.querySelector('[aria-label="Send"]')).toBeTruthy();
  expect(container.querySelectorAll(".activity-orb")).toHaveLength(1);
  expect(container.querySelector("textarea")?.placeholder).toBe("Talk to VIKI… / for skills");
});

test("placeholder replaces the textarea's default text", () => {
  const { container } = render(
    <Composer
      running={false}
      onSend={vi.fn(async () => true)}
      onStop={vi.fn()}
      placeholder="Ask your team"
    />,
  );
  expect(container.querySelector("textarea")?.placeholder).toBe("Ask your team");
});

test("maxLength sets the textarea's maxLength, and leaves it uncapped when omitted", () => {
  const onSend = vi.fn(async () => true);
  const onStop = vi.fn();
  const { container, rerender } = render(
    <Composer running={false} onSend={onSend} onStop={onStop} maxLength={120} />,
  );
  expect(container.querySelector("textarea")?.getAttribute("maxlength")).toBe("120");
  rerender(<Composer running={false} onSend={onSend} onStop={onStop} />);
  expect(container.querySelector("textarea")?.getAttribute("maxlength")).toBeNull();
});

test("sendLabel replaces the Send button's aria-label", () => {
  const { container } = render(
    <Composer
      running={false}
      onSend={vi.fn(async () => true)}
      onStop={vi.fn()}
      sendLabel="Send to VIKI"
    />,
  );
  expect(container.querySelector('[aria-label="Send to VIKI"]')).toBeTruthy();
  expect(container.querySelector('[aria-label="Send"]')).toBeNull();
});

test("activityLead renders in composer-activity in place of the ActivityOrb", () => {
  const { container } = render(
    <Composer
      running={false}
      onSend={vi.fn(async () => true)}
      onStop={vi.fn()}
      activityLead={<span data-testid="custom-lead" />}
    />,
  );
  const status = screen.getByRole("status", { name: "Chat activity" });
  expect(status.firstElementChild).toBe(screen.getByTestId("custom-lead"));
  expect(container.querySelector(".activity-orb")).toBeNull();
});

test("recipient renders first in composer-actions, ahead of mcpControl, and only when given", () => {
  const onSend = vi.fn(async () => true);
  const onStop = vi.fn();
  const mcp = <button type="button" data-testid="mcp-trigger" />;
  const { container, rerender } = render(
    <Composer
      running={false}
      onSend={onSend}
      onStop={onStop}
      recipient={<span data-testid="recipient-chip" />}
      mcpControl={mcp}
    />,
  );
  const actions = container.querySelector(".composer-actions");
  expect(actions?.firstElementChild).toBe(screen.getByTestId("recipient-chip"));
  expect(actions?.children[1]).toBe(screen.getByTestId("mcp-trigger"));

  rerender(<Composer running={false} onSend={onSend} onStop={onStop} mcpControl={mcp} />);
  expect(container.querySelector(".composer-actions")?.firstElementChild).toBe(
    screen.getByTestId("mcp-trigger"),
  );
});
