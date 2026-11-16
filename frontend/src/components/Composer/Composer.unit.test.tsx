/**
 * Unit tests pinning behaviours the Composer docstring requires and
 * src/accept/m2c/composer.accept.test.tsx (frozen) never observes: the trailing-newline strip,
 * the box shrinking back when the text does, the caret-and-focus landing at the end of a pushed
 * draft, a refused send's text coming back without overwriting anything, and the slash menu
 * keeping its hands off the keyboard while something outside the composer has focus.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { Command } from "../../protocol";
import type { ExcerptApi, VaultExcerpt, VaultExcerptPart } from "../../state/excerptApi";
import { DictationContext } from "../../voice/dictationContext";
import type { DictationApi, VoiceSocketHandlers, VoiceStatus } from "../../voice/dictationTypes";
import { DEFAULT_IDENTITY, IdentityContext } from "../Avatar/identity";
import { Composer } from "./Composer";
import { withExcerpt } from "./ExcerptChips";

afterEach(cleanup);

test("only the focused pane's slash menu reads the composer's keyboard", () => {
  const alphaEdit = vi.fn();
  const betaEdit = vi.fn();
  const command = (name: string) => [{ name, description: name, argument_hint: "", skill: false }];
  render(
    <>
      <Composer
        focused={false}
        value="/"
        onEdit={alphaEdit}
        commands={command("alpha")}
        running={false}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />
      <Composer
        focused
        value="/"
        onEdit={betaEdit}
        commands={command("beta")}
        running={false}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />
    </>,
  );
  const beta = screen.getAllByRole("textbox")[1] as HTMLElement;
  beta.focus();
  fireEvent.keyDown(beta, { key: "Enter" });
  expect(betaEdit).toHaveBeenCalledWith("/beta ", 6, 6);
  expect(alphaEdit).not.toHaveBeenCalled();
});

test.each(["focus", "dictation admission"])(
  "losing %s cancels dictation without replacing the box or reloading capabilities",
  async (withdrawal) => {
    let handlers: VoiceSocketHandlers | undefined;
    const cancel = vi.fn();
    const api: DictationApi = {
      status: vi.fn(
        async (): Promise<VoiceStatus> => ({
          state: "ready",
          running: false,
          model: "small-q8_0",
          backend: "cpu",
          device: null,
          languages: ["auto"],
          language: "auto",
          language_unavailable: false,
          models: ["small-q8_0"],
          backends: ["cpu"],
          fallback: null,
          cpu_model: "small-q8_0",
          slow_on_cpu: false,
        }),
      ),
      clearFallback: vi.fn(),
      socket: vi.fn((next) => {
        handlers = next;
        return { begin: vi.fn(), audio: vi.fn(), end: vi.fn(), cancel, close: vi.fn() };
      }),
      startCapture: vi.fn(async () => ({ deviceId: null, stop: vi.fn(async () => {}) })),
      microphones: vi.fn(async () => []),
      workletUrl: "/worklet.js",
    };
    const view = (focused: boolean, dictationAllowed = true) => (
      <DictationContext.Provider value={api}>
        <Composer
          focused={focused}
          dictationAllowed={dictationAllowed}
          {...(!dictationAllowed
            ? { handsFree: { active: true, listening: true, dispatch: vi.fn() } }
            : {})}
          running={false}
          onSend={vi.fn()}
          onStop={vi.fn()}
        />
      </DictationContext.Provider>
    );
    const { container, rerender } = render(view(false));
    await act(async () => {
      for (let i = 0; i < 8; i++) await Promise.resolve();
    });
    const box = container.querySelector("textarea");
    const microphone = container.querySelector(".voice-mic") as Element;
    fireEvent.click(microphone);
    expect(api.startCapture).not.toHaveBeenCalled();
    rerender(view(true));
    expect(container.querySelector(".voice-mic")).toBe(microphone);
    await act(async () => {
      fireEvent.click(microphone);
    });
    expect(handlers).toBeDefined();
    rerender(view(withdrawal !== "focus", withdrawal !== "dictation admission"));
    await act(async () => {
      await Promise.resolve();
    });
    expect(cancel).toHaveBeenCalledTimes(1);
    await act(async () => handlers?.closed(1000));
    expect(container.querySelector("textarea")).toBe(box);
    expect(api.status).toHaveBeenCalledTimes(1);
    if (withdrawal === "dictation admission") {
      fireEvent.pointerDown(microphone, { pointerId: 1, button: 0 });
      fireEvent.pointerUp(microphone, { pointerId: 1, button: 0 });
      fireEvent.click(microphone, { detail: 1 });
    }
    fireEvent.click(microphone);
    expect(api.socket).toHaveBeenCalledTimes(1);
    expect(api.startCapture).toHaveBeenCalledTimes(1);
    if (withdrawal === "dictation admission") {
      rerender(view(true));
      await act(async () => fireEvent.click(microphone));
      expect(api.startCapture).toHaveBeenCalledTimes(2);
    }
  },
);

/** jsdom lays nothing out, so `document.documentElement.clientHeight` reads 0 and the box's cap
 * would be 0 in every test here. 900 is the window every other test in this repo assumes. */
function setDocumentHeight(px: number): void {
  Object.defineProperty(document.documentElement, "clientHeight", {
    value: px,
    configurable: true,
  });
}

beforeEach(() => {
  setDocumentHeight(900);
  // Redefined by the test that proves which of the two heights is read; restored here so the
  // order tests run in cannot change what any of them measures.
  Object.defineProperty(window, "innerHeight", { value: 768, configurable: true });
  // The root scale is an inline property on a document every test in this file shares.
  document.documentElement.style.removeProperty("--font-scale");
});

test("a rejected asynchronous handoff retains exact text and permits an explicit new attempt", async () => {
  const onSend = vi.fn(async () => false);
  const { box } = mount({ onSend });
  fireEvent.change(box, { target: { value: "unsent\n" } });
  await act(async () => {
    fireEvent.keyDown(box, { key: "Enter" });
  });
  expect(box.value).toBe("unsent\n");
  expect(onSend).toHaveBeenCalledTimes(1);
  await act(async () => {
    fireEvent.keyDown(box, { key: "Enter" });
  });
  expect(onSend).toHaveBeenCalledTimes(2);
});

test("a delayed handoff cannot clear a different selected editor", async () => {
  let finish: (result: boolean) => void = () => {};
  const onSend = vi.fn(
    () =>
      new Promise<boolean>((resolve) => {
        finish = resolve;
      }),
  );
  const { box, rerender } = mount({ editorKey: "a", onSend });
  fireEvent.change(box, { target: { value: "keep after selection" } });
  fireEvent.keyDown(box, { key: "Enter" });
  rerender(<Composer editorKey="b" running={false} onSend={onSend} onStop={vi.fn()} />);
  await act(async () => {
    finish(true);
  });
  expect(box.value).toBe("keep after selection");
  expect(onSend).toHaveBeenCalledTimes(1);
});

function mount(props: Partial<Parameters<typeof Composer>[0]> = {}) {
  const onSend = vi.fn(async () => true);
  const onStop = vi.fn();
  const { container, rerender } = render(
    <Composer running={false} onSend={onSend} onStop={onStop} {...props} />,
  );
  const box = container.querySelector("textarea") as HTMLTextAreaElement;
  return { container, box, onSend, onStop, rerender };
}

function blockedText(container: HTMLElement): string | null {
  return container.querySelector(".composer-blocked")?.textContent ?? null;
}

/** jsdom always reports scrollHeight 0, so the box's own measurement has to be scripted. */
function setScrollHeight(box: HTMLTextAreaElement, px: number): void {
  Object.defineProperty(box, "scrollHeight", { value: px, configurable: true });
}

// The document's own height is the measurement; the window is only the never-laid-out fallback
// (jsdom reports clientHeight 0, and a cap of 0 would collapse the box). In a real browser the
// two agree — see the test below — so this pins which one is asked first, not a scale.
test("the cap reads the document's own height, never the window's", () => {
  setDocumentHeight(600);
  Object.defineProperty(window, "innerHeight", { value: 1200, configurable: true });
  const { box } = mount();
  setScrollHeight(box, 1000);

  fireEvent.change(box, { target: { value: "a very tall draft" } });

  expect(box.style.height).toBe("240px"); // 0.4 x 600, not 0.4 x 1200
});

