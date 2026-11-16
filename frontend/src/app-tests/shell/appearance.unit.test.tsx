/**
 * App's appearance effect: with no settings document loaded the
 * app renders dark; a stored `system` theme follows the OS's live `prefers-color-scheme` and
 * flips without a remount; a stored `light` theme ignores the OS preference either way.
 */
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki } from "../../accept/m3b/fakes";
import { fakeDurable } from "../../testDurable";
import { fakeShellApi, settingsFixture } from "../../testShell";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  delete document.documentElement.dataset.theme;
});

/** Lets the shell store's reads settle (they resolve at once on the fake). */
async function settled(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

function stubMatchMedia(initialMatches: boolean): {
  setMatches: (value: boolean) => void;
  dispatchChange: () => void;
} {
  let matches = initialMatches;
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
  return {
    setMatches: (value: boolean) => {
      matches = value;
    },
    dispatchChange: () => {
      act(() => {
        for (const listener of [...listeners]) listener();
      });
    },
  };
}

test("with no settings document loaded, the app renders dark whatever the OS prefers", async () => {
  stubMatchMedia(true);
  const rest = fakeRest({ sessions: [] });
  const { viki } = fakeViki();
  render(<App viki={viki} rest={rest} durable={fakeDurable()} />);
  await settled();
  expect(document.documentElement.dataset.theme).toBe("dark");
  viki.client.close();
});

test("a stored system theme follows the OS preference and flips on change, without a remount", async () => {
  const { setMatches, dispatchChange } = stubMatchMedia(false);
  const rest = fakeRest({ sessions: [] });
  const { viki } = fakeViki();
  const shell = fakeShellApi({ settings: settingsFixture({ "appearance.theme": "system" }) });
  render(<App viki={viki} rest={rest} durable={fakeDurable()} shell={shell} />);
  await settled();
  expect(document.documentElement.dataset.theme).toBe("dark");
  const callsAfterMount = shell.calls.length;

  setMatches(true);
  dispatchChange();
  expect(document.documentElement.dataset.theme).toBe("light");
  // A remount would re-run App's mount effects and read capabilities/settings/workspaces again;
  // the call count staying put proves the flip is a plain re-render.
  expect(shell.calls.length).toBe(callsAfterMount);

  viki.client.close();
});

test("a stored light theme stays light whatever the OS stub says", async () => {
  const { setMatches, dispatchChange } = stubMatchMedia(true);
  const rest = fakeRest({ sessions: [] });
  const { viki } = fakeViki();
  const shell = fakeShellApi({ settings: settingsFixture({ "appearance.theme": "light" }) });
  render(<App viki={viki} rest={rest} durable={fakeDurable()} shell={shell} />);
  await settled();
  expect(document.documentElement.dataset.theme).toBe("light");

  setMatches(false);
  dispatchChange();
  expect(document.documentElement.dataset.theme).toBe("light");

  viki.client.close();
});
