/**
 * The chip's menu by keyboard: ArrowDown from the trigger's opening reaches the effort in
 * effect, and Radix marks it highlighted.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { ModelChip } from "./ModelChip";

afterEach(cleanup);

const MODELS = [
  { value: "default", label: "Default", description: "recommended" },
  { value: "sonnet", label: "Sonnet 5", description: "routine tasks" },
];

test("ArrowDown from the opened menu reaches the effort in effect, highlighted", async () => {
  render(
    <ModelChip
      models={MODELS}
      model="sonnet"
      effort="xhigh"
      running={false}
      onModel={vi.fn()}
      onEffort={vi.fn()}
    />,
  );
  const trigger = screen.getByRole("button");
  trigger.focus();
  fireEvent.keyDown(trigger, { key: "ArrowDown" });
  const items = screen.getAllByRole("menuitem");
  const target = document.body.querySelector(".chip-effort.on") as HTMLElement;
  expect(target).not.toBeNull();
  // Radix moves focus in a zero-delay timeout after each arrow key, so flush it per press.
  for (let i = 0; i < items.length && document.activeElement !== target; i += 1) {
    fireEvent.keyDown(document.activeElement ?? document.body, { key: "ArrowDown" });
    await act(() => new Promise((resolve) => setTimeout(resolve, 0)));
  }
  expect(document.activeElement).toBe(target);
  expect(target.hasAttribute("data-highlighted")).toBe(true);
});

test("long model labels retain their complete identity in trigger and menu titles", () => {
  const model = `viki-llm/11111111-1111-4111-8111-111111111111/${"m".repeat(512)}`;
  const label = `Connection ${"n".repeat(70)} · ${"m".repeat(512)}`;
  render(
    <ModelChip
      models={[{ value: model, label, description: "https://models.example/v1" }]}
      model={model}
      effort="high"
      running={false}
      onModel={vi.fn()}
      onEffort={vi.fn()}
    />,
  );
  expect(screen.getByRole("button").title).toContain(model);
  fireEvent.click(screen.getByRole("button"));
  expect(screen.getAllByRole("menuitem")[0]?.title).toBe(model);
});
