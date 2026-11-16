/**
 * Pins ComposerProps.label: the textarea's own accessible name, defaulting to "Message" so
 * every existing `aria-label="Message"` query (App.tsx, Shell.tsx and the tests that use
 * `getByLabelText("Message")`) keeps matching when the prop is omitted, and overridable —
 * LauncherChat passes "Message VIKI" — without moving or renaming anything else in the composer.
 */
import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { Composer } from "./Composer";

afterEach(cleanup);

test("the textarea's aria-label defaults to Message when label is omitted", () => {
  const { container } = render(
    <Composer running={false} onSend={vi.fn(async () => true)} onStop={vi.fn()} />,
  );
  expect(container.querySelector("textarea")?.getAttribute("aria-label")).toBe("Message");
});

test("a given label replaces the textarea's aria-label, leaving the rest of the composer unchanged", () => {
  const { container } = render(
    <Composer
      running={false}
      onSend={vi.fn(async () => true)}
      onStop={vi.fn()}
      label="Message VIKI"
    />,
  );
  expect(container.querySelector("textarea")?.getAttribute("aria-label")).toBe("Message VIKI");
  // Nothing else is named off this prop.
  expect(container.querySelector('[aria-label="Send"]')).toBeTruthy();
  expect(container.querySelector('[aria-label="Chat activity"]')).toBeTruthy();
  expect(container.querySelector('[aria-label="Chat composer"]')).toBeTruthy();
});