// Measured in real Edge: the appearance setting scales the page with
// `html { zoom: var(--font-scale) }`, and Chromium's standardized root zoom divides
// NEITHER `clientHeight` NOR `innerHeight` — both stay the window's pixels — while `scrollHeight`
// and `style.height` are the zoomed element's. Without the divisor the box reaches 60% of the
// visible window at scale 1.5, and the composer must never take more than 40% of it.
test("the cap divides by the root font scale, so 40% still means 40% of the window", () => {
  document.documentElement.style.setProperty("--font-scale", "1.5");
  const { box } = mount();
  setScrollHeight(box, 1000);

  fireEvent.change(box, { target: { value: "a very tall draft" } });

  // 240 local pixels is 360 on screen: 0.4 of the 900px window, the same fraction as at scale 1.
  expect(box.style.height).toBe("240px");
});

test("Shift+Enter's trailing newline is stripped before sending, interior ones are not", () => {
  const { box, onSend } = mount();
  fireEvent.change(box, { target: { value: "・milk\n・eggs\n\n" } });
  fireEvent.keyDown(box, { key: "Enter" });
  expect(onSend).toHaveBeenCalledWith("・milk\n・eggs");
});

test("a text of only newlines never sends: it is empty once trimmed", () => {
  const { box, onSend } = mount();
  fireEvent.change(box, { target: { value: "\n\n" } });
  fireEvent.keyDown(box, { key: "Enter" });
  expect(onSend).not.toHaveBeenCalled();
});

test("the box shrinks back when the text does, not just grows", () => {
  const { box } = mount();
  setScrollHeight(box, 200);
  fireEvent.change(box, { target: { value: "line\nline\nline\nline" } });
  expect(box.style.height).toBe("200px");

  // The height must be released before the next measurement, or scrollHeight would still
  // report the taller box and the composer would never come back down.
  setScrollHeight(box, 54);
  fireEvent.change(box, { target: { value: "line" } });
  expect(box.style.height).toBe("54px");
});

test("a pushed draft focuses the box and leaves the caret at the end of it", () => {
  const { box, rerender } = mount();
  rerender(
    <Composer
      running={false}
      onSend={vi.fn()}
      onStop={vi.fn()}
      draft="Help me decide on the H1 /recall skill."
    />,
  );
  expect(document.activeElement).toBe(box);
  expect(box.selectionStart).toBe(box.value.length);
  expect(box.selectionEnd).toBe(box.value.length);
});

test("ArrowUp recall also lands the caret at the end", () => {
  const { box } = mount({ lastPrompt: "prime directives?" });
  fireEvent.keyDown(box, { key: "ArrowUp" });
  expect(box.value).toBe("prime directives?");
  expect(box.selectionStart).toBe("prime directives?".length);
});

test("a draft the box already holds does not strand the caret move for a later keystroke", () => {
  const { box, rerender } = mount({ draft: "First draft" });
  fireEvent.change(box, { target: { value: "My own words" } });
  box.blur();

  // A new draft by value, but the same text the box already holds. React discards a setText
  // equal to the current value, so no layout effect runs to disarm the flag: arming it would
  // strand it, and the user's next unrelated keystroke would steal focus and jump the caret.
  rerender(<Composer running={false} onSend={vi.fn()} onStop={vi.fn()} draft="My own words" />);
  fireEvent.change(box, { target: { value: "My own words, more" } });
  expect(document.activeElement).not.toBe(box);
});

function command(name: string): Command {
  return { name, description: "", argument_hint: "", skill: false };
}

test("a refused send's text comes back into the box it was cleared from", async () => {
  const { box, rerender } = mount();
  fireEvent.change(box, { target: { value: "A long paragraph" } });
  fireEvent.keyDown(box, { key: "Enter" });
  await act(async () => {});
  expect(box.value).toBe(""); // local handoff completed
  rerender(
    <Composer running={false} onSend={vi.fn()} onStop={vi.fn()} refused="A long paragraph" />,
  );
  expect(box.value).toBe("A long paragraph");
  // Ready to try again or edit it down, without a click: the same courtesy a gate pick gets.
  expect(document.activeElement).toBe(box);
  expect(box.selectionStart).toBe("A long paragraph".length);
});

test("a refused send never overwrites what the user has typed since", () => {
  // The server's live_elsewhere guard runs at the moment the text is in flight, so the refusal
  // can land after they have started the next message. Returning the first must not destroy that.
  const { box, rerender } = mount();
  fireEvent.change(box, { target: { value: "The next message" } });
  rerender(
    <Composer running={false} onSend={vi.fn()} onStop={vi.fn()} refused="The refused paragraph" />,
  );
  expect(box.value).toBe("The next message");
});

test("the slash menu owns Escape and the arrows while the composer has focus", () => {
  const { box } = mount({ commands: [command("retro"), command("recap")] });
  fireEvent.change(box, { target: { value: "/re" } });
  box.focus();
  const seen: string[] = [];
  const overlay = (event: KeyboardEvent) => seen.push(event.key);
  window.addEventListener("keydown", overlay); // the bubble form every overlay here uses
  try {
    fireEvent.keyDown(window, { key: "ArrowDown" });
    fireEvent.keyDown(window, { key: "Escape" });
    expect(seen).toEqual([]);
  } finally {
    window.removeEventListener("keydown", overlay);
  }
});

test("with focus on an overlay outside the composer, the menu leaves those keys alone", () => {
  // The menu's open state is derived from the composer's TEXT alone, so a half-typed command
  // stays mounted under the search palette, a close confirmation or a row menu. Its listener is
  // the only capture-phase one at `window` in this app, so a stopPropagation() here ends the
  // event for the whole page: the overlay's own Escape never fires, its arrows never move, and
  // Enter picks a command instead of activating the button the user has focused.
  const { box, onSend } = mount({ commands: [command("retro"), command("recap")] });
  fireEvent.change(box, { target: { value: "/re" } });
  const elsewhere = document.createElement("button");
  document.body.appendChild(elsewhere);
  const seen: string[] = [];
  const overlay = (event: KeyboardEvent) => seen.push(event.key);
  window.addEventListener("keydown", overlay);
  try {
    elsewhere.focus();
    fireEvent.keyDown(window, { key: "ArrowDown" });
    fireEvent.keyDown(window, { key: "Enter" });
    fireEvent.keyDown(window, { key: "Escape" });
    expect(seen).toEqual(["ArrowDown", "Enter", "Escape"]);
    // Nothing of the composer's moved either: no pick rewrote the box, no send went out, and
    // focus stayed in the overlay rather than being dragged down to the textarea behind it.
    expect(box.value).toBe("/re");
    expect(onSend).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(elsewhere);
  } finally {
    window.removeEventListener("keydown", overlay);
    elsewhere.remove();
  }
});

test("the menu comes back unchanged once the overlay gives focus up", () => {
  // Suppression must not write closedSlashText: dismissing a palette would otherwise kill the
  // menu for that exact text, and the user would have to retype a character to get it back.
  const { box } = mount({ commands: [command("retro"), command("recap")] });
  fireEvent.change(box, { target: { value: "/re" } });
  const elsewhere = document.createElement("button");
  document.body.appendChild(elsewhere);
  elsewhere.focus();
  fireEvent.keyDown(window, { key: "Escape" });
  elsewhere.remove();
  box.focus();
  const onClose: string[] = [];
  const overlay = () => onClose.push("seen");
  window.addEventListener("keydown", overlay);
  try {
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toEqual([]); // still mounted, still consuming: it was never dismissed
  } finally {
    window.removeEventListener("keydown", overlay);
  }
});

test("a blocked Enter with text shows the default reason and sends nothing", () => {
  const { box, onSend, container } = mount({ submitDisabled: true });
  fireEvent.change(box, { target: { value: "hello" } });
  fireEvent.keyDown(box, { key: "Enter" });
  expect(onSend).not.toHaveBeenCalled();
  expect(blockedText(container)).toBe(
    "You can't send yet. Check the notes around the message box.",
  );
});

