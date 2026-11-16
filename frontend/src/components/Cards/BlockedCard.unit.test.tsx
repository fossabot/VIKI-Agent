import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { SessionBlockedData } from "../../protocol";
import { BlockedCard } from "./BlockedCard";

afterEach(cleanup);

const blocked: SessionBlockedData = {
  reason: "live_elsewhere",
  holder: {
    session_id: "synthetic-blocked",
    entrypoint: "Synthetic Provider <img src=x>",
    pid: 20412,
    title: "A user's own title",
  },
};

test("blocked guidance preserves the literal unknown holder and does not invoke an action", () => {
  const onFork = vi.fn();
  const onClose = vi.fn();
  const { container } = render(<BlockedCard data={blocked} onFork={onFork} onClose={onClose} />);
  expect(container.querySelector(".chip")?.textContent).toBe("In use");
  expect(container.querySelector(".blocked-holder")?.textContent).toBe(
    "Synthetic Provider <img src=x> · pid 20412",
  );
  expect(container.querySelector(".card-question")?.textContent).toBe(
    "This conversation is still open in Synthetic Provider <img src=x>",
  );
  expect(container.querySelector(".blocked-line")?.textContent).toBe(
    "Writing from both places can corrupt the record, so continue in a fork here.",
  );
  expect(container.querySelector("img")).toBeNull();
  expect(onFork).not.toHaveBeenCalled();
  expect(onClose).not.toHaveBeenCalled();
});

test.each(["Continue in a fork", "Close"])("%s remains an explicit one-shot action", (label) => {
  const onFork = vi.fn();
  const onClose = vi.fn();
  render(<BlockedCard data={blocked} onFork={onFork} onClose={onClose} />);
  fireEvent.click(screen.getByRole("button", { name: label }));
  fireEvent.click(screen.getByRole("button", { name: "Continue in a fork" }));
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  expect([onFork.mock.calls.length, onClose.mock.calls.length]).toEqual(
    label === "Close" ? [0, 1] : [1, 0],
  );
  expect(
    (screen.getByRole("button", { name: "Continue in a fork" }) as HTMLButtonElement).disabled,
  ).toBe(true);
  expect((screen.getByRole("button", { name: "Close" }) as HTMLButtonElement).disabled).toBe(true);
});
