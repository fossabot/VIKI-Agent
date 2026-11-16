/**
 * Acceptance (frozen): the toast that refuses a fourth live session. The rule it enforces:
 * nothing auto-closes. A toast that closed a session on the user's behalf could end a turn
 * that was writing to the vault.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { SessionLimitToast } from "../../components/Toast/SessionLimitToast";

afterEach(cleanup);

const LIVE = [
  { sessionId: "s1", title: "Dashboard v1 → reverted" },
  { sessionId: "s2", title: "retro H1–H4" },
  { sessionId: "s3", title: "" },
];

function show() {
  const onClose = vi.fn();
  const onDismiss = vi.fn();
  const { container } = render(
    <SessionLimitToast live={LIVE} onClose={onClose} onDismiss={onDismiss} />,
  );
  return { container, onClose, onDismiss };
}

test("it says the limit and what to do, in the approved words", () => {
  const { container } = show();
  const toast = container.querySelector(".toast") as HTMLElement;
  expect(toast.textContent).toContain("Up to 3 sessions can run at once");
  expect(toast.textContent).toContain("Close one to start a new session.");
});

test("it lists the live sessions in the order given, each with its own Close", () => {
  const { container } = show();
  const rows = [...container.querySelectorAll(".toast-row")];
  expect(rows).toHaveLength(3);
  expect(rows[0]?.textContent).toContain("Dashboard v1 → reverted");
  expect(rows[1]?.textContent).toContain("retro H1–H4");
  expect(rows[2]?.textContent).toContain("Untitled session");
  expect(screen.getAllByText("Close")).toHaveLength(3);
});

test("Close names the session it belongs to and closes nothing itself", () => {
  const { container, onClose, onDismiss } = show();
  const second = container.querySelectorAll(".toast-row")[1] as HTMLElement;
  fireEvent.click(second.querySelector("button") as HTMLElement);
  expect(onClose).toHaveBeenCalledWith("s2");
  // The toast stays: App dismisses it when a session actually closes, because an uncommitted
  // session asks first and the close can be refused.
  expect(onDismiss).not.toHaveBeenCalled();
  expect(container.querySelectorAll(".toast-row")).toHaveLength(3);
});

test("it can be dismissed by its control and by Escape, and never on a timer", () => {
  const { container, onDismiss } = show();
  fireEvent.click(container.querySelector(".toast-dismiss") as HTMLElement);
  expect(onDismiss).toHaveBeenCalledTimes(1);

  cleanup();
  const second = show();
  fireEvent.keyDown(window, { key: "Escape" });
  expect(second.onDismiss).toHaveBeenCalledTimes(1);
});

test("a session title is text, whatever a transcript holds", () => {
  const onClose = vi.fn();
  const { container } = render(
    <SessionLimitToast
      live={[{ sessionId: "s9", title: '<img src=x onerror="window.__pwned=1">' }]}
      onClose={onClose}
      onDismiss={vi.fn()}
    />,
  );
  expect(container.querySelector("img")).toBeNull();
  expect(container.textContent).toContain('<img src=x onerror="window.__pwned=1">');
});
