import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { HowItWorks, type JumpRow } from "./HowItWorks";

afterEach(cleanup);

const TITLES = ["Notices", "Decides", "Tells you", "Starts work, if you allow"];

const JUMPS: readonly (readonly [string, JumpRow])[] = [
  ["Choose what it watches", "watch"],
  ["Set up the Decision model", "model"],
  ["Choose how it reaches you", "reach"],
  ["Choose what it may do", "do"],
  ["See how VIKI is doing", "how"],
  ["See what leaves this computer", "privacy"],
];

test("lists the four steps in order", () => {
  render(<HowItWorks enabled={false} onJump={vi.fn()} />);
  const items = screen.getAllByRole("listitem");
  expect(items).toHaveLength(4);
  TITLES.forEach((title, i) => {
    expect(items[i]?.textContent).toContain(title);
    expect(items[i]?.textContent?.startsWith(String(i + 1))).toBe(true);
  });
});

test.each(JUMPS)("%s jumps to its row once", (name, row) => {
  const onJump = vi.fn();
  render(<HowItWorks enabled={false} onJump={onJump} />);
  fireEvent.click(screen.getByRole("button", { name }));
  expect(onJump).toHaveBeenCalledTimes(1);
  expect(onJump).toHaveBeenCalledWith(row);
});

test("off: a region named How VIKI works, no details", () => {
  const { container } = render(<HowItWorks enabled={false} onJump={vi.fn()} />);
  expect(screen.getByRole("region", { name: "How VIKI works" })).toBeTruthy();
  expect(container.querySelector("details")).toBeNull();
});

test("on: one closed details summarised How VIKI works", () => {
  const { container } = render(<HowItWorks enabled onJump={vi.fn()} />);
  const details = container.querySelectorAll("details");
  expect(details).toHaveLength(1);
  expect(details[0]?.open).toBe(false);
  expect(container.querySelector("summary")?.textContent).toBe("How VIKI works");
  expect(screen.queryByRole("region")).toBeNull();
});

test("says what needs the Decision model and names no internal term", () => {
  const { container } = render(<HowItWorks enabled={false} onJump={vi.fn()} />);
  const text = container.textContent ?? "";
  expect(text).toContain("need the Decision model");
  expect(text).not.toMatch(/jev/i);
  expect(text).not.toMatch(/system 1/i);
});