test("a blocked Enter shows the caller's own blockedReason instead", () => {
  const { box, onSend, container } = mount({
    submitDisabled: true,
    blockedReason: "Checking the image",
  });
  fireEvent.change(box, { target: { value: "hello" } });
  fireEvent.keyDown(box, { key: "Enter" });
  expect(onSend).not.toHaveBeenCalled();
  expect(blockedText(container)).toBe("Checking the image");
});

test("a blocked Enter while running says only to wait", () => {
  const { box, onSend, container } = mount({ running: true });
  fireEvent.change(box, { target: { value: "hello" } });
  fireEvent.keyDown(box, { key: "Enter" });
  expect(onSend).not.toHaveBeenCalled();
  expect(blockedText(container)).toBe("A reply is still running. Send when it ends.");
});

test("the blocked notice clears once submitDisabled lifts, without sending anything by itself", () => {
  const { box, onSend, onStop, container, rerender } = mount({ submitDisabled: true });
  fireEvent.change(box, { target: { value: "hello" } });
  fireEvent.keyDown(box, { key: "Enter" });
  expect(blockedText(container)).not.toBeNull();
  rerender(<Composer running={false} submitDisabled={false} onSend={onSend} onStop={onStop} />);
  expect(blockedText(container)).toBeNull();
  expect(onSend).not.toHaveBeenCalled();
});

test("the blocked notice clears once submitDisabled lifts, though a reply still runs", () => {
  const { box, onSend, onStop, container, rerender } = mount({
    running: true,
    submitDisabled: true,
  });
  fireEvent.change(box, { target: { value: "hello" } });
  fireEvent.keyDown(box, { key: "Enter" });
  expect(blockedText(container)).not.toBeNull();
  rerender(<Composer running={true} submitDisabled={false} onSend={onSend} onStop={onStop} />);
  expect(blockedText(container)).toBeNull();
  fireEvent.keyDown(box, { key: "Enter" });
  expect(blockedText(container)).toBe("A reply is still running. Send when it ends.");
});

test("100,000 trailing newlines strip to the exact content", () => {
  const { box, onSend } = mount();
  fireEvent.change(box, { target: { value: `content${"\n".repeat(100_000)}` } });
  fireEvent.keyDown(box, { key: "Enter" });
  expect(onSend).toHaveBeenCalledWith("content");
});

test("an interior run of newlines is sent unchanged", () => {
  const { box, onSend } = mount();
  const value = `a${"\n".repeat(100_000)}b`;
  fireEvent.change(box, { target: { value } });
  fireEvent.keyDown(box, { key: "Enter" });
  expect(onSend).toHaveBeenCalledWith(value);
});

test("running with submitDisabled says why, not that a reply is running", () => {
  const { box, onSend, container } = mount({
    running: true,
    submitDisabled: true,
  });
  fireEvent.change(box, { target: { value: "hello" } });
  fireEvent.keyDown(box, { key: "Enter" });
  expect(onSend).not.toHaveBeenCalled();
  expect(blockedText(container)).toBe(
    "You can't send yet. Check the notes around the message box.",
  );
});

test("running with a still-pending send says only to wait", async () => {
  let finish: (result: boolean) => void = () => {};
  const onSend = vi.fn(
    () =>
      new Promise<boolean>((resolve) => {
        finish = resolve;
      }),
  );
  const { box, onStop, container, rerender } = mount({ onSend });
  fireEvent.change(box, { target: { value: "hello" } });
  fireEvent.keyDown(box, { key: "Enter" });
  rerender(<Composer running={true} submitDisabled={false} onSend={onSend} onStop={onStop} />);
  fireEvent.change(box, { target: { value: "still typing" } });
  fireEvent.keyDown(box, { key: "Enter" });
  expect(blockedText(container)).toBe("A reply is still running. Send when it ends.");
  expect(onSend).toHaveBeenCalledTimes(1);
  await act(async () => {
    finish(true);
  });
});

test("the running notice clears once the reply ends", () => {
  const { box, onSend, onStop, container, rerender } = mount({ running: true });
  fireEvent.change(box, { target: { value: "hello" } });
  fireEvent.keyDown(box, { key: "Enter" });
  expect(blockedText(container)).not.toBeNull();
  rerender(<Composer running={false} onSend={onSend} onStop={onStop} />);
  expect(blockedText(container)).toBeNull();
});

test("a running reply with steer shows the Steer button before Stop", () => {
  const { container } = mount({ running: true, steer: true });
  const actions = container.querySelector(".composer-actions") as HTMLElement;
  const children = Array.from(actions.children);
  const steerIndex = children.findIndex((el) => el.classList.contains("composer-steer"));
  const stopIndex = children.findIndex((el) => el.classList.contains("composer-stop"));
  expect(steerIndex).toBeGreaterThanOrEqual(0);
  expect(stopIndex).toBeGreaterThan(steerIndex);
});

test("a running reply without steer shows no Steer button", () => {
  const { container } = mount({ running: true });
  expect(container.querySelector(".composer-steer")).toBeNull();
});

test("Enter calls onSend through Steer while a reply runs", async () => {
  const { box, onSend } = mount({ running: true, steer: true });
  fireEvent.change(box, { target: { value: "keep going" } });
  await act(async () => {
    fireEvent.keyDown(box, { key: "Enter" });
  });
  expect(onSend).toHaveBeenCalledWith("keep going");
});

test("a known command is never steered: Steer is disabled and Enter still waits for the reply", () => {
  const { box, onSend, container } = mount({
    running: true,
    steer: true,
    commands: [command("retro")],
  });
  // A trailing argument, as the chip tests above do, so the slash menu (open for a bare "/retro")
  // does not itself capture Enter before it ever reaches attemptSend.
  fireEvent.change(box, { target: { value: "/retro now" } });
  expect((container.querySelector(".composer-steer") as HTMLButtonElement).disabled).toBe(true);
  fireEvent.keyDown(box, { key: "Enter" });
  expect(onSend).not.toHaveBeenCalled();
  expect(blockedText(container)).toBe("A reply is still running. Send when it ends.");
});

test("Steer is disabled while the box is empty", () => {
  const { container } = mount({ running: true, steer: true });
  expect((container.querySelector(".composer-steer") as HTMLButtonElement).disabled).toBe(true);
});

test("the Stop button explains that background agents keep going", () => {
  mount({ running: true });
  expect(screen.getByRole("button", { name: "Stop" }).getAttribute("title")).toBe(
    "Stops the reply. Agents running in the background keep going.",
  );
});

// A clip that holds text beside a file is the user's text paste: hijacking it as a file intake
// would swallow the text the user meant to insert. Only a file-only clip is handed off.
test("a paste with text and a file is left to the browser, and no file is handed off", () => {
  const onFiles = vi.fn();
  const { box } = mount({ onFiles });
  const file = new File(["hello"], "note.txt", { type: "text/plain" });
  const notPrevented = fireEvent.paste(box, {
    clipboardData: { files: [file], getData: () => "hello" },
  });
  expect(onFiles).not.toHaveBeenCalled();
  expect(notPrevented).toBe(true);
});

test("a paste with only a file is handed to onFiles as the file list and the source paste", () => {
  const onFiles = vi.fn();
  const { box } = mount({ onFiles });
  const file = new File(["png-bytes"], "image.png", { type: "image/png" });
  const prevented = fireEvent.paste(box, {
    clipboardData: { files: [file], getData: () => "" },
  });
  expect(prevented).toBe(false);
  expect(onFiles).toHaveBeenCalledTimes(1);
  expect(onFiles).toHaveBeenCalledWith([file], "paste");
});

const WORKSPACE = "11111111-1111-4111-8111-111111111111";
const SHA = "a".repeat(64);
const chipPart: VaultExcerptPart = {
  kind: "vault_excerpt",
  vault_id: WORKSPACE,
  ref: "notes/a.md",
  start_line: 1,
  end_line: 3,
  sha256: SHA,
};

/** An api that answers every check at once with the digest the test names. */
function digestApi(sha: string): ExcerptApi {
  return {
    excerpt: async (_id, _ref, start, end) => ({
      part: { ...chipPart, start_line: start, end_line: end, sha256: sha },
      text: "body",
      block: "block",
      block_bytes: 10,
      revision: "r",
      modified_at: "2026-10-02T09:00:00Z",
    }),
  };
}

/** The one command this session offers, as the service's `started.commands` names them. */
const OFFERED: Command[] = [command("review")];

