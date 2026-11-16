/**
 * `AppearancePage`. `onPreview` in these tests plays App's own
 * role (App runs it in an effect): it calls `applyAppearance`
 * itself, so asserting against `document.documentElement` proves the whole wiring, not just that
 * a callback fired.
 */
import { act, cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import { type SettingsView, ShellApiError } from "../../clients/app/shellApi";
import { createShellStore, type ShellStore } from "../../state/shell";
import { AppStore } from "../../state/store";
import type { FakeShellApi } from "../../testShell";
import { fakeShellApi, settingsFixture } from "../../testShell";
import { AppearancePage } from "./AppearancePage";
import { type AppearanceValues, appearanceFromSettings, applyAppearance } from "./appearance";

async function flush(): Promise<void> {
  for (let turn = 0; turn < 10; turn += 1) await Promise.resolve();
}

async function readyStore(
  overrides: Parameters<typeof fakeShellApi>[0] = {},
): Promise<{ api: FakeShellApi; store: ShellStore }> {
  const api = fakeShellApi(overrides);
  const store = createShellStore(api, new AppStore());
  store.start();
  await flush();
  return { api, store };
}

/** Stands in for App's own appearance effect: records every call and actually applies it, so a
 * test can read `document.documentElement`. */
function trackedPreview(committed: AppearanceValues) {
  const calls: (AppearanceValues | null)[] = [];
  const onPreview = vi.fn((values: AppearanceValues | null) => {
    calls.push(values);
    applyAppearance(values ?? committed);
  });
  return { onPreview, calls };
}

function themeSelect(): HTMLSelectElement {
  return screen.getByLabelText("Theme") as HTMLSelectElement;
}
function densitySelect(): HTMLSelectElement {
  return screen.getByLabelText("Spacing") as HTMLSelectElement;
}
function fontScaleSelect(): HTMLSelectElement {
  return screen.getByLabelText("Display size") as HTMLSelectElement;
}
function change(select: HTMLSelectElement, value: string): void {
  select.value = value;
  select.dispatchEvent(new Event("change", { bubbles: true }));
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  const root = document.documentElement;
  delete root.dataset.theme;
  delete root.dataset.density;
  delete root.dataset.motion;
  root.style.removeProperty("--font-scale");
});

describe("preview", () => {
  test("a control change calls onPreview live and applies it to the document at once", async () => {
    const { store } = await readyStore();
    const committed = appearanceFromSettings(store.getSnapshot().settings);
    const { onPreview, calls } = trackedPreview(committed);
    render(<AppearancePage store={store} onPreview={onPreview} />);

    act(() => change(fontScaleSelect(), "1.3"));

    expect(calls.at(-1)).toEqual({ ...committed, font_scale: 1.3 });
    expect(document.documentElement.style.getPropertyValue("--font-scale")).toBe("1.3");
  });

  test("cancel clears the preview, reverts the control, and shows the discard notice, which the next change clears", async () => {
    const { store } = await readyStore();
    const committed = appearanceFromSettings(store.getSnapshot().settings);
    const { onPreview } = trackedPreview(committed);
    render(<AppearancePage store={store} onPreview={onPreview} />);

    act(() => change(densitySelect(), "compact"));
    expect(document.documentElement.dataset.density).toBe("compact");

    act(() => screen.getByRole("button", { name: "Cancel" }).click());
    expect(densitySelect().value).toBe(committed.density);
    expect(document.documentElement.dataset.density).toBe(committed.density);
    expect(screen.getByRole("status").textContent).toBe("Unapplied changes were discarded.");

    act(() => change(fontScaleSelect(), "1.3"));
    expect(screen.queryByText("Unapplied changes were discarded.")).toBeNull();
  });

  test("leaving the page (unmounting) discards a pending preview", async () => {
    const { store } = await readyStore();
    const committed = appearanceFromSettings(store.getSnapshot().settings);
    const { onPreview, calls } = trackedPreview(committed);
    const { unmount } = render(<AppearancePage store={store} onPreview={onPreview} />);

    act(() => change(densitySelect(), "compact"));
    expect(calls.at(-1)).not.toBeNull();

    act(() => unmount());
    expect(calls.at(-1)).toBeNull();
    expect(document.documentElement.dataset.density).toBe(committed.density);
  });
});

