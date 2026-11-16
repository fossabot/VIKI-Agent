/**
 * Unit tests for ReconnectBanner covering the incompatible status and other scenarios.
 */
import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { ReconnectBanner } from "./ReconnectBanner";

afterEach(cleanup);

test("incompatible status renders the version mismatch message and alert role", () => {
  const { container } = render(<ReconnectBanner status="incompatible" everOpen={true} />);
  const banner = container.querySelector(".banner") as HTMLElement;
  expect(banner).not.toBeNull();
  expect(banner.textContent).toBe(
    "This window's version doesn't match the server's. Copy what you typed, then reload the window.",
  );
  expect(banner.getAttribute("role")).toBe("alert");
});