function ChipsHarness({
  api,
  onSend,
  parts = [chipPart],
  attach,
}: {
  api: ExcerptApi;
  onSend: (text: string) => Promise<boolean>;
  parts?: VaultExcerptPart[];
  attach?: VaultExcerptPart;
}) {
  const [excerpts, setExcerpts] = useState<readonly VaultExcerptPart[]>(parts);
  return (
    <>
      <Composer
        running={false}
        onSend={onSend}
        onStop={vi.fn()}
        commands={OFFERED}
        excerpts={excerpts}
        onExcerptsChange={setExcerpts}
        excerptApi={api}
        excerptWorkspaceId={WORKSPACE}
      />
      {attach ? (
        <button
          type="button"
          onClick={() => setExcerpts((current) => withExcerpt(current, attach))}
        >
          Attach another excerpt
        </button>
      ) : null}
    </>
  );
}

async function mountChips(api: ExcerptApi, onSend = vi.fn(async (_text: string) => true)) {
  const view = render(<ChipsHarness api={api} onSend={onSend} />);
  await act(async () => {});
  const box = view.container.querySelector("textarea") as HTMLTextAreaElement;
  const send = screen.getByRole("button", { name: "Send" }) as HTMLButtonElement;
  return { ...view, box, send, onSend };
}

test("without the excerpt props the composer shows no chips and Send works as before", () => {
  const { container } = mount();
  expect(container.querySelector(".excerpt-chips")).toBeNull();
});

test("fresh chips let a message go, and onSend still receives the one plain string", async () => {
  const { box, send, onSend } = await mountChips(digestApi(SHA));
  expect(send.disabled).toBe(false);
  fireEvent.change(box, { target: { value: "hi" } });
  await act(async () => {
    fireEvent.keyDown(box, { key: "Enter" });
  });
  expect(onSend).toHaveBeenCalledTimes(1);
  expect(onSend).toHaveBeenCalledWith("hi");
});

test("a stale chip disables Send and Enter sends nothing", async () => {
  const { box, send, onSend } = await mountChips(digestApi("b".repeat(64)));
  expect(
    screen.getByText("The note has changed. Refresh or remove this excerpt before sending."),
  ).toBeTruthy();
  expect(send.disabled).toBe(true);
  fireEvent.change(box, { target: { value: "hi" } });
  await act(async () => {
    fireEvent.keyDown(box, { key: "Enter" });
  });
  expect(onSend).not.toHaveBeenCalled();
});

test("a chip still being checked holds Send back", () => {
  const never: ExcerptApi = { excerpt: () => new Promise(() => {}) };
  render(<ChipsHarness api={never} onSend={vi.fn(async () => true)} />);
  expect((screen.getByRole("button", { name: "Send" }) as HTMLButtonElement).disabled).toBe(true);
});

test("removing the chip lifts the hold", async () => {
  const { send } = await mountChips(digestApi("b".repeat(64)));
  expect(send.disabled).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Remove" }));
  expect(screen.queryByText(/App-added content/)).toBeNull();
  expect(send.disabled).toBe(false);
});

const siblingPart = { ...chipPart, ref: "notes/b.md" };
const addedPart = { ...chipPart, ref: "notes/c.md" };

/** Initial checks answer immediately; the explicit refresh waits for the test. */
async function beginExcerptRefresh(parts = [chipPart, siblingPart]) {
  let checked = false;
  let finish: (value: VaultExcerpt | Error) => void = () => {
    throw new Error("No refresh is pending");
  };
  const fresh = { ...chipPart, sha256: "c".repeat(64) };
  const answer = (part: VaultExcerptPart): VaultExcerpt => ({
    part,
    text: "body",
    block: "block",
    block_bytes: 5,
    revision: "r",
    modified_at: "2026-10-02T09:00:00Z",
  });
  const api: ExcerptApi = {
    excerpt: async (_id, ref, start, end) => {
      if (ref === chipPart.ref) {
        if (checked) {
          return new Promise<VaultExcerpt>((resolve, reject) => {
            finish = (value) => (value instanceof Error ? reject(value) : resolve(value));
          });
        }
        checked = true;
      }
      return answer(
        ref === chipPart.ref ? fresh : { ...chipPart, ref, start_line: start, end_line: end },
      );
    },
  };
  const onSend = vi.fn(async () => true);
  render(<ChipsHarness api={api} onSend={onSend} parts={parts} attach={addedPart} />);
  await act(async () => {});
  const refresh = screen.getByRole("button", { name: "Refresh" }) as HTMLButtonElement;
  expect(refresh.disabled).toBe(false);
  fireEvent.click(refresh);
  expect(refresh.disabled).toBe(true);
  return {
    fresh,
    onSend,
    finish: async (value: VaultExcerpt | Error = answer(fresh)) => {
      await act(async () => finish(value));
      expect(onSend).not.toHaveBeenCalled();
    },
  };
}

function excerptLabels(): (string | null)[] {
  return [...document.querySelectorAll(".excerpt-chip-label")].map((label) => label.textContent);
}

test.each([
  { name: "a following sibling", parts: [chipPart, siblingPart], removed: 1, kept: chipPart },
  { name: "a preceding sibling", parts: [siblingPart, chipPart], removed: 0, kept: chipPart },
  { name: "the refreshed chip", parts: [chipPart, siblingPart], removed: 0, kept: siblingPart },
])("refresh preserves removal of $name", async ({ parts, removed, kept }) => {
  const pending = await beginExcerptRefresh(parts);
  const remove = screen.getAllByRole("button", { name: "Remove" })[removed] as HTMLButtonElement;
  expect(remove.disabled).toBe(false);
  fireEvent.click(remove);
  const expected = [`${kept.ref} (lines 1–3)`];
  expect(excerptLabels()).toEqual(expected);
  await pending.finish();
  expect(excerptLabels()).toEqual(expected);
  if (kept === chipPart) expect(screen.getByText(pending.fresh.sha256)).toBeTruthy();
});

test("a refresh finishing after final-chip removal leaves the chips unmounted", async () => {
  const pending = await beginExcerptRefresh([chipPart]);
  fireEvent.click(screen.getByRole("button", { name: "Remove" }));
  expect(screen.queryByRole("region", { name: "Excerpts to send" })).toBeNull();
  await pending.finish();
  expect(screen.queryByRole("region", { name: "Excerpts to send" })).toBeNull();
});

test("refresh preserves a newly attached chip", async () => {
  const pending = await beginExcerptRefresh();
  fireEvent.click(screen.getByRole("button", { name: "Attach another excerpt" }));
  const expected = [chipPart, siblingPart, addedPart].map((part) => `${part.ref} (lines 1–3)`);
  expect(excerptLabels()).toEqual(expected);
  await pending.finish();
  expect(excerptLabels()).toEqual(expected);
  expect(screen.getByText(pending.fresh.sha256)).toBeTruthy();
});

test("refresh without collection edits updates only the requested chip", async () => {
  const pending = await beginExcerptRefresh();
  await pending.finish();
  expect(excerptLabels()).toEqual(["notes/a.md (lines 1–3)", "notes/b.md (lines 1–3)"]);
  expect(screen.getByText(pending.fresh.sha256)).toBeTruthy();
  expect(screen.getAllByText(/To be sent to Claude/)).toHaveLength(2);
});

test("a rejected refresh preserves removal of another chip", async () => {
  const pending = await beginExcerptRefresh();
  fireEvent.click(screen.getAllByRole("button", { name: "Remove" })[1] as HTMLButtonElement);
  await pending.finish(new Error("unavailable"));
  expect(excerptLabels()).toEqual(["notes/a.md (lines 1–3)"]);
  expect(screen.getByText("Could not check this excerpt.")).toBeTruthy();
});

const WITH_COMMAND = "Commands cannot be sent with excerpts.";

test("a slash command with a chip present cannot be sent, and the box says why", async () => {
  const { box, send, onSend } = await mountChips(digestApi(SHA));
  fireEvent.change(box, { target: { value: "/review" } });
  expect(send.disabled).toBe(true);
  expect(screen.getByText(WITH_COMMAND)).toBeTruthy();
  await act(async () => {
    fireEvent.keyDown(box, { key: "Enter" });
  });
  expect(onSend).not.toHaveBeenCalled();
  fireEvent.change(box, { target: { value: "・milk" } });
  expect(screen.queryByText(WITH_COMMAND)).toBeNull();
  expect(send.disabled).toBe(false);
});

