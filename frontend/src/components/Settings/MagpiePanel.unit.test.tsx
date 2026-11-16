import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { MagpieApi, MagpieStatus } from "../../clients/app/magpieApi";
import { ModelConnectionsContext } from "../../clients/app/modelConnectionsApi";
import { readCss } from "../../testing/css";
import { MagpiePanel } from "./MagpiePanel";
import { readMagpieView, watchMagpieView } from "./magpieView";

const ui = "a".repeat(32);
const state: MagpieStatus = {
  available: true,
  running: true,
  version: "0.1.3357",
  desktop: true,
  base_url: "http://127.0.0.1:3426/v1",
};
const colors = {
  "--g0": "#010101",
  "--g2": "#020202",
  "--g3": "#030303",
  "--t0": "#040404",
  "--t1": "#050505",
  "--t2": "#060606",
  "--t3": "#070707",
  "--line": "#080808",
  "--hair": "#090909",
  "--or": "#101010",
  "--on-or": "#111111",
  "--fill-popover": "#121212",
};
let frames = new Map<number, FrameRequestCallback>();
let frameId = 0;
let stageStyle: HTMLStyleElement;
const stageCss = readCss(import.meta.url, "MagpiePanel.css");

beforeEach(() => {
  history.replaceState(null, "", `/?ui=${ui}`);
  const root = document.documentElement;
  root.dataset.theme = "dark";
  root.dataset.density = "comfortable";
  for (const [key, value] of Object.entries(colors)) root.style.setProperty(key, value);
  root.style.setProperty("--font-scale", "1");
  stageStyle = document.createElement("style");
  stageStyle.textContent = stageCss;
  document.head.append(stageStyle);
  frames = new Map();
  frameId = 0;
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
    frames.set(++frameId, callback);
    return frameId;
  });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => frames.delete(id));
  vi.stubGlobal("matchMedia", (media: string) => ({
    media,
    matches: false,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }));
});

