/**
 * `appearance.ts`'s mechanism: reading the four keys off a settings document, and applying a set
 * of values to the live document.
 */
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { readCss } from "../../testing/css";
import { settingsFixture } from "../../testShell";
import {
  appearanceFromSettings,
  applyAppearance,
  DEFAULT_APPEARANCE,
  resolveTheme,
  usePrefersLight,
} from "./appearance";

test("the mechanism module imports its own stylesheet, not the page (review: a stored density/scale must not depend on the page ever having been opened)", () => {
  const mechanism = readCss(import.meta.url, "appearance.ts");
  expect(mechanism).toMatch(/import\s+"\.\/appearance\.css";/);
  const page = readCss(import.meta.url, "AppearancePage.tsx");
  expect(page).not.toMatch(/appearance\.css/);
});

beforeEach(() => {
  const root = document.documentElement;
  delete root.dataset.theme;
  delete root.dataset.density;
  delete root.dataset.motion;
  root.style.removeProperty("--font-scale");
});

describe("appearanceFromSettings", () => {
  test("reads the four keys' effective values off the document", () => {
    const settings = settingsFixture({
      "appearance.theme": "dark",
      "appearance.density": "compact",
      "accessibility.font_scale": 1.3,
      "accessibility.reduce_motion": "reduce",
    });
    expect(appearanceFromSettings(settings)).toEqual({
      theme: "dark",
      density: "compact",
      font_scale: 1.3,
      reduce_motion: "reduce",
    });
  });

  test("null settings (no document loaded yet) resolves dark: until a document loads the app renders dark, so the first frame matches today's and a no-backend page stays dark", () => {
    expect(appearanceFromSettings(null)).toEqual({ ...DEFAULT_APPEARANCE, theme: "dark" });
  });

  test("a loaded document with no theme key falls back to the default (system), not dark", () => {
    const settings = settingsFixture();
    const missing = { ...settings, effective: { ...settings.effective } };
    delete missing.effective["appearance.theme"];
    expect(appearanceFromSettings(missing).theme).toBe(DEFAULT_APPEARANCE.theme);
  });

  test("a wrongly-typed effective value (a corrupt or older document) falls back to the default", () => {
    const settings = settingsFixture();
    const corrupt = {
      ...settings,
      effective: { ...settings.effective, "accessibility.font_scale": "1.3" },
    };
    expect(appearanceFromSettings(corrupt).font_scale).toBe(DEFAULT_APPEARANCE.font_scale);
  });
});

describe("resolveTheme", () => {
  test("dark and light pass through regardless of prefersLight", () => {
    expect(resolveTheme("dark", true)).toBe("dark");
    expect(resolveTheme("dark", false)).toBe("dark");
    expect(resolveTheme("light", true)).toBe("light");
    expect(resolveTheme("light", false)).toBe("light");
  });

  test("system follows prefersLight", () => {
    expect(resolveTheme("system", true)).toBe("light");
    expect(resolveTheme("system", false)).toBe("dark");
  });

  test("any other value resolves dark", () => {
    expect(resolveTheme("sepia", true)).toBe("dark");
    expect(resolveTheme("sepia", false)).toBe("dark");
  });
});

describe("usePrefersLight", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  test("returns matchMedia's matches, and re-renders once on a dispatched change", () => {
    let matches = false;
    const listeners = new Set<() => void>();
    vi.stubGlobal("matchMedia", (query: string) => ({
      media: query,
      get matches(): boolean {
        return matches;
      },
      addEventListener: (_type: string, listener: () => void) => {
        listeners.add(listener);
      },
      removeEventListener: (_type: string, listener: () => void) => {
        listeners.delete(listener);
      },
    }));
    const { result } = renderHook(() => usePrefersLight());
    expect(result.current).toBe(false);

    matches = true;
    act(() => {
      for (const listener of [...listeners]) listener();
    });
    expect(result.current).toBe(true);
  });

  test("returns false when matchMedia is not a function", () => {
    vi.stubGlobal("matchMedia", undefined);
    const { result } = renderHook(() => usePrefersLight());
    expect(result.current).toBe(false);
  });
});

describe("applyAppearance", () => {
  test("resolves data-theme from the theme and prefersLight (system true then false)", () => {
    applyAppearance(
      { theme: "system", density: "comfortable", font_scale: 1, reduce_motion: "system" },
      true,
    );
    expect(document.documentElement.dataset.theme).toBe("light");
    applyAppearance(
      { theme: "system", density: "comfortable", font_scale: 1, reduce_motion: "system" },
      false,
    );
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  test("with no prefersLight argument, resolves as if the OS prefers dark", () => {
    applyAppearance({
      theme: "system",
      density: "comfortable",
      font_scale: 1,
      reduce_motion: "system",
    });
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  test("sets data-density to exactly comfortable or compact", () => {
    applyAppearance({ theme: "dark", density: "compact", font_scale: 1, reduce_motion: "system" });
    expect(document.documentElement.dataset.density).toBe("compact");
    applyAppearance({
      theme: "dark",
      density: "comfortable",
      font_scale: 1,
      reduce_motion: "system",
    });
    expect(document.documentElement.dataset.density).toBe("comfortable");
  });

  test("writes --font-scale as a plain number string", () => {
    applyAppearance({
      theme: "dark",
      density: "comfortable",
      font_scale: 1.15,
      reduce_motion: "system",
    });
    expect(document.documentElement.style.getPropertyValue("--font-scale")).toBe("1.15");
  });

  test('sets data-motion="reduce" only for "reduce"; "allow" and "system" remove the attribute', () => {
    applyAppearance({
      theme: "dark",
      density: "comfortable",
      font_scale: 1,
      reduce_motion: "reduce",
    });
    expect(document.documentElement.dataset.motion).toBe("reduce");

    applyAppearance({
      theme: "dark",
      density: "comfortable",
      font_scale: 1,
      reduce_motion: "allow",
    });
    expect(document.documentElement.dataset.motion).toBeUndefined();

    applyAppearance({
      theme: "dark",
      density: "comfortable",
      font_scale: 1,
      reduce_motion: "reduce",
    });
    applyAppearance({
      theme: "dark",
      density: "comfortable",
      font_scale: 1,
      reduce_motion: "system",
    });
    expect(document.documentElement.dataset.motion).toBeUndefined();
  });

  test("is idempotent: the same values applied twice leave the same DOM", () => {
    const values = { theme: "dark", density: "compact", font_scale: 1.5, reduce_motion: "reduce" };
    applyAppearance(values);
    const once = {
      ...document.documentElement.dataset,
      fontScale: document.documentElement.style.getPropertyValue("--font-scale"),
    };
    applyAppearance(values);
    const twice = {
      ...document.documentElement.dataset,
      fontScale: document.documentElement.style.getPropertyValue("--font-scale"),
    };
    expect(twice).toEqual(once);
  });
});