describe("apply", () => {
  test("patches exactly the changed keys, at once, and clears the preview on success", async () => {
    const { store, api } = await readyStore();
    const committed = appearanceFromSettings(store.getSnapshot().settings);
    const { onPreview, calls } = trackedPreview(committed);
    render(<AppearancePage store={store} onPreview={onPreview} />);

    act(() => change(densitySelect(), "compact"));
    act(() => change(fontScaleSelect(), "1.3"));

    await act(async () => {
      screen.getByRole("button", { name: "Apply" }).click();
      await flush();
    });

    const patchCall = api.calls.find((c) => c.method === "patchSettings");
    expect(patchCall?.args[2]).toEqual({
      "appearance.density": "compact",
      "accessibility.font_scale": 1.3,
    });
    expect(calls.at(-1)).toBeNull();
  });

  test("a refused patch shows the error and keeps the preview on screen", async () => {
    const { store, api } = await readyStore();
    const { onPreview } = trackedPreview(appearanceFromSettings(store.getSnapshot().settings));
    render(<AppearancePage store={store} onPreview={onPreview} />);

    act(() => change(densitySelect(), "compact"));
    api.fail("patchSettings", new ShellApiError(409, "read_only"));

    await act(async () => {
      screen.getByRole("button", { name: "Apply" }).click();
      await flush();
    });

    expect(screen.getByRole("alert").textContent).toBe(
      "Settings are read-only; see the recovery guidance.",
    );
    expect(densitySelect().value).toBe("compact");
  });

  test("double-clicking Apply sends exactly one patch (the handler checks the guard first)", async () => {
    const { store, api } = await readyStore();
    const { onPreview } = trackedPreview(appearanceFromSettings(store.getSnapshot().settings));
    render(<AppearancePage store={store} onPreview={onPreview} />);

    act(() => change(densitySelect(), "compact"));

    await act(async () => {
      const button = screen.getByRole("button", { name: "Apply" });
      button.click();
      button.click();
      await flush();
    });

    expect(api.calls.filter((c) => c.method === "patchSettings")).toHaveLength(1);
  });

  describe("409 recovery (a conflict on any page must not be a dead end)", () => {
    test("stale_revision adopts the server's current document, keeps the draft dirty, and the next Apply carries the new revision", async () => {
      const { store, api } = await readyStore();
      const { onPreview } = trackedPreview(appearanceFromSettings(store.getSnapshot().settings));
      render(<AppearancePage store={store} onPreview={onPreview} />);

      act(() => change(densitySelect(), "compact"));
      const fresh: SettingsView = settingsFixture(
        { "appearance.density": "comfortable", "accessibility.font_scale": 1.5 },
        { revision: 9 },
      );
      api.fail("patchSettings", new ShellApiError(409, "stale_revision", { current: fresh }));

      await act(async () => {
        screen.getByRole("button", { name: "Apply" }).click();
        await flush();
      });

      expect(store.getSnapshot().settings?.revision).toBe(9);
      // The touched key (density) keeps the user's own edit; the untouched key (font_scale)
      // follows the server's fresher value instead of being silently overwritten later.
      expect(densitySelect().value).toBe("compact");
      expect(fontScaleSelect().value).toBe("1.5");
      expect(screen.getByRole("status").textContent).toBe(
        "Current settings reloaded; apply again.",
      );
      expect(
        screen.getByRole("button", { name: "Apply" }).getAttribute("aria-disabled"),
      ).toBeNull();

      await act(async () => {
        screen.getByRole("button", { name: "Apply" }).click();
        await flush();
      });
      const second = api.calls.filter((c) => c.method === "patchSettings")[1];
      expect(second?.args[1]).toBe(9);
      expect(second?.args[2]).toEqual({ "appearance.density": "compact" });
    });

    test("command_conflict re-reads the document and the next Apply carries the new revision", async () => {
      const { store, api } = await readyStore();
      const { onPreview } = trackedPreview(appearanceFromSettings(store.getSnapshot().settings));
      render(<AppearancePage store={store} onPreview={onPreview} />);

      act(() => change(densitySelect(), "compact"));
      api.data.settings = settingsFixture({}, { revision: 7 });
      api.fail("patchSettings", new ShellApiError(409, "command_conflict"));

      await act(async () => {
        screen.getByRole("button", { name: "Apply" }).click();
        await flush();
      });

      expect(store.getSnapshot().settings?.revision).toBe(7);
      expect(screen.getByRole("status").textContent).toBe(
        "Current settings reloaded; apply again.",
      );

      await act(async () => {
        screen.getByRole("button", { name: "Apply" }).click();
        await flush();
      });
      const second = api.calls.filter((c) => c.method === "patchSettings")[1];
      expect(second?.args[1]).toBe(7);
    });
  });
});

