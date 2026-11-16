import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import {
  hasUnsavedWork,
  setUnsavedWork,
  useHasUnsavedWork,
  useReportUnsavedWork,
} from "./unsavedWork";

afterEach(cleanup);

test("a view's report is held while it is mounted and dirty, and cleared on unmount", () => {
  const reader = renderHook(() => useHasUnsavedWork());
  const view = renderHook(({ dirty }) => useReportUnsavedWork("vault", dirty), {
    initialProps: { dirty: false },
  });
  expect(hasUnsavedWork()).toBe(false);

  view.rerender({ dirty: true });
  expect(hasUnsavedWork()).toBe(true);
  expect(reader.result.current).toBe(true);

  view.rerender({ dirty: false });
  expect(reader.result.current).toBe(false);

  view.rerender({ dirty: true });
  view.unmount();
  expect(hasUnsavedWork()).toBe(false);
  expect(reader.result.current).toBe(false);
});

test("a code buffer does not make leaving a view ask", () => {
  const reader = renderHook(() => useHasUnsavedWork());
  act(() => setUnsavedWork("code", true));
  expect(hasUnsavedWork()).toBe(false);
  expect(reader.result.current).toBe(true);

  act(() => setUnsavedWork("vault", true));
  expect(hasUnsavedWork()).toBe(true);
  act(() => setUnsavedWork("vault", false));
  act(() => setUnsavedWork("code", false));
  expect(reader.result.current).toBe(false);
});

test("one clean view does not clear another view's unsaved work", () => {
  const vault = renderHook(() => useReportUnsavedWork("vault", true));
  const profile = renderHook(({ dirty }) => useReportUnsavedWork("profile", dirty), {
    initialProps: { dirty: true },
  });
  profile.rerender({ dirty: false });
  expect(hasUnsavedWork()).toBe(true);
  vault.unmount();
  expect(hasUnsavedWork()).toBe(false);
});
