/**
 * The round voice control stands in Send's place in an empty, idle
 * composer and steps aside — back to a plain ghost button beside Send — the moment there is
 * something to send, something running, or a send in flight. VoiceControl is mounted for real
 * (not stubbed), so the tooltip's engine name is exercised against an actual VoiceMenu status.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, expect, test, vi } from "vitest";
import { VoiceControl } from "../../voice/HandsFreeControls";
import type { HandsFreeComposerEvent, HandsFreeState } from "../../voice/handsFree";
import type { UseHandsFree } from "../../voice/useHandsFree";
import type { VoiceboxApi, VoiceboxStatus } from "../../voice/voiceboxApi";
import {
  type VoicePackApi,
  VoicePackContext,
  type VoicePackStatus,
} from "../../voice/voicePackApi";
import { Composer } from "./Composer";

afterEach(() => {
  cleanup();
});

const settled = () =>
  act(async () => {
    for (let i = 0; i < 10; i++) await Promise.resolve();
  });

const OFF: HandsFreeState = { phase: "off", noticeOpen: false, notice: null, turnId: null };

function hf(phase: HandsFreeState["phase"]): UseHandsFree {
  return {
    state: { ...OFF, phase },
    bridge: {
      active: phase !== "off",
      listening: phase === "listening",
      dispatch: vi.fn((_event: HandsFreeComposerEvent) => undefined),
    },
    toggle: vi.fn(),
    confirmNotice: vi.fn(),
    cancelNotice: vi.fn(),
    dismissNotice: vi.fn(),
    interrupt: vi.fn(),
  };
}

/** The same shape App.tsx wires up: VoiceControl for real, with a scripted profile/api. */
function voiceControl(
  phase: HandsFreeState["phase"] = "off",
  profile: string | null = null,
  api?: VoiceboxApi,
) {
  return (primary: boolean) => (
    <VoiceControl
      handsFree={hf(phase)}
      primary={primary}
      settings={{ "voice.voicebox_profile": profile }}
      patch={vi.fn(async () => undefined)}
      openSettings={vi.fn()}
      {...(api !== undefined ? { api } : {})}
    />
  );
}

/** A `VoicePackStatus` fixture: every field defaulted, override just what a test cares about. */
function packStatus(overrides: Partial<VoicePackStatus>): VoicePackStatus {
  return {
    state: "not_installed",
    reason: null,
    error: null,
    progress: null,
    downloadBytes: 0,
    diskBytes: 0,
    needBytes: 0,
    freeBytes: null,
    gpu: null,
    kokoro: false,
    speed: { kokoro: null, qwen: null },
    fallbacks: [],
    ...overrides,
  };
}

/** Renders the real VoiceControl inside a real Composer, with a scripted pack status (via
 * `VoicePackContext`) and/or own-app Voicebox status (via the `api` prop), and returns the
 * tooltip's text once both have had a chance to settle. */
async function tipFor(opts: {
  settings: Readonly<Record<string, unknown>>;
  pack?: VoicePackStatus;
  voicebox?: VoiceboxStatus;
  openMenu?: boolean;
  strict?: boolean;
}): Promise<string> {
  const api: VoiceboxApi | undefined = opts.voicebox
    ? { status: vi.fn(async () => opts.voicebox as VoiceboxStatus), speech: vi.fn() }
    : undefined;
  const packApi: VoicePackApi | undefined = opts.pack
    ? {
        status: vi.fn(async () => opts.pack as VoicePackStatus),
        act: vi.fn(async () => opts.pack as VoicePackStatus),
        openSpeechSettings: vi.fn(async () => {}),
      }
    : undefined;
  const { container } = render(
    <VoicePackContext.Provider value={packApi ? { api: packApi, openUrl: vi.fn() } : null}>
      <Composer
        running={false}
        onSend={onSend()}
        onStop={vi.fn()}
        voiceControl={(primary) => (
          <VoiceControl
            handsFree={hf("off")}
            primary={primary}
            settings={opts.settings}
            patch={vi.fn(async () => undefined)}
            openSettings={vi.fn()}
            {...(api !== undefined ? { api } : {})}
          />
        )}
      />
    </VoicePackContext.Provider>,
    opts.strict ? { wrapper: StrictMode } : {},
  );
  await settled();
  if (opts.openMenu) {
    fireEvent.click(screen.getByRole("button", { name: "Voice options" }));
    await settled();
  }
  return tipText(container);
}