test("restore defaults patches all four keys to null, in one request", async () => {
  const { store, api } = await readyStore({
    settings: settingsFixture({ "appearance.density": "compact" }),
  });
  const { onPreview } = trackedPreview(appearanceFromSettings(store.getSnapshot().settings));
  render(<AppearancePage store={store} onPreview={onPreview} />);

  await act(async () => {
    screen.getByRole("button", { name: "Restore defaults" }).click();
    await flush();
  });

  const patchCall = api.calls.find((c) => c.method === "patchSettings");
  expect(patchCall?.args[2]).toEqual({
    "appearance.theme": null,
    "appearance.density": null,
    "accessibility.font_scale": null,
    "accessibility.reduce_motion": null,
  });
});

test("a fresher document landing mid-edit does not make Apply revert an untouched key (touched keys, not a diff)", async () => {
  const { store, api } = await readyStore();
  const { onPreview } = trackedPreview(appearanceFromSettings(store.getSnapshot().settings));
  render(<AppearancePage store={store} onPreview={onPreview} />);

  // Window A: the user sets density, leaving font_scale untouched.
  act(() => change(densitySelect(), "compact"));

  // Window B (or any other adoption path — a 409 reload, an import apply, afterMutation):
  // commits a fresher document that changed font_scale.
  act(() =>
    store.adoptSettings(
      settingsFixture(
        { "appearance.density": "comfortable", "accessibility.font_scale": 1.5 },
        { revision: 5 },
      ),
    ),
  );

  // The untouched key follows the fresh document; the touched key keeps the user's edit.
  expect(fontScaleSelect().value).toBe("1.5");
  expect(densitySelect().value).toBe("compact");

  await act(async () => {
    screen.getByRole("button", { name: "Apply" }).click();
    await flush();
  });
  const patchCall = api.calls.find((c) => c.method === "patchSettings");
  expect(patchCall?.args[2]).toEqual({ "appearance.density": "compact" });
  expect(patchCall?.args[1]).toBe(5);
});

describe("read-only document (every control is aria-disabled with the notice as its reason)", () => {
  test("every row and button is aria-disabled, named through aria-describedby and title, and no control sends anything", async () => {
    const { store, api } = await readyStore({
      settings: settingsFixture({}, { read_only: true, reason: "newer_version" }),
    });
    const { onPreview, calls } = trackedPreview(
      appearanceFromSettings(store.getSnapshot().settings),
    );
    render(<AppearancePage store={store} onPreview={onPreview} />);

    const density = densitySelect();
    expect(density.getAttribute("aria-disabled")).toBe("true");
    expect(density.title).toBe("Settings are read-only; see the recovery guidance.");
    const reasonId = density.getAttribute("aria-describedby")?.split(" ").at(-1);
    expect(reasonId).not.toBeUndefined();
    expect(document.getElementById(reasonId as string)?.textContent).toBe(
      "Settings are read-only; see the recovery guidance.",
    );

    for (const name of ["Apply", "Cancel", "Restore defaults"]) {
      const button = screen.getByRole("button", { name });
      expect(button.getAttribute("aria-disabled")).toBe("true");
      expect(button.getAttribute("aria-describedby")).toBe(reasonId);
    }

    act(() => change(density, "compact"));
    expect(calls).toHaveLength(0);
    act(() => screen.getByRole("button", { name: "Restore defaults" }).click());
    expect(api.calls.filter((c) => c.method === "patchSettings")).toHaveLength(0);
  });
});

test("the theme select's aria-describedby names only its own description", async () => {
  const { store } = await readyStore();
  const { onPreview } = trackedPreview(appearanceFromSettings(store.getSnapshot().settings));
  render(<AppearancePage store={store} onPreview={onPreview} />);

  const describedBy = themeSelect().getAttribute("aria-describedby") ?? "";
  const ids = describedBy.split(" ");
  expect(ids).toHaveLength(1);
  expect(document.getElementById(ids[0] ?? "")?.textContent).toBe(
    "The colour scheme this app's own screens use.",
  );
});

