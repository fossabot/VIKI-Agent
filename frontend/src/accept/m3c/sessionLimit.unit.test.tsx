/**
 * Two SessionLimitToast behaviours sessionLimit.accept.test.tsx never pinned:
 *
 * (c) the IME guard on its window Escape listener (SessionLimitToast.tsx): nothing fired a
 *     composing Escape, so a regression that dropped `event.isComposing` would dismiss the
 *     toast every time the user cancels a Japanese candidate window mid-type. Same shape as
 *     src/accept/m2c/composer.accept.test.tsx's own IME guard test.
 * (d) listener cleanup on unmount: nothing unmounted the toast and re-fired Escape, so a stale
 *     `window` listener calling a dead `onDismiss` would go unnoticed.
 */
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { SessionLimitToast } from "../../components/Toast/SessionLimitToast";

afterEach(cleanup);

const LIVE = [{ sessionId: "s1", title: "Dashboard v1" }];

test("(c) Escape from an IME candidate window does not dismiss the toast", () => {
  const onDismiss = vi.fn();
  render(<SessionLimitToast live={LIVE} onClose={vi.fn()} onDismiss={onDismiss} />);

  fireEvent.keyDown(window, { key: "Escape", isComposing: true });
  expect(onDismiss).not.toHaveBeenCalled();

  fireEvent.keyDown(window, { key: "Escape", keyCode: 229 });
  expect(onDismiss).not.toHaveBeenCalled();

  fireEvent.keyDown(window, { key: "Escape" });
  expect(onDismiss).toHaveBeenCalledTimes(1);
});

test("(d) an unmounted toast leaves no window listener behind", () => {
  const onDismiss = vi.fn();
  const { unmount } = render(
    <SessionLimitToast live={LIVE} onClose={vi.fn()} onDismiss={onDismiss} />,
  );
  unmount();
  fireEvent.keyDown(window, { key: "Escape" });
  expect(onDismiss).not.toHaveBeenCalled();
});