const onSend = () => vi.fn(async (_text: string) => true);

function submits(container: HTMLElement): NodeListOf<Element> {
  return container.querySelectorAll(".composer-submit");
}

function tipText(container: HTMLElement): string {
  return container.querySelector(".hands-free-tip")?.textContent ?? "";
}

test("shows the voice button in Send's place", () => {
  const { container } = render(
    <Composer running={false} onSend={onSend()} onStop={vi.fn()} voiceControl={voiceControl()} />,
  );
  const toggle = screen.getByRole("button", { name: "Hands-free" });
  expect(submits(container)).toHaveLength(1);
  expect(toggle.classList.contains("composer-submit")).toBe(true);
  expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
  expect(tipText(container)).toBe("Start voice conversation — system voice");
});

test("text in the box steps the voice button aside and brings Send back", () => {
  render(
    <Composer
      running={false}
      onSend={onSend()}
      onStop={vi.fn()}
      voiceControl={voiceControl()}
      value="hello"
      onEdit={() => {}}
    />,
  );
  expect(screen.getByRole("button", { name: "Send" })).not.toBeNull();
  expect(
    screen.getByRole("button", { name: "Hands-free" }).classList.contains("composer-submit"),
  ).toBe(false);
});

test("an attachment steps the voice button aside and brings Send back", () => {
  render(
    <Composer
      running={false}
      onSend={onSend()}
      onStop={vi.fn()}
      voiceControl={voiceControl()}
      hasAttachments
    />,
  );
  expect(screen.getByRole("button", { name: "Send" })).not.toBeNull();
  expect(
    screen.getByRole("button", { name: "Hands-free" }).classList.contains("composer-submit"),
  ).toBe(false);
});

test("an excerpt steps the voice button aside and brings Send back", () => {
  render(
    <Composer
      running={false}
      onSend={onSend()}
      onStop={vi.fn()}
      voiceControl={voiceControl()}
      excerpts={[
        {
          kind: "vault_excerpt",
          vault_id: "v1",
          ref: "note.md",
          start_line: 1,
          end_line: 2,
          sha256: "x",
        },
      ]}
    />,
  );
  expect(screen.getByRole("button", { name: "Send" })).not.toBeNull();
  expect(
    screen.getByRole("button", { name: "Hands-free" }).classList.contains("composer-submit"),
  ).toBe(false);
});

test("running shows Stop and the ghost voice button, never Send", () => {
  render(
    <Composer running={true} onSend={onSend()} onStop={vi.fn()} voiceControl={voiceControl()} />,
  );
  expect(screen.getByRole("button", { name: "Stop" })).not.toBeNull();
  expect(
    screen.getByRole("button", { name: "Hands-free" }).classList.contains("composer-submit"),
  ).toBe(false);
  expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
});

test("a send in flight keeps Send, alongside the ghost voice button", async () => {
  let resolveSend!: (value: boolean) => void;
  const pendingSend = vi.fn(
    () =>
      new Promise<boolean>((resolve) => {
        resolveSend = resolve;
      }),
  );
  render(
    <Composer
      running={false}
      onSend={pendingSend}
      onStop={vi.fn()}
      voiceControl={voiceControl()}
      value="hi"
      onEdit={() => {}}
    />,
  );
  fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter" });
  await settled();
  expect(screen.getByRole("button", { name: "Send" })).not.toBeNull();
  expect(
    screen.getByRole("button", { name: "Hands-free" }).classList.contains("composer-submit"),
  ).toBe(false);
  await act(async () => {
    resolveSend(true);
  });
  await settled();
});

