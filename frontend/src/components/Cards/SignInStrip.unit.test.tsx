import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import { type ProviderLoginApi, ProviderLoginApiError } from "../../clients/app/providerLoginApi";
import { SignInStrip } from "./SignInStrip";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  delete (window.navigator as { platform?: string }).platform;
});

const stub = (open: ProviderLoginApi["open"] = vi.fn().mockResolvedValue(undefined)) => ({ open });
const show = (api: ProviderLoginApi) => render(<SignInStrip api={api} />);
const settle = () => act(async () => {});
const signInButton = () => screen.getByRole("button", { name: "Sign in" });
const setPlatform = (value: string) =>
  Object.defineProperty(window.navigator, "platform", { value, configurable: true });

const LINE =
  "Sign in to Claude Code to keep chatting: VIKI Agent opens Claude Code's own sign-in in a PowerShell window, and your browser shows the Claude sign-in page.";
const LINE_MAC =
  "Sign in to Claude Code to keep chatting: VIKI Agent opens Claude Code's own sign-in in a Terminal window, and your browser shows the Claude sign-in page.";
const LINE_LINUX =
  "Sign in to Claude Code to keep chatting: VIKI Agent opens Claude Code's own sign-in in your desktop's terminal, and your browser shows the Claude sign-in page.";
const OPENED = "Finish signing in in your browser, then send your message again.";
const PROVIDER = "VIKI Agent can't start Claude Code right now. See Settings → Providers.";
const FAILED = "The sign-in window could not open. In a terminal, run claude auth login.";

describe("SignInStrip", () => {
  test("renders the explanation line and the Sign in button inside .signin-strip", () => {
    const { container } = show(stub());
    expect(container.querySelector(".signin-strip")).toBeTruthy();
    expect(screen.getByText(LINE)).toBeTruthy();
    expect(signInButton()).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  test("names Terminal on macOS", () => {
    setPlatform("MacIntel");
    show(stub());
    expect(screen.getByText(LINE_MAC)).toBeTruthy();
  });

  test("names the desktop's terminal on Linux", () => {
    setPlatform("Linux x86_64");
    show(stub());
    expect(screen.getByText(LINE_LINUX)).toBeTruthy();
  });

  test.each([
    [
      "Win32",
      "Sign in opens Claude Code's own sign-in in a PowerShell window, and your browser shows the Claude sign-in page.",
    ],
    [
      "MacIntel",
      "Sign in opens Claude Code's own sign-in in a Terminal window, and your browser shows the Claude sign-in page.",
    ],
    [
      "Linux x86_64",
      "Sign in opens Claude Code's own sign-in in your desktop's terminal, and your browser shows the Claude sign-in page.",
    ],
  ])("setup context on %s uses the setup line and opened text", async (platform, line) => {
    setPlatform(platform);
    render(<SignInStrip api={stub()} context="setup" />);
    expect(screen.getByText(line)).toBeTruthy();
    fireEvent.click(signInButton());
    await settle();
    expect(screen.getByRole("status").textContent).toBe(
      "Finish signing in in your browser. VIKI Agent checks your sign-in when you start a chat.",
    );
  });

  test("a click calls open once with an AbortSignal, goes busy, then shows the opened line", async () => {
    const open = vi.fn().mockResolvedValue(undefined);
    show(stub(open));
    fireEvent.click(signInButton());
    const busy = screen.getByRole("button", { name: "Opening…" }) as HTMLButtonElement;
    expect(busy.disabled).toBe(true);
    await settle();
    expect(open).toHaveBeenCalledTimes(1);
    expect(open).toHaveBeenCalledWith(expect.any(AbortSignal));
    expect(screen.getByRole("status").textContent).toBe(OPENED);
    expect(signInButton()).toBeTruthy();
  });

  test("a click while busy is ignored: no second call, button stays disabled", async () => {
    let finish: () => void = () => {};
    const open = vi.fn(() => new Promise<void>((resolve) => (finish = resolve)));
    show(stub(open));
    fireEvent.click(signInButton());
    const busy = screen.getByRole("button", { name: "Opening…" });
    fireEvent.click(busy);
    finish();
    await settle();
    expect(open).toHaveBeenCalledTimes(1);
  });

  test("provider_unavailable shows the provider refusal as an alert", async () => {
    show(stub(vi.fn().mockRejectedValue(new ProviderLoginApiError("provider_unavailable"))));
    fireEvent.click(signInButton());
    await settle();
    expect(screen.getByRole("alert").textContent).toBe(PROVIDER);
    expect(screen.getByRole("status").textContent).toBe("");
  });

  test("any other refusal code shows the generic failed line", async () => {
    show(stub(vi.fn().mockRejectedValue(new ProviderLoginApiError("overloaded"))));
    fireEvent.click(signInButton());
    await settle();
    expect(screen.getByRole("alert").textContent).toBe(FAILED);
  });

  test("a failure without a code reads as failed, not success", async () => {
    show(stub(vi.fn().mockRejectedValue(new Error("boom"))));
    fireEvent.click(signInButton());
    await settle();
    expect(screen.getByRole("alert").textContent).toBe(FAILED);
    expect(screen.getByRole("status").textContent).toBe("");
  });

  test("a later successful click clears a previous refusal and shows opened", async () => {
    const open = vi
      .fn()
      .mockRejectedValueOnce(new ProviderLoginApiError("start_failed"))
      .mockResolvedValueOnce(undefined);
    show(stub(open));
    fireEvent.click(signInButton());
    await settle();
    expect(screen.getByRole("alert")).toBeTruthy();
    fireEvent.click(signInButton());
    await settle();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("status").textContent).toBe(OPENED);
  });

  test("a failure after an earlier success clears the opened line", async () => {
    const open = vi
      .fn()
      .mockResolvedValueOnce(undefined)
      .mockRejectedValueOnce(new ProviderLoginApiError("start_failed"));
    show(stub(open));
    fireEvent.click(signInButton());
    await settle();
    expect(screen.getByRole("status").textContent).toBe(OPENED);
    fireEvent.click(signInButton());
    await settle();
    expect(screen.getByRole("alert")).toBeTruthy();
    expect(screen.getByRole("status").textContent).toBe("");
  });
});
