/**
 * Acceptance (frozen): the model and effort chip. The chip is the one place the user
 * changes what VIKI costs them, so what it offers and when it refuses are pinned here.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { EFFORTS, ModelChip } from "../../components/Composer/ModelChip";
import type { ModelOption } from "../../protocol";

afterEach(cleanup);

const MODELS: ModelOption[] = [
  { value: "default", label: "Default", description: "Opus 5 with 1M context · recommended" },
  { value: "opus", label: "Opus 5", description: "1M context · best for everyday, complex tasks" },
  {
    value: "fable",
    label: "Fable 5.1",
    description: "Most capable for your hardest and longest-running tasks",
  },
  { value: "sonnet", label: "Sonnet 5", description: "Efficient for routine tasks" },
];

function show(over: Partial<Parameters<typeof ModelChip>[0]> = {}) {
  const onModel = vi.fn();
  const onEffort = vi.fn();
  const { container } = render(
    <ModelChip
      models={MODELS}
      model="fable"
      effort="xhigh"
      running={false}
      onModel={onModel}
      onEffort={onEffort}
      {...over}
    />,
  );
  const chip = container.querySelector(".model-chip") as HTMLElement;
  return { container, chip, onModel, onEffort };
}

test("the chip reads the current model and effort, exactly as the mock words it", () => {
  const { chip } = show();
  expect(chip.textContent).toBe("Fable 5.1 · xhigh ▾");
});

test("an unknown model value falls back to the value itself rather than going blank", () => {
  const { chip } = show({ model: "claude-haiku-4-5-20251001" });
  expect(chip.textContent).toBe("claude-haiku-4-5-20251001 · xhigh ▾");
});

test("the menu lists every model the CLI reported, with the current one marked", () => {
  const { chip } = show();
  fireEvent.click(chip);
  const rows = screen.getAllByRole("menuitem").filter((r) => r.className.includes("mi"));
  const labels = rows.map((r) => r.textContent ?? "");
  expect(labels[0]).toContain("Default");
  expect(labels[0]).toContain("Opus 5 with 1M context · recommended");
  expect(labels.filter((l) => l.includes("✓"))).toHaveLength(1);
  expect(labels.find((l) => l.includes("✓"))).toContain("Fable 5.1");
});

test("the CLI's five effort levels are offered, not four", () => {
  expect([...EFFORTS]).toEqual(["low", "medium", "high", "xhigh", "max"]);
  const { chip } = show();
  fireEvent.click(chip);
  for (const effort of EFFORTS) expect(screen.getByText(effort)).toBeTruthy();
});

test("picking a model reports it once; picking an effort reports that instead", () => {
  const { chip, onModel, onEffort } = show();
  fireEvent.click(chip);
  fireEvent.click(screen.getByText("Sonnet 5"));
  expect(onModel).toHaveBeenCalledWith("sonnet");
  expect(onEffort).not.toHaveBeenCalled();

  cleanup();
  const second = show();
  fireEvent.click(second.chip);
  fireEvent.click(screen.getByText("low"));
  expect(second.onEffort).toHaveBeenCalledWith("low");
  expect(second.onModel).not.toHaveBeenCalled();
});

test("the chip refuses to open while a turn runs: an effort change respawns the process", () => {
  const { chip, onModel, onEffort } = show({ running: true });
  expect((chip as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(chip);
  expect(screen.queryByText("Sonnet 5")).toBeNull();
  expect(onModel).not.toHaveBeenCalled();
  expect(onEffort).not.toHaveBeenCalled();
});

test("a model label from the CLI is text, never markup", () => {
  const { chip } = show({
    models: [{ value: "x", label: "<script>window.__pwned=1</script>", description: "" }],
    model: "x",
  });
  fireEvent.click(chip);
  expect(document.querySelector("script")).toBeNull();
  expect(document.body.textContent).toContain("<script>window.__pwned=1</script>");
});
