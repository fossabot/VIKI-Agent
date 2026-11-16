/**
 * Acceptance (frozen): the reconnect banner. Its promise —
 * that an interrupted reply resumes — is the reason the user does not close the window mid
 * turn, so it must never appear when it cannot keep that promise.
 */
import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { ReconnectBanner } from "../../components/Shell/ReconnectBanner";

afterEach(cleanup);

function show(status: Parameters<typeof ReconnectBanner>[0]["status"], everOpen = true) {
  const { container } = render(<ReconnectBanner status={status} everOpen={everOpen} />);
  return container;
}

test("an open socket shows nothing at all, not an empty bar", () => {
  expect(show("open").innerHTML).toBe("");
});

test("a first load that has never connected stays quiet: there is nothing to resume", () => {
  expect(show("connecting", false).innerHTML).toBe("");
});

test("a dropped socket says so, and promises the reply resumes", () => {
  const container = show("reconnecting");
  const banner = container.querySelector(".banner") as HTMLElement;
  expect(banner.textContent).toContain("Disconnected. Reconnecting…");
  expect(banner.textContent).toContain("After reconnecting, the rest of the reply will appear.");
  expect(banner.querySelector(".banner-spin")).not.toBeNull();
  expect(banner.getAttribute("role")).toBe("status");
  expect(banner.getAttribute("aria-live")).toBe("polite");
});

test("closed and connecting read the same as reconnecting once we have been connected", () => {
  for (const status of ["closed", "connecting"] as const) {
    const container = show(status);
    expect(container.querySelector(".banner")?.textContent).toContain(
      "Disconnected. Reconnecting…",
    );
    cleanup();
  }
});

test("an unauthorized socket shows nothing: waiting will not fix a dead token", () => {
  expect(show("unauthorized").innerHTML).toBe("");
});

test("the banner loads no image and no remote asset for its spinner", () => {
  const container = show("reconnecting");
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector("svg image")).toBeNull();
});
