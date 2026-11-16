import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki } from "../../accept/m3b/fakes";
import { fakeDurable } from "../../testDurable";
import { fakeShellApi, settingsFixture } from "../../testShell";

beforeEach(() => {
  window.sessionStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
async function settled() {
  await act(async () => {
    for (let n = 0; n < 5; n++) await Promise.resolve();
  });
}
function widthSettings() {
  const view = settingsFixture({ "onboarding.completed": true });
  for (const [key, value, fallback, min, max] of [
    ["layout.left_rail_width", 300, 272, 220, 480],
    ["layout.right_rail_width", 360, 320, 240, 560],
  ] as const) {
    view.values = { ...view.values, [key]: value };
    view.effective = { ...view.effective, [key]: value };
    view.sources = { ...view.sources, [key]: "setting" };
    view.keys = {
      ...view.keys,
      [key]: {
        scope: "app",
        applies: "now",
        kind: "number",
        allowed: Array.from({ length: max - min + 1 }, (_, offset) => min + offset),
        default: fallback,
        personal: false,
      },
    };
  }
  return view;
}
async function mount(settings = widthSettings()) {
  const shell = fakeShellApi({ settings });
  const persist = shell.patchSettings.bind(shell);
  const patch = vi.spyOn(shell, "patchSettings");
  const view = render(
    <App viki={fakeViki().viki} rest={fakeRest()} durable={fakeDurable()} shell={shell} />,
  );
  await settled();
  return { shell, patch, persist, view };
}
const handle = (side: "left" | "right") =>
  screen.getByRole("separator", { name: `Resize ${side} rail` });
test("loaded app widths reach the actual Shell without a settings write", async () => {
  const { patch } = await mount();
  expect(handle("left").getAttribute("aria-valuenow")).toBe("300");
  expect(handle("right").getAttribute("aria-valuenow")).toBe("360");
  expect(patch).not.toHaveBeenCalled();
});
test("one accepted keyboard commit sends its exact width against the loaded revision", async () => {
  const { patch } = await mount();
  fireEvent.keyDown(handle("left"), { key: "ArrowRight" });
  await settled();
  expect(handle("left").getAttribute("aria-valuenow")).toBe("310");
  expect(patch).toHaveBeenCalledExactlyOnceWith(expect.any(String), 3, {
    "layout.left_rail_width": 310,
  });
});

function pointer(target: HTMLElement, type: string, clientX: number) {
  const event = new MouseEvent(type, { bubbles: true, button: 0, clientX });
  Object.defineProperties(event, { pointerId: { value: 1 }, isPrimary: { value: true } });
  fireEvent(target, event);
}
function capture(target: HTMLElement) {
  let held = false;
  Object.assign(target, {
    setPointerCapture: () => {
      held = true;
    },
    hasPointerCapture: () => held,
    releasePointerCapture: () => {
      held = false;
    },
  });
}

test("pointer samples stay local and release sends one exact revision-pinned width", async () => {
  const { patch } = await mount();
  const left = handle("left");
  capture(left);
  pointer(left, "pointerdown", 100);
  pointer(left, "pointermove", 110);
  pointer(left, "pointermove", 120);
  expect(left.getAttribute("aria-valuenow")).toBe("320");
  expect(patch).not.toHaveBeenCalled();
  pointer(left, "pointerup", 120);
  await settled();
  expect(patch).toHaveBeenCalledExactlyOnceWith(expect.any(String), 3, {
    "layout.left_rail_width": 320,
  });
});

test("cancel and late release do not save; a double-click reset sends only null", async () => {
  const { patch } = await mount();
  const left = handle("left");
  capture(left);
  pointer(left, "pointerdown", 100);
  pointer(left, "pointermove", 120);
  pointer(left, "pointercancel", 120);
  pointer(left, "pointerup", 120);
  expect(patch).not.toHaveBeenCalled();
  fireEvent.doubleClick(handle("right"));
  await settled();
  expect(patch).toHaveBeenCalledExactlyOnceWith(expect.any(String), 3, {
    "layout.right_rail_width": null,
  });
  expect(handle("right").getAttribute("aria-valuenow")).toBe("320");
});

const grow = (side: "left" | "right") =>
  fireEvent.keyDown(handle(side), { key: side === "left" ? "ArrowRight" : "ArrowLeft" });
function deferred<T>() {
  let resolve: (value: T) => void = () => {};
  const promise = new Promise<T>((settle) => {
    resolve = settle;
  });
  return { promise, resolve: (value: T) => resolve(value) };
}
test("keyboard commits and Restore save once each while Collapse saves nothing", async () => {
  const { patch } = await mount();
  grow("left");
  await settled();
  fireEvent.keyDown(handle("right"), { key: "Enter" });
  expect(handle("right").getAttribute("aria-valuenow")).toBe("0");
  expect(patch).toHaveBeenCalledTimes(1);
  fireEvent.keyDown(handle("right"), { key: "Enter" });
  await settled();
  expect(patch.mock.calls.map(([, revision, changes]) => [revision, changes])).toEqual([
    [3, { "layout.left_rail_width": 310 }],
    [4, { "layout.right_rail_width": 360 }],
  ]);
});
test.each(["left", "right"] as const)(
  "an older write cannot replace a newer %s local commit or queue a write",
  async (side) => {
    const { patch, persist } = await mount();
    const wait = deferred<void>();
    patch.mockImplementationOnce(async (...args) => {
      await wait.promise;
      return persist(...args);
    });
    grow("left");
    grow(side);
    expect(patch).toHaveBeenCalledTimes(1);
    expect(handle(side).getAttribute("aria-valuenow")).toBe(side === "left" ? "320" : "370");
    expect(
      screen.getByRole("button", { name: `Save ${side} rail width` }).hasAttribute("disabled"),
    ).toBe(true);
    await act(async () => wait.resolve());
    await settled();
    expect(handle(side).getAttribute("aria-valuenow")).toBe(side === "left" ? "320" : "370");
    expect(patch).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: `Save ${side} rail width` }));
    await settled();
    expect(patch.mock.calls.map(([, revision, changes]) => [revision, changes])).toEqual([
      [3, { "layout.left_rail_width": 310 }],
      [
        4,
        {
          [side === "left" ? "layout.left_rail_width" : "layout.right_rail_width"]:
            side === "left" ? 320 : 370,
        },
      ],
    ]);
    expect(patch.mock.calls[0]?.[0]).not.toBe(patch.mock.calls[1]?.[0]);
  },
);
test("stale widths stay local without adopting the conflict document; Reload reads and Save is a new command", async () => {
  const { ShellApiError } = await import("../../clients/app/shellApi");
  const { shell, patch } = await mount();
  const current = widthSettings();
  current.revision = 9;
  current.effective = { ...current.effective, "layout.left_rail_width": 380 };
  current.values = { ...current.values, "layout.left_rail_width": 380 };
  patch.mockRejectedValueOnce(new ShellApiError(409, "stale_revision", { current }));
  grow("left");
  await settled();
  expect(handle("left").getAttribute("aria-valuenow")).toBe("310");
  expect(screen.getByRole("region", { name: "Rail width review" }).textContent).toContain(
    "current 380 pixels; requested 310 pixels",
  );
  expect(patch).toHaveBeenCalledExactlyOnceWith(expect.any(String), 3, {
    "layout.left_rail_width": 310,
  });
  expect(shell.calls.filter((call) => call.method === "settings")).toHaveLength(1);
  expect(
    screen.getByRole("button", { name: "Save left rail width" }).hasAttribute("disabled"),
  ).toBe(true);
  shell.data.settings = current;
  fireEvent.click(screen.getByRole("button", { name: "Reload current rail widths" }));
  await settled();
  expect(shell.calls.filter((call) => call.method === "settings")).toHaveLength(2);
  expect(patch).toHaveBeenCalledTimes(1);
  expect(handle("left").getAttribute("aria-valuenow")).toBe("310");
  fireEvent.click(screen.getByRole("button", { name: "Save left rail width" }));
  await settled();
  expect(patch.mock.calls.map(([, revision, changes]) => [revision, changes])).toEqual([
    [3, { "layout.left_rail_width": 310 }],
    [9, { "layout.left_rail_width": 310 }],
  ]);
  expect(patch.mock.calls[0]?.[0]).not.toBe(patch.mock.calls[1]?.[0]);
  expect(screen.queryByRole("region", { name: "Rail width review" })).toBeNull();
});
test.each(["read_only", "transport"])(
  "%s refusal keeps widths local with explicit read recovery and no retry",
  async (code) => {
    const { ShellApiError } = await import("../../clients/app/shellApi");
    const { patch } = await mount();
    patch.mockRejectedValueOnce(new ShellApiError(code === "read_only" ? 403 : 0, code));
    grow("left");
    await settled();
    grow("left");
    await settled();
    expect(handle("left").getAttribute("aria-valuenow")).toBe("320");
    expect(patch).toHaveBeenCalledTimes(1);
    expect(
      screen.getByRole("button", { name: "Save left rail width" }).hasAttribute("disabled"),
    ).toBe(true);
    expect(screen.getByRole("button", { name: "Reload current rail widths" })).toBeTruthy();
    expect(screen.getByRole("region", { name: "Rail width review" }).textContent).toContain(
      "could not be saved",
    );
  },
);
test.each(["read-only", "legacy"] as const)(
  "%s loaded settings allow local commits without a patch",
  async (mode) => {
    const settings =
      mode === "legacy" ? settingsFixture({ "onboarding.completed": true }) : widthSettings();
    if (mode === "read-only") {
      settings.read_only = true;
      settings.reason = "newer_version";
    }
    const { patch } = await mount(settings);
    grow("left");
    await settled();
    expect(handle("left").getAttribute("aria-valuenow")).toBe(mode === "legacy" ? "282" : "310");
    expect(patch).not.toHaveBeenCalled();
    expect(screen.getByRole("region", { name: "Rail width review" }).textContent).toContain(
      "remain local",
    );
  },
);
test("a late initial settings read does not replace a width committed while loading", async () => {
  const shell = fakeShellApi({ settings: widthSettings() });
  const wait = deferred<ReturnType<typeof widthSettings>>();
  vi.spyOn(shell, "settings").mockReturnValueOnce(wait.promise);
  const patch = vi.spyOn(shell, "patchSettings");
  render(<App viki={fakeViki().viki} rest={fakeRest()} durable={fakeDurable()} shell={shell} />);
  await settled();
  grow("left");
  expect(handle("left").getAttribute("aria-valuenow")).toBe("282");
  await act(async () => wait.resolve(widthSettings()));
  await settled();
  expect(handle("left").getAttribute("aria-valuenow")).toBe("282");
  expect(patch).not.toHaveBeenCalled();
  expect(
    screen.getByRole("button", { name: "Save left rail width" }).hasAttribute("disabled"),
  ).toBe(true);
});
test("passive narrowing and growth do not publish local widths", async () => {
  const { patch } = await mount();
  const frame = document.querySelector<HTMLElement>(".shell-body");
  if (!frame) throw new Error("Missing actual Shell frame");
  for (const width of [700, 1440]) {
    Object.defineProperty(frame, "clientWidth", { configurable: true, value: width });
    fireEvent(window, new Event("resize"));
    await settled();
  }
  expect(handle("left").getAttribute("aria-valuenow")).toBe("300");
  expect(handle("right").getAttribute("aria-valuenow")).toBe("360");
  expect(patch).not.toHaveBeenCalled();
});
test("unmount does not save or replay a pending width command", async () => {
  const { patch, persist, view } = await mount();
  const wait = deferred<void>();
  patch.mockImplementationOnce(async (...args) => {
    await wait.promise;
    return persist(...args);
  });
  grow("left");
  view.unmount();
  expect(patch).toHaveBeenCalledTimes(1);
  await act(async () => wait.resolve());
  await settled();
  expect(patch).toHaveBeenCalledTimes(1);
});
test("a legacy missing patch method keeps default widths and usable local resizing", async () => {
  const shell = fakeShellApi({ settings: widthSettings() });
  const patch = vi.spyOn(shell, "patchSettings");
  Reflect.deleteProperty(shell, "patchSettings");
  render(<App viki={fakeViki().viki} rest={fakeRest()} durable={fakeDurable()} shell={shell} />);
  await settled();
  expect(handle("left").getAttribute("aria-valuenow")).toBe("272");
  expect(handle("right").getAttribute("aria-valuenow")).toBe("320");
  grow("left");
  await settled();
  expect(handle("left").getAttribute("aria-valuenow")).toBe("282");
  expect(patch).not.toHaveBeenCalled();
  expect(screen.getByRole("region", { name: "Rail width review" }).textContent).toContain(
    "remain local",
  );
});