test("the theme select offers all three themes enabled, and no element carries the old theme_later note", async () => {
  const { store } = await readyStore({
    settings: settingsFixture({ "appearance.theme": "system" }),
  });
  const { onPreview } = trackedPreview(appearanceFromSettings(store.getSnapshot().settings));
  render(<AppearancePage store={store} onPreview={onPreview} />);

  const select = themeSelect();
  const scoped = within(select);
  expect(select.value).toBe("system");
  const systemOption = scoped.getByRole("option", {
    name: "Follow computer settings",
  }) as HTMLOptionElement;
  expect(systemOption.disabled).toBe(false);
  expect(systemOption.selected).toBe(true);
  const lightOption = scoped.getByRole("option", { name: "Light" }) as HTMLOptionElement;
  expect(lightOption.disabled).toBe(false);
  const darkOption = scoped.getByRole("option", { name: "Dark" }) as HTMLOptionElement;
  expect(darkOption.disabled).toBe(false);
  expect(
    screen.queryByText("Light theme arrives in a future update; this version always shows dark."),
  ).toBeNull();
  expect(document.getElementById(`${select.id}-theme-later`)).toBeNull();
});

test("choosing Light and Apply stores it through the store's patch and the preview sets data-theme to light", async () => {
  const { store, api } = await readyStore();
  const committed = appearanceFromSettings(store.getSnapshot().settings);
  const { onPreview } = trackedPreview(committed);
  render(<AppearancePage store={store} onPreview={onPreview} />);

  act(() => change(themeSelect(), "light"));
  expect(document.documentElement.dataset.theme).toBe("light");

  await act(async () => {
    screen.getByRole("button", { name: "Apply" }).click();
    await flush();
  });
  const patchCall = api.calls.find((c) => c.method === "patchSettings");
  expect(patchCall?.args[2]).toEqual({ "appearance.theme": "light" });
});

describe("one card, a .settings-actions bar, and row meta", () => {
  test("every button is btn plus go, alt or txt, or a settings-pill", async () => {
    const { store } = await readyStore();
    const { onPreview } = trackedPreview(appearanceFromSettings(store.getSnapshot().settings));
    const { container } = render(<AppearancePage store={store} onPreview={onPreview} />);

    const buttons = container.querySelectorAll("button");
    expect(buttons.length).toBeGreaterThan(0);
    for (const button of Array.from(buttons)) {
      const classes = button.className.split(/\s+/);
      const ok =
        classes.includes("settings-pill") ||
        (classes.includes("btn") &&
          ["go", "alt", "txt"].some((variant) => classes.includes(variant)));
      expect(ok).toBe(true);
    }
  });

  test("Apply is the go button inside .settings-actions, with Cancel and Restore defaults as alt", async () => {
    const { store } = await readyStore();
    const { onPreview } = trackedPreview(appearanceFromSettings(store.getSnapshot().settings));
    render(<AppearancePage store={store} onPreview={onPreview} />);

    const apply = screen.getByRole("button", { name: "Apply" });
    expect(apply.classList.contains("go")).toBe(true);
    expect(apply.closest(".settings-actions")).not.toBeNull();
    expect(screen.getByRole("button", { name: "Cancel" }).classList.contains("alt")).toBe(true);
    expect(screen.getByRole("button", { name: "Restore defaults" }).classList.contains("alt")).toBe(
      true,
    );
  });

  test("the four rows sit in one settings-card", async () => {
    const { store } = await readyStore();
    const { onPreview } = trackedPreview(appearanceFromSettings(store.getSnapshot().settings));
    render(<AppearancePage store={store} onPreview={onPreview} />);

    const card = themeSelect().closest(".settings-card");
    expect(card).not.toBeNull();
    expect(card?.contains(densitySelect())).toBe(true);
    expect(card?.contains(fontScaleSelect())).toBe(true);
  });

  test("a key set here shows its source in the row meta", async () => {
    const { store } = await readyStore({
      settings: settingsFixture({ "appearance.theme": "dark" }),
    });
    const { onPreview } = trackedPreview(appearanceFromSettings(store.getSnapshot().settings));
    render(<AppearancePage store={store} onPreview={onPreview} />);

    const row = themeSelect().closest(".settings-row");
    expect(row?.querySelector(".settings-row-meta")?.textContent).toBe("Set here");
  });

  test("a default-sourced, now-applied key (density, untouched) renders no row meta at all", async () => {
    const { store } = await readyStore();
    const { onPreview } = trackedPreview(appearanceFromSettings(store.getSnapshot().settings));
    render(<AppearancePage store={store} onPreview={onPreview} />);

    const row = densitySelect().closest(".settings-row");
    expect(row?.querySelector(".settings-row-meta")).toBeNull();
  });
});

describe("loading (this page is reachable with settings still null)", () => {
  test("renders a loading status instead of crashing on a null document", () => {
    const api = fakeShellApi();
    const store = createShellStore(api, new AppStore()); // not started: settings stays null
    render(<AppearancePage store={store} onPreview={() => {}} />);
    expect(screen.getByRole("status").textContent).toBe("Loading…");
  });
});