test("listening shows the button pressed, with the Esc hint in its tip", () => {
  const { container } = render(
    <Composer
      running={false}
      onSend={onSend()}
      onStop={vi.fn()}
      voiceControl={voiceControl("listening")}
    />,
  );
  const toggle = screen.getByRole("button", { name: "Hands-free" });
  expect(toggle.getAttribute("aria-pressed")).toBe("true");
  expect(toggle.classList.contains("composer-submit")).toBe(true);
  expect(tipText(container)).toBe("End voice conversation (Esc) — system voice");
});

test("a Voicebox status naming the chosen profile names the engine in the tip", async () => {
  const status: VoiceboxStatus = {
    state: "ready",
    gpu: null,
    profiles: [{ id: "p1", name: "Emma", language: "en" }],
  };
  const api: VoiceboxApi = {
    status: vi.fn(async () => status),
    speech: vi.fn(async () => new ArrayBuffer(0)),
  };
  const { container } = render(
    <Composer
      running={false}
      onSend={onSend()}
      onStop={vi.fn()}
      voiceControl={voiceControl("off", "p1", api)}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Voice options" }));
  await settled();
  expect(tipText(container)).toBe("Start voice conversation — Voicebox · Emma");
});

test("with no voiceControl prop, Send renders as it always has", () => {
  render(<Composer running={false} onSend={onSend()} onStop={vi.fn()} />);
  expect(screen.getByRole("button", { name: "Send" })).not.toBeNull();
  expect(screen.queryByRole("button", { name: "Hands-free" })).toBeNull();
});

test("the tip says natural voice for an installed pack", async () => {
  expect(
    await tipFor({
      settings: { "voice.speech_engine": "pack" },
      pack: packStatus({ state: "ready" }),
    }),
  ).toBe("Start voice conversation — natural voice");
});

test("the tip says natural voice while the pack status is still unknown", async () => {
  expect(await tipFor({ settings: { "voice.speech_engine": "pack" } })).toBe(
    "Start voice conversation — natural voice",
  );
});

test("the tip says system voice, needing repair, for a failed pack", async () => {
  expect(
    await tipFor({
      settings: { "voice.speech_engine": "pack" },
      pack: packStatus({ state: "failed" }),
    }),
  ).toBe("Start voice conversation — system voice (natural voices need repair)");
});

test("under StrictMode's development remount, the pack status still reaches the tip", async () => {
  expect(
    await tipFor({
      settings: { "voice.speech_engine": "pack" },
      pack: packStatus({ state: "failed" }),
      strict: true,
    }),
  ).toBe("Start voice conversation — system voice (natural voices need repair)");
});

test("the tip says system voice, not installed, before the pack is installed", async () => {
  expect(
    await tipFor({
      settings: { "voice.speech_engine": "pack" },
      pack: packStatus({ state: "not_installed" }),
    }),
  ).toBe("Start voice conversation — system voice (natural voices aren't installed)");
});

test("the tip names the Voicebox app, unchecked, before any status check", async () => {
  expect(await tipFor({ settings: { "voice.speech_engine": "voicebox" } })).toBe(
    "Start voice conversation — Voicebox app, if it's running",
  );
});

test("the tip says the chosen Voicebox voice is gone once a ready check doesn't list it", async () => {
  expect(
    await tipFor({
      settings: { "voice.voicebox_profile": "ghost" },
      voicebox: { state: "ready", gpu: null, profiles: [] },
      openMenu: true,
    }),
  ).toBe("Start voice conversation — system voice (the chosen Voicebox voice is gone)");
});

test("the tip says Voicebox isn't running once the own app answers unreachable", async () => {
  expect(
    await tipFor({
      settings: { "voice.voicebox_profile": "p1" },
      voicebox: { state: "unreachable", gpu: null, profiles: [] },
      openMenu: true,
    }),
  ).toBe("Start voice conversation — system voice (Voicebox isn't running)");
});