afterEach(() => {
  cleanup();
  stageStyle.remove();
  document.body.replaceChildren();
  document.documentElement.removeAttribute("style");
  delete document.documentElement.dataset.theme;
  delete document.documentElement.dataset.density;
  delete document.documentElement.dataset.motion;
  history.replaceState(null, "", "/");
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function measure(main: HTMLElement, rect = new DOMRect(272, 49, 700, 600)) {
  vi.spyOn(main, "getBoundingClientRect").mockReturnValue(rect);
}

function flushFrame() {
  const pending = [...frames.values()];
  frames.clear();
  for (const callback of pending) callback(performance.now());
}

function magpieApi() {
  return {
    status: vi.fn<MagpieApi["status"]>(async () => state),
    open: vi.fn<MagpieApi["open"]>(async () => state),
    view: vi.fn<MagpieApi["view"]>(async () => state),
    close: vi.fn<MagpieApi["close"]>(async () => state),
  };
}

async function show(magpie: ReturnType<typeof magpieApi>) {
  const listing = { revision: 0, connections: [], default_model: null };
  const result = render(
    <ModelConnectionsContext.Provider
      value={{
        magpie,
        list: async () => listing,
        save: async () => listing,
        remove: async () => listing,
        updateKey: async () => listing,
        test: async () => [],
        discover: async () => [],
      }}
    >
      <main className="app-view" data-region="view">
        <MagpiePanel onConnect={vi.fn()} connectionDisabled={false} />
      </main>
    </ModelConnectionsContext.Provider>,
  );
  measure(result.container.querySelector("main") as HTMLElement);
  const open = await screen.findByRole("button", { name: "Open Magpie" }, { timeout: 50 });
  await waitFor(() => expect(open.matches(":disabled")).toBe(false), { timeout: 50 });
  return { ...result, open };
}

test("the panel says Magpie starts with its subscription accounts turned off", async () => {
  await show(magpieApi());
  expect(screen.getByText(/subscription accounts turned off/)).toBeTruthy();
});

test("an unprepared Magpie says installed copies include it and shows the source setup command as text", async () => {
  const magpie = magpieApi();
  magpie.status.mockResolvedValue({ ...state, available: false, running: false });
  const view = render(
    <ModelConnectionsContext.Provider
      value={{
        magpie,
        list: async () => ({ revision: 0, connections: [], default_model: null }),
        save: async () => ({ revision: 0, connections: [], default_model: null }),
        remove: async () => ({ revision: 0, connections: [], default_model: null }),
        updateKey: async () => ({ revision: 0, connections: [], default_model: null }),
        test: async () => [],
        discover: async () => [],
      }}
    >
      <MagpiePanel onConnect={vi.fn()} connectionDisabled={false} />
    </ModelConnectionsContext.Provider>,
  );
  const notice = await screen.findByText(/Installed copies of VIKI Agent include it/);
  expect(notice.getAttribute("role")).toBe("status");
  const command = view.container.querySelector("code");
  expect(command?.textContent).toBe("uv run python scripts/prepare_magpie.py");
  expect(command?.children).toHaveLength(0);
  expect(screen.getByText(/prepares it once with this command/)).toBeTruthy();
  expect((screen.getByRole("button", { name: "Open Magpie" }) as HTMLButtonElement).disabled).toBe(
    true,
  );
  expect(magpie.open).not.toHaveBeenCalled();
});

test("the dashboard samples VIKI's resolved palette and scaled content rectangle", () => {
  const main = document.createElement("main");
  main.className = "app-view";
  main.dataset.region = "view";
  document.body.append(main);
  measure(main, new DOMRect(408, 73.5, 500, 600));
  const root = document.documentElement;
  root.dataset.theme = "light";
  root.dataset.density = "compact";
  root.dataset.motion = "reduce";
  root.style.setProperty("--font-scale", "1.5");
  root.style.setProperty("--t2", "#C8C8C8");
  expect(readMagpieView("b".repeat(32))).toEqual({
    id: "b".repeat(32),
    bounds: { x: 408, y: 73.5, width: 500, height: 600 },
    appearance: {
      theme: "light",
      density: "compact",
      scale: 1.5,
      reduce_motion: true,
      locale: "en",
      palette: {
        bg: "#010101",
        panel: "#020202",
        raised: "#030303",
        text: "#040404",
        secondary: "#050505",
        muted: "#c8c8c8",
        faint: "#070707",
        line: "#080808",
        hair: "#090909",
        accent: "#101010",
        onAccent: "#111111",
        popover: "#121212",
      },
    },
    visible: true,
  });
  root.style.setProperty("--g0", "url(https://example.com)");
  expect(readMagpieView("b".repeat(32))).toBeNull();
});

test("VIKI overlays hide the dashboard only when they intersect its content", () => {
  const main = document.createElement("main");
  main.className = "app-view";
  main.dataset.region = "view";
  document.body.append(main);
  measure(main);
  const menu = document.createElement("div");
  menu.setAttribute("role", "menu");
  document.body.append(menu);
  const box = vi.spyOn(menu, "getBoundingClientRect");
  box.mockReturnValue(new DOMRect(0, 50, 200, 200));
  expect(readMagpieView("b".repeat(32))?.visible).toBe(true);
  box.mockReturnValue(new DOMRect(300, 100, 200, 200));
  expect(readMagpieView("b".repeat(32))?.visible).toBe(false);
  menu.hidden = true;
  expect(readMagpieView("b".repeat(32))?.visible).toBe(true);
  main.setAttribute("inert", "");
  expect(readMagpieView("b".repeat(32))?.visible).toBe(false);
});

test("appearance and layout updates coalesce and stop after the view closes", async () => {
  const main = document.createElement("main");
  main.className = "app-view";
  main.dataset.region = "view";
  document.body.append(main);
  measure(main);
  const initial = readMagpieView("b".repeat(32));
  if (!initial) throw new Error("missing view");
  const changed = vi.fn();
  const stop = watchMagpieView(initial, changed);
  document.documentElement.dataset.theme = "light";
  document.documentElement.style.setProperty("--t2", "#c8c8c8");
  window.dispatchEvent(new Event("resize"));
  window.dispatchEvent(new Event("resize"));
  await act(async () => {});
  expect(frames.size).toBe(1);
  flushFrame();
  expect(changed).toHaveBeenCalledOnce();
  expect(changed.mock.calls[0]?.[0].appearance.theme).toBe("light");
  expect(changed.mock.calls[0]?.[0].appearance.palette.muted).toBe("#c8c8c8");
  stop();
  document.documentElement.style.setProperty("--t2", "#ffffff");
  window.dispatchEvent(new Event("resize"));
  await act(async () => {});
  flushFrame();
  expect(changed).toHaveBeenCalledOnce();
});

test("navigating away cancels pending startup and closes only its owned view", async () => {
  const api = magpieApi();
  let finish!: (status: MagpieStatus) => void;
  api.open.mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  const result = await show(api);
  fireEvent.click(result.open);
  const [windowId, signal, view] = api.open.mock.calls[0] ?? [];
  expect(windowId).toBe(ui);
  expect(view?.id).toMatch(/^[0-9a-f]{32}$/);
  expect(view?.bounds).toEqual({ x: 272, y: 49, width: 700, height: 600 });
  expect(view?.visible).toBe(false);
  result.unmount();
  expect(signal?.aborted).toBe(true);
  expect(api.close).toHaveBeenCalledExactlyOnceWith(ui, view?.id);
  await act(async () => finish(state));
  window.dispatchEvent(new Event("resize"));
  flushFrame();
  expect(api.view).not.toHaveBeenCalled();
});

test("startup frames stay hidden until acknowledgment and then use the current VIKI overlay", async () => {
  const api = magpieApi();
  let finish!: (status: MagpieStatus) => void;
  api.open.mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  const result = await show(api);
  fireEvent.click(result.open);
  expect(api.open.mock.calls[0]?.[2]?.visible).toBe(false);
  const main = result.container.querySelector("main");
  if (!main) throw new Error("missing content view");
  vi.mocked(main.getBoundingClientRect).mockReturnValue(new DOMRect(280, 50, 600, 550));
  window.dispatchEvent(new Event("resize"));
  await act(async () => {});
  await act(async () => flushFrame());
  expect(api.view).toHaveBeenCalledOnce();
  expect(api.view.mock.calls[0]?.[1]).toMatchObject({
    bounds: { x: 280, y: 50, width: 600, height: 550 },
    visible: false,
  });
  const dialog = document.createElement("div");
  dialog.setAttribute("role", "dialog");
  document.body.append(dialog);
  vi.spyOn(dialog, "getBoundingClientRect").mockReturnValue(new DOMRect(300, 100, 200, 200));
  await act(async () => {});
  await act(async () => flushFrame());
  expect(api.view).toHaveBeenCalledTimes(2);
  expect(api.view.mock.calls.every(([, view]) => !view.visible)).toBe(true);
  await act(async () => finish(state));
  expect(api.view).toHaveBeenCalledTimes(3);
  expect(api.view.mock.calls[2]?.[1]).toMatchObject({
    bounds: { x: 280, y: 50, width: 600, height: 550 },
    visible: false,
  });
  dialog.remove();
  await act(async () => {});
  await act(async () => flushFrame());
  expect(api.view.mock.calls.at(-1)?.[1].visible).toBe(true);
});

test("late host-close events cannot close a newly opened dashboard", async () => {
  const api = magpieApi();
  const result = await show(api);
  const main = result.container.querySelector("main");
  if (!main) throw new Error("missing content view");
  main.scrollTop = 350;
  main.scrollLeft = 12;
  fireEvent.click(result.open);
  await waitFor(() => expect(result.open.matches(":disabled")).toBe(false), { timeout: 50 });
  const first = api.open.mock.calls[0]?.[2];
  if (!first) throw new Error("missing first view");
  expect(document.querySelector(".magpie-stage")).not.toBeNull();
  expect([main.scrollTop, main.scrollLeft]).toEqual([0, 0]);
  const form = result.container.querySelector<HTMLElement>(".settings-details");
  if (!form) throw new Error("missing connection form");
  expect(form.hasAttribute("inert")).toBe(false);
  expect(getComputedStyle(form).visibility).toBe("hidden");
  const dialog = document.createElement("div");
  dialog.setAttribute("role", "dialog");
  form.append(dialog);
  vi.spyOn(dialog, "getBoundingClientRect").mockReturnValue(new DOMRect(300, 100, 200, 200));
  expect(getComputedStyle(dialog).visibility).toBe("visible");
  expect(readMagpieView(first.id)?.visible).toBe(false);
  dialog.remove();
  act(() => window.dispatchEvent(new CustomEvent("viki-magpie-closed", { detail: first.id })));
  expect(api.close).toHaveBeenCalledExactlyOnceWith(ui, first.id);
  expect(document.querySelector(".magpie-stage")).toBeNull();
  expect([main.scrollTop, main.scrollLeft]).toEqual([350, 12]);
  expect(getComputedStyle(form).visibility).toBe("visible");
  expect(result.container.querySelector(".settings-details")?.hasAttribute("inert")).toBe(false);
  fireEvent.click(result.open);
  await waitFor(() => expect(api.open).toHaveBeenCalledTimes(2), { timeout: 50 });
  const second = api.open.mock.calls[1]?.[2];
  if (!second) throw new Error("missing second view");
  expect(second.id).not.toBe(first.id);
  act(() => window.dispatchEvent(new CustomEvent("viki-magpie-closed", { detail: first.id })));
  expect(api.close).toHaveBeenCalledOnce();
  document.documentElement.style.setProperty("--font-scale", "1.3");
  await act(async () => {});
  await act(async () => flushFrame());
  expect(api.view).toHaveBeenCalledWith(
    ui,
    expect.objectContaining({ id: second.id }),
    expect.any(AbortSignal),
  );
  result.unmount();
  expect(api.close).toHaveBeenLastCalledWith(ui, second.id);
});

test("a slow view update sends the latest pending appearance after it completes", async () => {
  const api = magpieApi();
  let finish!: (status: MagpieStatus) => void;
  api.view.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  const result = await show(api);
  fireEvent.click(result.open);
  await waitFor(() => expect(result.open.matches(":disabled")).toBe(false), { timeout: 50 });
  for (const scale of ["1.1", "1.2", "1.3"]) {
    document.documentElement.style.setProperty("--font-scale", scale);
    await act(async () => {});
    await act(async () => flushFrame());
  }
  expect(api.view).toHaveBeenCalledOnce();
  await act(async () => finish(state));
  expect(api.view).toHaveBeenCalledTimes(2);
  expect(api.view.mock.calls[0]?.[1].appearance.scale).toBe(1);
  expect(api.view.mock.calls[1]?.[1].appearance.scale).toBe(1.3);
});
