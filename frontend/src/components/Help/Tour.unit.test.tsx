import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import { Tour, type TourStep } from "./Tour";

afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
});

function steps(): TourStep[] {
  return [
    { title: "Welcome", body: "First step.", region: "sources" },
    { title: "Search", body: "Second step.", region: "search" },
    { title: "Almost done", body: "Last step." },
  ];
}

describe("Tour", () => {
  test("a labelled, docked dialog on step 1: no Back, Next and Skip tour present", () => {
    render(<Tour steps={steps()} onClose={vi.fn()} />);
    const dialog = screen.getByRole("dialog");
    expect(dialog.className).toBe("tour");
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    screen.getByRole("heading", { name: "Welcome" });
    screen.getByText("Step 1 of 3");
    expect(screen.queryByRole("button", { name: "Back" })).toBeNull();
    screen.getByRole("button", { name: "Next" });
    screen.getByRole("button", { name: "Skip tour" });
  });

  test("Next advances the step, reveals Back, and the last step reads Done", () => {
    render(<Tour steps={steps()} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    screen.getByText("Step 2 of 3");
    screen.getByRole("heading", { name: "Search" });
    screen.getByRole("button", { name: "Back" });

    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    screen.getByText("Step 3 of 3");
    expect(screen.queryByRole("button", { name: "Next" })).toBeNull();
    screen.getByRole("button", { name: "Done" });
  });

  test("Back returns to the previous step and can hide itself again", () => {
    render(<Tour steps={steps()} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    screen.getByText("Step 1 of 3");
    expect(screen.queryByRole("button", { name: "Back" })).toBeNull();
  });

  test("Done on the last step closes the tour", () => {
    const onClose = vi.fn();
    render(<Tour steps={steps()} onClose={onClose} />);
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  test("Skip tour closes immediately, from any step", () => {
    const onClose = vi.fn();
    render(<Tour steps={steps()} onClose={onClose} />);
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    fireEvent.click(screen.getByRole("button", { name: "Skip tour" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  test("Escape closes the tour", () => {
    const onClose = vi.fn();
    render(<Tour steps={steps()} onClose={onClose} />);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  test("highlights only the current step's region, moving it on Next", () => {
    document.body.innerHTML = '<div data-region="sources"></div><div data-region="search"></div>';
    const sources = document.querySelector('[data-region="sources"]') as HTMLElement;
    const search = document.querySelector('[data-region="search"]') as HTMLElement;

    render(<Tour steps={steps()} onClose={vi.fn()} />);
    expect(sources.classList.contains("tour-highlight")).toBe(true);
    expect(search.classList.contains("tour-highlight")).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(sources.classList.contains("tour-highlight")).toBe(false);
    expect(search.classList.contains("tour-highlight")).toBe(true);

    // The third step carries no region: the highlight left behind by step 2 is cleared too.
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(search.classList.contains("tour-highlight")).toBe(false);
    expect(document.querySelector(".tour-highlight")).toBeNull();
  });

  test("a region with no matching element on screen highlights nothing (and does not throw)", () => {
    render(<Tour steps={steps()} onClose={vi.fn()} />);
    expect(document.querySelector(".tour-highlight")).toBeNull();
  });

  test("unmounting removes whatever highlight the current step added", () => {
    document.body.innerHTML = '<div data-region="sources"></div>';
    const sources = document.querySelector('[data-region="sources"]') as HTMLElement;
    const { unmount } = render(<Tour steps={steps()} onClose={vi.fn()} />);
    expect(sources.classList.contains("tour-highlight")).toBe(true);
    unmount();
    expect(sources.classList.contains("tour-highlight")).toBe(false);
  });

  test("renders nothing for an empty steps list", () => {
    const { container } = render(<Tour steps={[]} onClose={vi.fn()} />);
    expect(container.firstChild).toBeNull();
  });
});
