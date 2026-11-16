import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CaptureInboxRow } from "./CaptureInboxRow";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("CaptureInboxRow", () => {
  it("renders with singular 'file' when count is 1", () => {
    const onOpen = vi.fn();
    render(<CaptureInboxRow count={1} onOpen={onOpen} />);

    const text = screen.getByText("1 file waits in 00-inbox/.");
    expect(text).toBeTruthy();
  });

  it("renders with plural 'files' when count is greater than 1", () => {
    const onOpen = vi.fn();
    render(<CaptureInboxRow count={5} onOpen={onOpen} />);

    const text = screen.getByText("5 files wait in 00-inbox/.");
    expect(text).toBeTruthy();
  });

  it("renders the Triage inbox button", () => {
    const onOpen = vi.fn();
    render(<CaptureInboxRow count={3} onOpen={onOpen} />);

    const buttons = screen.getAllByRole("button", { name: "Triage inbox" });
    const button = buttons[0] as HTMLElement;
    expect(button).toBeTruthy();
    expect(button.className).toContain("btn");
    expect(button.className).toContain("alt");
    expect(button.className).toContain("sm");
  });

  it("calls onOpen when button is clicked", () => {
    const onOpen = vi.fn();
    render(<CaptureInboxRow count={2} onOpen={onOpen} />);

    const buttons = screen.getAllByRole("button", { name: "Triage inbox" });
    const button = buttons[0] as HTMLElement;
    fireEvent.click(button);

    expect(onOpen).toHaveBeenCalledOnce();
  });

  it("has correct aria-label on section", () => {
    const onOpen = vi.fn();
    const { container } = render(<CaptureInboxRow count={1} onOpen={onOpen} />);

    const section = container.querySelector('section[aria-label="Inbox captures"]') as HTMLElement;
    expect(section).toBeTruthy();
  });
});
