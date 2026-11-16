/**
 * The App-level identity provider: the settings document's two identity keys reach the top bar,
 * the right rail, a reply and configured portraits through one IdentityContext, and a fake
 * `avatars` client resolves a portrait to an object URL.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { App } from "../../App";
import { fakeRest, fakeViki } from "../../accept/m3b/fakes";
import type { KeySpec, SettingsView } from "../../clients/app/shellApi";
import type { AvatarsApi } from "../../clients/media/avatarsApi";
import type { Envelope } from "../../protocol";
import { fakeDurable } from "../../testDurable";
import {
  capabilitiesFixture,
  fakeShellApi,
  listingFixture,
  settingsFixture,
} from "../../testShell";

const GENERATION = "11111111-1111-4111-8111-111111111111";
const AVATAR_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";

beforeEach(() => {
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
  vi.restoreAllMocks();
});

/** `settingsFixture`'s table predates the two avatar keys (frozen for this card): a `KeySpec`
 * this test adds itself, matching `DocumentPage.unit.test.tsx`'s `withAvatarKeys`. */
const AVATAR_KEY_SPEC: KeySpec = {
  scope: "app",
  applies: "now",
  kind: "string",
  allowed: null,
  default: null,
  personal: true,
};

function mount(options: { settings?: Record<string, unknown>; avatars?: AvatarsApi } = {}) {
  const rest = fakeRest();
  const { viki, socket } = fakeViki();
  const durable = fakeDurable();
  const base = settingsFixture({
    "onboarding.completed": true,
    ...options.settings,
  });
  // The two avatar-id keys are not in testShell.ts's key registry (frozen for this card), so
  // `settingsFixture` never carries them in `keys`, `sources` or `effective`; this test's own
  // settings document adds them directly, the same way a real document would once the registry
  // catches up (both `keys` and `sources`, so a reset patch's default lookup finds them too).
  const settings: SettingsView = {
    ...base,
    keys: {
      ...base.keys,
      "identity.user_avatar_id": AVATAR_KEY_SPEC,
      "identity.assistant_avatar_id": AVATAR_KEY_SPEC,
    },
    sources: {
      ...base.sources,
      "identity.user_avatar_id": "default",
      "identity.assistant_avatar_id": "default",
    },
    effective: { ...base.effective, ...options.settings },
  };
  const shell = fakeShellApi({
    capabilities: capabilitiesFixture(),
    workspaces: listingFixture(),
    settings,
  });
  const utils = render(
    <App
      viki={viki}
      rest={rest}
      durable={durable}
      shell={shell}
      {...(options.avatars ? { avatars: options.avatars } : {})}
    />,
  );
  return { ...utils, viki, socket };
}

/** Lets the shell store's capabilities/settings/workspaces reads settle (the fake resolves them
 * at once). */
async function settled(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

function userMessage(sessionId: string, seq: number): Envelope {
  return {
    v: 2,
    generation: GENERATION,
    request_id: "",
    type: "user.message",
    session_id: sessionId,
    seq,
    data: { text: "hi", command: "", at: "2026-09-27T10:00:00.000Z" },
  };
}

function textDelta(sessionId: string, seq: number): Envelope {
  return {
    v: 2,
    generation: GENERATION,
    request_id: "",
    type: "text.delta",
    session_id: sessionId,
    seq,
    data: { block_id: "b1", text: "hello" },
  };
}

test("identity.assistant_name renders at the top bar, the right rail and a reply", async () => {
  const { container, socket } = mount({ settings: { "identity.assistant_name": "Aoi" } });
  await settled();
  act(() => socket.open());
  socket.sent.length = 0;
  fireEvent.click(screen.getByText("New session"));
  const opened = socket.sent[0] as { session_id: string };
  act(() => socket.receive(userMessage(opened.session_id, 1)));
  act(() => socket.receive(textDelta(opened.session_id, 2)));

  expect(container.querySelector('[data-region="wordmark"]')?.textContent).toBe("Aoi");
  expect(container.querySelector(".right-rail-name")?.textContent).toContain("Aoi");
  expect(container.querySelector(".reply-name")?.textContent).toBe("Aoi");
});

test("identity.assistant_avatar_id with a fake avatars client shows the object URL on the face", async () => {
  vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:test-avatar");
  vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
  const variant = vi.fn<AvatarsApi["variant"]>(async () => new Blob(["x"]));
  const avatars: AvatarsApi = { transform: vi.fn<AvatarsApi["transform"]>(), variant };
  const { container } = mount({
    settings: { "identity.assistant_avatar_id": AVATAR_ID },
    avatars,
  });
  await settled();

  await waitFor(() => {
    const img = container.querySelector(".face-img") as HTMLImageElement;
    expect(img.src).toContain("blob:test-avatar");
  });
  expect(variant).toHaveBeenCalledWith(AVATAR_ID, expect.any(Number), expect.anything());
});

test("with neither identity key set, the built-in name renders without the old portrait", async () => {
  const { container } = mount();
  await settled();
  expect(container.querySelector('[data-region="wordmark"]')?.textContent).toBe("VIKI Agent");
  expect(container.querySelector(".face-img")).toBeNull();
  expect(container.querySelector('img[src^="/api/face/"]')).toBeNull();
});

test("a settings change back to null restores the built-in name", async () => {
  const { container } = mount({ settings: { "identity.assistant_name": "Aoi" } });
  await settled();
  expect(container.querySelector('[data-region="wordmark"]')?.textContent).toBe("Aoi");

  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "App menu" }));
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("menuitem", { name: /^Settings\s*Ctrl/ }));
  });
  const resetButton = document.querySelector(
    '[data-setting-key="identity.assistant_name"] .settings-reset',
  ) as HTMLElement;
  await act(async () => {
    fireEvent.click(resetButton);
    await Promise.resolve();
    await Promise.resolve();
  });

  expect(container.querySelector('[data-region="wordmark"]')?.textContent).toBe("VIKI Agent");
});

test("resetting the custom avatar removes it without restoring the old portrait", async () => {
  vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:test-avatar");
  vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
  const variant = vi.fn<AvatarsApi["variant"]>(async () => new Blob(["x"]));
  const avatars: AvatarsApi = { transform: vi.fn<AvatarsApi["transform"]>(), variant };
  const { container } = mount({
    settings: { "identity.assistant_avatar_id": AVATAR_ID },
    avatars,
  });
  await settled();
  await waitFor(() => {
    const img = container.querySelector(".face-img") as HTMLImageElement;
    expect(img.src).toContain("blob:test-avatar");
  });

  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "App menu" }));
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("menuitem", { name: /^Settings\s*Ctrl/ }));
  });
  const row = document.querySelector(
    '[data-setting-key="identity.assistant_avatar_id"]',
  ) as HTMLElement;
  const resetButton = within(row).getByRole("button", { name: /^reset$/i });
  await act(async () => {
    fireEvent.click(resetButton);
    await Promise.resolve();
    await Promise.resolve();
  });

  // Settings renders with no right rail (App.tsx's non-chat views carry neither rail), and the
  // top bar shows only its text identity: back on the chat view,
  // the right rail shows the reset having taken effect by omitting the custom portrait.
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
  });
  await waitFor(() => {
    expect(container.querySelector(".right-rail")).not.toBeNull();
    expect(container.querySelector(".right-rail .face")).toBeNull();
    expect(container.querySelector('img[src^="/api/face/"]')).toBeNull();
  });
});