test("an offered command with its argument blocks the send; another word does not", async () => {
  const { box, send } = await mountChips(digestApi(SHA));
  fireEvent.change(box, { target: { value: "/review this excerpt" } });
  expect(send.disabled).toBe(true);
  expect(screen.getByText(WITH_COMMAND)).toBeTruthy();
  // "reviewer" is not the command "review", and no command is offered under that name.
  fireEvent.change(box, { target: { value: "/reviewer show this" } });
  expect(screen.queryByText(WITH_COMMAND)).toBeNull();
  expect(send.disabled).toBe(false);
});

test("a message that begins with a path is not a command and goes with its chip", async () => {
  // A leading "/" alone must not block the send: the service reads a command only when the leading
  // token names one it offers (`session._command`): a leading path names none.
  const { box, send, onSend } = await mountChips(digestApi(SHA));
  fireEvent.change(box, { target: { value: "/30-knowledge/note.md compared with this" } });
  expect(screen.queryByText(WITH_COMMAND)).toBeNull();
  expect(send.disabled).toBe(false);
  await act(async () => {
    fireEvent.keyDown(box, { key: "Enter" });
  });
  expect(onSend).toHaveBeenCalledWith("/30-knowledge/note.md compared with this");
});

test("a slash command alone, with no chip, is sent as ever", () => {
  const { box, onSend } = mount();
  fireEvent.change(box, { target: { value: "/review" } });
  fireEvent.keyDown(box, { key: "Enter" });
  expect(onSend).toHaveBeenCalledWith("/review");
});

test("chips with an empty box say text is needed; typing hides the line and frees Send", async () => {
  const hint = "Enter a question or instruction to send an excerpt.";
  const { box, onSend } = await mountChips(digestApi(SHA));
  expect(screen.getByText(hint)).toBeTruthy();
  await act(async () => {
    fireEvent.keyDown(box, { key: "Enter" });
  });
  expect(onSend).not.toHaveBeenCalled();
  fireEvent.change(box, { target: { value: "hi" } });
  expect(screen.queryByText(hint)).toBeNull();
  await act(async () => {
    fireEvent.keyDown(box, { key: "Enter" });
  });
  expect(onSend).toHaveBeenCalledWith("hi");
});

test("an empty box with no chips shows no excerpt hint", () => {
  mount();
  expect(screen.queryByText("Enter a question or instruction to send an excerpt.")).toBeNull();
});

let restoreSizing: (() => void) | undefined;
afterEach(() => {
  restoreSizing?.();
  restoreSizing = undefined;
});

function sizingSignals() {
  let width = 300;
  const widthRead = vi
    .spyOn(HTMLElement.prototype, "clientWidth", "get")
    .mockImplementation(() => width);
  let resize = () => {};
  const disconnect = vi.fn();
  vi.stubGlobal(
    "ResizeObserver",
    class {
      constructor(callback: ResizeObserverCallback) {
        resize = () => callback([], this as unknown as ResizeObserver);
      }
      observe() {}
      disconnect = disconnect;
    },
  );
  const fonts = new EventTarget();
  let ready = () => {};
  Object.defineProperty(fonts, "ready", {
    value: new Promise<void>((resolve) => {
      ready = resolve;
    }),
  });
  const previousFonts = Object.getOwnPropertyDescriptor(document, "fonts");
  Object.defineProperty(document, "fonts", { configurable: true, value: fonts });
  restoreSizing = () => {
    vi.unstubAllGlobals();
    widthRead.mockRestore();
    if (previousFonts) Object.defineProperty(document, "fonts", previousFonts);
    else Reflect.deleteProperty(document, "fonts");
  };
  return {
    fonts,
    ready: () => ready(),
    resize: () => act(() => resize()),
    disconnect,
    width: (next: number) => {
      width = next;
    },
  };
}

test("viewport resizing recomputes the cap without moving focus or the selection", () => {
  sizingSignals();
  const { box, container, onSend } = mount();
  setScrollHeight(box, 1000);
  fireEvent.change(box, { target: { value: "unchanged draft" } });
  box.setSelectionRange(2, 5);
  const send = container.querySelector<HTMLButtonElement>(".composer-submit");
  send?.focus();
  setDocumentHeight(600);
  fireEvent(window, new Event("resize"));
  expect(box.style.height).toBe("240px");
  expect(document.activeElement).toBe(send);
  expect([box.selectionStart, box.selectionEnd]).toEqual([2, 5]);
  expect(box.value).toBe("unchanged draft");
  expect(onSend).not.toHaveBeenCalled();
});

test("width changes remeasure wrapping while same-width height notifications do not feed back", () => {
  const signals = sizingSignals();
  const { box } = mount();
  setScrollHeight(box, 200);
  fireEvent.change(box, { target: { value: "same draft" } });
  signals.resize();
  setScrollHeight(box, 100);
  signals.resize();
  expect(box.style.height).toBe("200px");
  signals.width(450);
  signals.resize();
  expect(box.style.height).toBe("100px");
  setScrollHeight(box, 320);
  signals.width(200);
  signals.resize();
  expect(box.style.height).toBe("320px");
});

test("an appearance scale change recomputes the local cap without editing or moving the caret", async () => {
  sizingSignals();
  const { box } = mount();
  setScrollHeight(box, 1000);
  fireEvent.change(box, { target: { value: "unchanged draft" } });
  box.focus();
  box.setSelectionRange(1, 4);
  await act(async () => document.documentElement.style.setProperty("--font-scale", "1.5"));
  expect(box.style.height).toBe("240px");
  expect(document.activeElement).toBe(box);
  expect([box.selectionStart, box.selectionEnd]).toEqual([1, 4]);
  expect(box.value).toBe("unchanged draft");
});

test.each(["ready", "loadingdone"])(
  "font %s recomputes unchanged text without focusing it",
  async (signal) => {
    const signals = sizingSignals();
    const { box, container } = mount();
    setScrollHeight(box, 100);
    fireEvent.change(box, { target: { value: "unchanged draft" } });
    box.setSelectionRange(1, 3);
    const send = container.querySelector<HTMLButtonElement>(".composer-submit");
    send?.focus();
    setScrollHeight(box, 180);
    await act(async () => {
      if (signal === "ready") signals.ready();
      else signals.fonts.dispatchEvent(new Event("loadingdone"));
    });
    expect(box.style.height).toBe("180px");
    expect(document.activeElement).toBe(send);
    expect([box.selectionStart, box.selectionEnd]).toEqual([1, 3]);
  },
);

test("unmount disconnects sizing and ignores queued font or observer notifications", async () => {
  const signals = sizingSignals();
  const { box } = mount();
  setScrollHeight(box, 100);
  fireEvent.change(box, { target: { value: "keep" } });
  const height = box.style.height;
  cleanup();
  expect(signals.disconnect).toHaveBeenCalledTimes(1);
  setScrollHeight(box, 500);
  signals.width(100);
  await act(async () => {
    signals.resize();
    fireEvent(window, new Event("resize"));
    signals.ready();
    signals.fonts.dispatchEvent(new Event("loadingdone"));
  });
  expect(box.style.height).toBe(height);
});

test("fractional observed widths reflow text despite constant clientWidth without caret changes", () => {
  sizingSignals();
  let width = 300.25;
  let target: Element = document.body;
  let notify = () => {};
  vi.stubGlobal(
    "ResizeObserver",
    class {
      constructor(callback: ResizeObserverCallback) {
        notify = () =>
          callback(
            [{ target, contentRect: { width } } as ResizeObserverEntry],
            this as unknown as ResizeObserver,
          );
      }
      observe(element: Element) {
        target = element;
      }
      disconnect() {}
    },
  );
  const { box, container, onSend } = mount();
  setScrollHeight(box, 200);
  fireEvent.change(box, { target: { value: "same draft" } });
  box.setSelectionRange(1, 4);
  const send = container.querySelector<HTMLButtonElement>(".composer-submit");
  send?.focus();
  act(() => notify());
  expect(box.clientWidth).toBe(300);
  setScrollHeight(box, 100);
  width = 300.5;
  act(() => notify());
  expect(box.style.height).toBe("100px");
  setScrollHeight(box, 240);
  act(() => notify());
  expect(box.style.height).toBe("100px");
  width = 300.1;
  act(() => notify());
  expect(box.style.height).toBe("240px");
  expect(document.activeElement).toBe(send);
  expect([box.selectionStart, box.selectionEnd]).toEqual([1, 4]);
  expect(box.value).toBe("same draft");
  expect(onSend).not.toHaveBeenCalled();
});

