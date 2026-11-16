/**
 * Acceptance: the card a window from an earlier launch shows. The token is per launch, so a
 * window left open across a restart can never work again — and it must say so plainly
 * rather than sit there looking alive.
 */
import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { StaleWindow } from "../../components/Shell/StaleWindow";

afterEach(cleanup);

test("it says which window this is and what to do about it", () => {
  const { container } = render(<StaleWindow />);
  const card = container.querySelector(".stale") as HTMLElement;
  expect(card.textContent).toContain("This window belongs to a previous launch.");
  /* AMENDMENT (2026-10-09). The app's name
     is VIKI Agent in every message that says to open it again. */
  expect(card.textContent).toContain("Open VIKI Agent again. Your conversation records are kept.");
});

test("it offers no button: the page cannot mint itself a new token", () => {
  const { container } = render(<StaleWindow />);
  expect(container.querySelector("button")).toBeNull();
  expect(container.querySelector("a")).toBeNull();
});

test("it announces itself without loading the old default portrait", () => {
  const { container } = render(<StaleWindow />);
  expect(container.querySelector(".stale")?.getAttribute("role")).toBe("alert");
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector(".face")).toBeNull();
});