function composerHost(scale = 1) {
  const signals = sizingSignals();
  const metrics = { height: 500, banner: 0, inputTop: 550, inputLeft: 50, pillExtra: 2 };
  document.documentElement.style.setProperty("--font-scale", String(scale));
  const heights = vi
    .spyOn(HTMLElement.prototype, "clientHeight", "get")
    .mockImplementation(function (this: HTMLElement) {
      return this.className === "conversation" ? metrics.height : 0;
    });
  const offsets = vi
    .spyOn(HTMLElement.prototype, "offsetHeight", "get")
    .mockImplementation(function (this: HTMLElement) {
      if (this.tagName === "TEXTAREA") return Number.parseFloat(this.style.height) || 54;
      if (this.className === "composer")
        return (this.querySelector("textarea")?.offsetHeight ?? 54) + metrics.pillExtra;
      return this.className === "conversation-header"
        ? 53
        : this.className === "banner"
          ? metrics.banner
          : 0;
    });
  const rectangles = vi
    .spyOn(HTMLElement.prototype, "getBoundingClientRect")
    .mockImplementation(function (this: HTMLElement) {
      const rect =
        this.className === "conversation"
          ? [0, 100, 800, metrics.height]
          : this.className === "composer-slot"
            ? [0, 400, 800, 200]
            : this.className === "composer"
              ? [metrics.inputLeft, metrics.inputTop, 200, 56]
              : [50, -1000, 200, 1500];
      return new DOMRect(...rect.map((value) => value * scale));
    });
  const priorCleanup = restoreSizing;
  restoreSizing = () => {
    priorCleanup?.();
    heights.mockRestore();
    offsets.mockRestore();
    rectangles.mockRestore();
  };
  const onSend = vi.fn(async () => false);
  const view = render(
    <main className="conversation">
      <div className="conversation-header" />
      <div className="banner" />
      <div className="conversation-scroll" />
      <div className="composer-slot" style={{ padding: "10px 16px 14px" }}>
        <Composer running={false} onSend={onSend} onStop={vi.fn()} commands={[command("review")]} />
      </div>
    </main>,
  );
  return {
    ...view,
    signals,
    metrics,
    onSend,
    box: screen.getByRole("textbox") as HTMLTextAreaElement,
    slot: view.container.querySelector(".composer-slot") as HTMLElement,
  };
}

test.each([
  { scale: 1, height: "274px" },
  { scale: 1.5, height: "240px" },
])("the hosted local budget retains the legacy scale cap at scale $scale", ({ scale, height }) => {
  const { box, slot, onSend } = composerHost(scale);
  setScrollHeight(box, 1000);
  fireEvent.change(box, { target: { value: "tall draft" } });
  expect(slot.style.getPropertyValue("--composer-max-height")).toBe("300px");
  expect(slot.style.getPropertyValue("--composer-min-height")).toBe("80px");
  expect(box.style.height).toBe(height);
  expect(box.value).toBe("tall draft");
  expect(onSend).not.toHaveBeenCalled();
});

test("in-flow chrome bounds the local maximum without collapsing the natural composer core", () => {
  const { slot, metrics } = composerHost();
  metrics.banner = 330;
  fireEvent(window, new Event("resize"));
  expect(slot.style.getPropertyValue("--composer-max-height")).toBe("117px");
  expect(slot.style.getPropertyValue("--composer-min-height")).toBe("80px");
  metrics.banner = 0;
  metrics.height = 100;
  fireEvent(window, new Event("resize"));
  expect(slot.style.getPropertyValue("--composer-max-height")).toBe("80px");
  expect(slot.style.getPropertyValue("--composer-min-height")).toBe("80px");
});

test("the actual transcript minimum reserves room before the composer grows", () => {
  const { slot, box, container, metrics } = composerHost();
  const transcript = container.querySelector(".conversation-scroll") as HTMLElement;
  transcript.style.minHeight = "53px";
  metrics.height = 200;
  setScrollHeight(box, 1000);
  fireEvent.change(box, { target: { value: "long retained draft" } });
  expect(slot.style.getPropertyValue("--composer-max-height")).toBe("94px");
  expect(slot.style.getPropertyValue("--composer-min-height")).toBe("80px");
  expect(box.style.height).toBe("68px");
  metrics.height = 100;
  fireEvent(window, new Event("resize"));
  expect(slot.style.getPropertyValue("--composer-max-height")).toBe("80px");
  expect(slot.style.getPropertyValue("--composer-min-height")).toBe("80px");
  expect(box.style.height).toBe("54px");
  expect(box.value).toBe("long retained draft");
});

test("a wrapped 103px pill retains its natural floor without counting long text as fixed chrome", () => {
  const { slot, box, metrics, onSend } = composerHost();
  metrics.pillExtra = 49;
  setScrollHeight(box, 1000);
  fireEvent.change(box, { target: { value: "long wrapped draft" } });
  expect(slot.style.getPropertyValue("--composer-min-height")).toBe("127px");
  expect(slot.style.getPropertyValue("--composer-max-height")).toBe("300px");
  expect(box.style.height).toBe("227px");
  fireEvent(window, new Event("resize"));
  expect(slot.style.getPropertyValue("--composer-min-height")).toBe("127px");
  expect(box.style.height).toBe("227px");
  setDocumentHeight(300);
  fireEvent(window, new Event("resize"));
  expect(box.style.height).toBe("120px");
  metrics.height = 100;
  fireEvent(window, new Event("resize"));
  expect(slot.style.getPropertyValue("--composer-max-height")).toBe("127px");
  expect(slot.style.getPropertyValue("--composer-min-height")).toBe("127px");
  expect(box.style.height).toBe("54px");
  expect(box.value).toBe("long wrapped draft");
  expect(onSend).not.toHaveBeenCalled();
});

test("a hosted menu follows the visible input despite tall attachment chrome and cleans up", async () => {
  const { box, slot, container, metrics, signals, onSend, unmount } = composerHost(1.5);
  const conversation = container.querySelector(".conversation") as HTMLElement;
  fireEvent.change(box, { target: { value: "/" } });
  const menu = container.querySelector(".menu") as HTMLElement;
  expect(menu.closest(".composer-wrap")).toBe(box.closest(".composer-wrap"));
  expect([menu.style.left, menu.style.width, menu.style.bottom]).toEqual(["50px", "200px", "58px"]);
  expect(menu.style.maxHeight).toBe("360px");
  box.setSelectionRange(0, 1);
  screen.getByRole("button", { name: "Send" }).focus();
  metrics.inputTop = 1200;
  fireEvent.scroll(slot);
  expect(menu.style.bottom).toBe("8px");
  metrics.inputTop = -100;
  fireEvent.scroll(slot);
  expect(menu.style.bottom).toBe("208px");
  expect(menu.style.maxHeight).toBe("292px");
  metrics.inputTop = 500;
  fireEvent.scroll(conversation);
  expect(menu.style.bottom).toBe("108px");
  expect(container.querySelector(".menu")).toBe(menu);
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "Send" }));
  expect([box.selectionStart, box.selectionEnd]).toEqual([0, 1]);
  expect(box.value).toBe("/");
  expect(onSend).not.toHaveBeenCalled();
  box.focus();
  fireEvent.keyDown(box, { key: "Escape" });
  expect(container.querySelector(".menu")).toBeNull();
  expect(signals.disconnect).toHaveBeenCalledTimes(1);
  metrics.inputLeft = 100;
  fireEvent.scroll(slot);
  fireEvent.scroll(conversation);
  signals.resize();
  expect(menu.style.left).toBe("50px");
  fireEvent.change(box, { target: { value: "/r" } });
  const reopened = container.querySelector(".menu") as HTMLElement;
  expect(reopened.style.left).toBe("100px");
  unmount();
  metrics.inputLeft = 150;
  await act(async () => {
    signals.resize();
    signals.ready();
  });
  fireEvent.scroll(slot);
  fireEvent.scroll(conversation);
  expect(reopened.style.left).toBe("100px");
});

function sharedBudgetHost() {
  const signals = sizingSignals();
  const observed = new Set<Element>();
  let target: Element = document.body;
  const BaseObserver = ResizeObserver;
  vi.stubGlobal(
    "ResizeObserver",
    class extends BaseObserver {
      constructor(callback: ResizeObserverCallback) {
        super((_, observer) => callback([{ target } as ResizeObserverEntry], observer));
      }
      override observe(node: Element) {
        observed.add(node);
      }
      override disconnect = () => {
        observed.clear();
        signals.disconnect();
      };
    },
  );
  const heights = vi
    .spyOn(HTMLElement.prototype, "clientHeight", "get")
    .mockImplementation(function (this: HTMLElement) {
      return this.className === "conversation" ? 800 : 0;
    });
  const offsets = vi
    .spyOn(HTMLElement.prototype, "offsetHeight", "get")
    .mockImplementation(function (this: HTMLElement) {
      if (this.className === "composer")
        return (Number.parseFloat(this.querySelector("textarea")?.style.height ?? "") || 54) + 2;
      if (this.className === "composer-wrap")
        return (
          (this.querySelector<HTMLElement>(".composer")?.offsetHeight ?? 56) +
          Number(this.querySelector<HTMLElement>("section")?.dataset.height ?? 0)
        );
      return Number(this.dataset.height ?? 0);
    });
  const priorCleanup = restoreSizing;
  restoreSizing = () => {
    priorCleanup?.();
    heights.mockRestore();
    offsets.mockRestore();
  };
  const onSend = vi.fn(async () => false);
  const view = render(
    <main className="conversation">
      <div className="conversation-header" data-height="53" />
      <div className="conversation-scroll">Transcript</div>
      <div className="composer-slot" style={{ padding: "10px 16px 14px" }}>
        <Composer
          running={false}
          onSend={onSend}
          onStop={vi.fn()}
          attachmentStrip={<section data-height="20">Attachment notice</section>}
        />
      </div>
    </main>,
  );
  const slot = view.container.querySelector(".composer-slot") as HTMLElement;
  slot.insertAdjacentHTML(
    "beforeend",
    `
    <details class="local-drafts" open data-height="2500" style="padding:10px 12px;border:1px solid;margin:12px 0">
      <summary data-height="18">Queue</summary><ol data-height="2400"><li><button>Queued action</button></li></ol>
    </details>
    <div class="draft-status" data-height="24">Saved draft</div>
    <details class="draft-recovery" open data-height="3500" style="margin-bottom:8px">
      <summary data-height="18">Recovery</summary><div class="draft-recovery-body" data-height="3400" style="margin-top:6px"><button>Restore</button></div>
    </details>`,
  );
  const box = screen.getByRole("textbox") as HTMLTextAreaElement;
  setScrollHeight(box, 1000);
  fireEvent.change(box, { target: { value: "retained shared draft" } });
  const resize = (node: Element, queued = false) => {
    target = node;
    if (queued || observed.has(node)) signals.resize();
  };
  return { ...view, slot, box, onSend, signals, observed, resize };
}

test("open drafts and recovery share growth without making their long bodies fixed chrome", () => {
  const { slot, box } = sharedBudgetHost();
  expect(slot.style.getPropertyValue("--composer-min-height")).toBe("214px");
  expect(slot.style.getPropertyValue("--composer-max-height")).toBe("480px");
  expect(box.style.height).toBe("142px");
  expect(slot.style.getPropertyValue("--composer-panel-height")).toBe("89px");
  setScrollHeight(box, 54);
  fireEvent(window, new Event("resize"));
  expect(box.style.height).toBe("54px");
  expect(slot.style.getPropertyValue("--composer-panel-height")).toBe("133px");
});

test("an explicit slot height uses current bounds and exhausted bodies retain a scrollable floor", async () => {
  const { slot, box } = sharedBudgetHost();
  slot.style.height = "300px";
  fireEvent(window, new Event("resize"));
  expect(box.style.height).toBe("82px");
  expect(slot.style.getPropertyValue("--composer-panel-height")).toBe("54px");
  slot.style.height = "100px";
  fireEvent(window, new Event("resize"));
  expect(box.style.height).toBe("54px");
  expect(slot.style.height).toBe("100px");
  await act(async () =>
    slot.insertAdjacentHTML("beforeend", '<div data-height="1000">Long notice</div>'),
  );
  fireEvent(window, new Event("resize"));
  expect(slot.style.getPropertyValue("--composer-min-height")).toBe("480px");
  expect(slot.style.getPropertyValue("--composer-panel-height")).toBe("54px");
  expect((screen.getByText("Queued action") as HTMLButtonElement).disabled).toBe(false);
  expect((screen.getByText("Restore") as HTMLButtonElement).disabled).toBe(false);
});

test("current nested notices, body toggles and added banners recompute the shared allocation", async () => {
  const { slot, box, container, observed, resize, onSend } = sharedBudgetHost();
  box.setSelectionRange(2, 5);
  screen.getByRole("button", { name: "Send" }).focus();
  const notice = slot.querySelector("section") as HTMLElement;
  await act(async () => {
    notice.dataset.height = "80";
    (notice.firstChild as Text).data = "Longer attachment notice";
  });
  expect(slot.style.getPropertyValue("--composer-min-height")).toBe("274px");
  const localDrafts = slot.querySelector(".local-drafts") as HTMLDetailsElement;
  await act(async () => {
    localDrafts.open = false;
  });
  expect(box.style.height).toBe("157px");
  const banner = document.createElement("div");
  banner.dataset.height = "330";
  await act(async () => {
    slot.before(banner);
  });
  expect(observed.has(banner)).toBe(true);
  expect(slot.style.getPropertyValue("--composer-max-height")).toBe("417px");
  banner.dataset.height = "680";
  resize(banner);
  expect(slot.style.getPropertyValue("--composer-max-height")).toBe("80px");
  await act(async () => {
    banner.remove();
  });
  expect(observed.has(banner)).toBe(false);
  expect(slot.style.getPropertyValue("--composer-max-height")).toBe("480px");
  expect(container.querySelector(".composer-wrap section")).toBe(notice);
  expect(box.value).toBe("retained shared draft");
  expect([box.selectionStart, box.selectionEnd]).toEqual([2, 5]);
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "Send" }));
  expect(onSend).not.toHaveBeenCalled();
});

test("transcript/style mutations stay inert and queued size callbacks stop after unmount", async () => {
  const { slot, box, container, signals, resize, unmount } = sharedBudgetHost();
  const reads = vi.fn(() => 1000);
  Object.defineProperty(box, "scrollHeight", { configurable: true, get: reads });
  await act(async () => {});
  reads.mockClear();
  await act(async () => {
    const transcript = container.querySelector(".conversation-scroll") as HTMLElement;
    transcript.textContent = "Another transcript frame";
    slot.style.setProperty("--fixture-style", "1");
  });
  expect(reads).not.toHaveBeenCalled();
  unmount();
  await act(async () => {
    resize(slot, true);
    signals.ready();
  });
  expect(slot.style.getPropertyValue("--composer-panel-height")).toBe("");
  expect(signals.disconnect).toHaveBeenCalled();
  expect(box.value).toBe("retained shared draft");
});

test("placeholders retain draft text and selection across identity resolution", () => {
  const onSend = vi.fn(async () => true);
  const onStop = vi.fn();
  const unresolved = { ...DEFAULT_IDENTITY, assistantName: "Sidekick", unresolved: true };
  const { rerender } = render(
    <IdentityContext value={unresolved}>
      <Composer running={false} onSend={onSend} onStop={onStop} />
    </IdentityContext>,
  );
  const box = screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
  expect(box.placeholder).toBe("Talk… / for skills");
  fireEvent.change(box, { target: { value: "A draft in progress" } });
  box.focus();
  box.setSelectionRange(2, 5);
  rerender(
    <IdentityContext value={{ ...unresolved, unresolved: false }}>
      <Composer running={false} onSend={onSend} onStop={onStop} />
    </IdentityContext>,
  );
  expect(box.placeholder).toBe("Talk to Sidekick… / for skills");
  expect(box.value).toBe("A draft in progress");
  expect(box.selectionStart).toBe(2);
  expect(box.selectionEnd).toBe(5);
  expect(document.activeElement).toBe(box);
  expect(onSend).not.toHaveBeenCalled();
});

// App wires a McpServersButton through this prop; pinned here at the
// Composer level since src/accept never renders one.
test("mcpControl renders first in composer-actions, and only when given", () => {
  const { container, onSend, onStop, rerender } = mount({
    mcpControl: <button type="button" data-testid="mcp-trigger" />,
  });
  const actions = container.querySelector(".composer-actions");
  expect(actions?.firstElementChild).toBe(screen.getByTestId("mcp-trigger"));

  rerender(<Composer running={false} onSend={onSend} onStop={onStop} />);
  expect(screen.queryByTestId("mcp-trigger")).toBeNull();
});

test("send status lasts until settlement without changing input or resending", async () => {
  let finish: (result: boolean) => void = () => {};
  const onSend = vi.fn(
    () =>
      new Promise<boolean>((resolve) => {
        finish = resolve;
      }),
  );
  const { box } = mount({ onSend });
  fireEvent.change(box, { target: { value: "A question\n" } });
  fireEvent.keyDown(box, { key: "Enter" });
  expect(screen.getByText("Waiting for the send response…").getAttribute("role")).toBe("status");
  expect(screen.getByRole("status", { name: "Chat activity" })).toBeTruthy();
  expect(screen.getByRole("button", { name: "Send" })).toHaveProperty("disabled", true);
  expect(onSend).toHaveBeenCalledTimes(1);
  expect(onSend).toHaveBeenCalledWith("A question");
  fireEvent.change(box, { target: { value: "Still typing" } });
  fireEvent.keyDown(box, { key: "Enter" });
  expect(onSend).toHaveBeenCalledTimes(1);
  expect(box.value).toBe("Still typing");
  await act(async () => {
    finish(false);
  });
  expect(screen.queryByText("Waiting for the send response…")).toBeNull();
  expect(screen.getByRole("status", { name: "Chat activity" })).toBeTruthy();
  expect(box.value).toBe("Still typing");
  expect(screen.getByRole("button", { name: "Send" })).toHaveProperty("disabled", false);
  expect(onSend).toHaveBeenCalledTimes(1);
});

test.each(["focus", "dictation admission"])(
  "a pending status retry cannot start dictation after losing %s",
  async (withdrawal) => {
    const ready: VoiceStatus = {
      state: "ready",
      running: false,
      model: "small-q8_0",
      backend: "cpu",
      device: null,
      languages: ["auto"],
      language: "auto",
      language_unavailable: false,
      models: ["small-q8_0"],
      backends: ["cpu"],
      fallback: null,
      cpu_model: "small-q8_0",
      slow_on_cpu: false,
    };
    let resolveStatus: (status: VoiceStatus) => void = () => {};
    const status = vi
      .fn()
      .mockResolvedValueOnce({ ...ready, state: "setup_needed" })
      .mockImplementationOnce(
        () =>
          new Promise<VoiceStatus>((resolve) => {
            resolveStatus = resolve;
          }),
      )
      .mockResolvedValue(ready);
    const api: DictationApi = {
      status,
      clearFallback: vi.fn(),
      socket: vi.fn((_handlers: VoiceSocketHandlers) => ({
        begin: vi.fn(),
        audio: vi.fn(),
        end: vi.fn(),
        cancel: vi.fn(),
        close: vi.fn(),
      })),
      startCapture: vi.fn(async () => ({ deviceId: null, stop: vi.fn(async () => {}) })),
      microphones: vi.fn(async () => []),
      workletUrl: "/worklet.js",
    };
    const view = (focused: boolean, dictationAllowed = true) => (
      <DictationContext.Provider value={api}>
        <Composer
          focused={focused}
          dictationAllowed={dictationAllowed}
          running={false}
          onSend={vi.fn()}
          onStop={vi.fn()}
        />
      </DictationContext.Provider>
    );
    const { container, rerender } = render(view(true));
    await act(async () => {
      for (let i = 0; i < 8; i++) await Promise.resolve();
    });
    fireEvent.click(container.querySelector(".voice-mic") as Element);
    expect(status).toHaveBeenCalledTimes(2);
    rerender(view(withdrawal !== "focus", withdrawal !== "dictation admission"));
    await act(async () => {
      resolveStatus(ready);
      for (let i = 0; i < 8; i++) await Promise.resolve();
    });
    expect(api.startCapture).not.toHaveBeenCalled();
    rerender(view(true));
    await act(async () => fireEvent.click(container.querySelector(".voice-mic") as Element));
    expect(api.startCapture).toHaveBeenCalledTimes(1);
  },
);

function scriptSlotScroll(slot: HTMLElement) {
  const metrics = { content: 600, viewport: 200, top: 0 };
  Object.defineProperties(slot, {
    scrollHeight: { configurable: true, get: () => metrics.content },
    clientHeight: { configurable: true, get: () => metrics.viewport },
    scrollTop: {
      configurable: true,
      get: () => metrics.top,
      set: (value: number) => {
        metrics.top = Math.max(0, Math.min(value, metrics.content - metrics.viewport));
      },
    },
  });
  return metrics;
}

test("the first bound update reveals an overflowing composer input without taking focus", () => {
  const { slot } = composerHost();
  scriptSlotScroll(slot);
  const send = screen.getByRole("button", { name: "Send" });
  send.focus();
  fireEvent(window, new Event("resize"));
  expect(slot.scrollTop).toBe(400);
  expect(document.activeElement).toBe(send);
});

test("a composer slot at the bottom follows later chrome growth", () => {
  const { slot } = composerHost();
  const metrics = scriptSlotScroll(slot);
  fireEvent(window, new Event("resize"));
  expect(slot.scrollTop).toBe(400);
  metrics.content = 900;
  fireEvent(window, new Event("resize"));
  expect(slot.scrollTop).toBe(700);
});

test("reading earlier composer panels preserves the manual scroll position through resize", () => {
  const { slot } = composerHost();
  const metrics = scriptSlotScroll(slot);
  fireEvent(window, new Event("resize"));
  slot.scrollTop = 80;
  fireEvent.scroll(slot);
  metrics.content = 900;
  fireEvent(window, new Event("resize"));
  expect(slot.scrollTop).toBe(80);
});

test("returning to the composer slot bottom resumes following later growth", () => {
  const { slot } = composerHost();
  const metrics = scriptSlotScroll(slot);
  fireEvent(window, new Event("resize"));
  slot.scrollTop = 80;
  fireEvent.scroll(slot);
  metrics.content = 900;
  fireEvent(window, new Event("resize"));
  expect(slot.scrollTop).toBe(80);
  slot.scrollTop = 700;
  fireEvent.scroll(slot);
  metrics.content = 1100;
  fireEvent(window, new Event("resize"));
  expect(slot.scrollTop).toBe(900);
});

test("queued font and size updates do not scroll a detached composer slot", async () => {
  const { slot, signals } = composerHost();
  const metrics = scriptSlotScroll(slot);
  fireEvent(window, new Event("resize"));
  expect(slot.scrollTop).toBe(400);
  slot.remove();
  metrics.content = 900;
  await act(async () => {
    fireEvent(window, new Event("resize"));
    signals.ready();
  });
  expect(slot.scrollTop).toBe(400);
});

test("opening a focused drafts panel preserves its position during content growth", async () => {
  const { slot } = composerHost();
  const metrics = scriptSlotScroll(slot);
  slot.scrollTop = 400;
  const panel = document.createElement("details");
  panel.className = "local-drafts";
  panel.innerHTML = "<summary>Queued messages</summary><button>Queued action</button>";
  slot.append(panel);
  const summary = panel.querySelector("summary") as HTMLElement;
  summary.focus();
  await act(async () => {
    panel.open = true;
    metrics.content = 900;
    fireEvent(window, new Event("resize"));
  });
  expect(slot.scrollTop).toBe(400);
  expect(document.activeElement).toBe(summary);
});
