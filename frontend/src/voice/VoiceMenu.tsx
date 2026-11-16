/**
 * The narrow chevron beside the round voice button: offers the three speech engines
 * — System voice, the installed Natural-voice pack, and the user's own Voicebox app — plus, only
 * once Voicebox is the chosen engine, every Voicebox profile as a further choice
 * (`voice.voicebox_profile`). The own-app check runs once per open and only for that engine (it
 * must never reach the user's own Voicebox app before they choose it); the pack's own status is read
 * by the caller (`VoiceControl`) and simply shown here. Also toggles speak-while-writing
 * (`voice.speak_while_writing`) and links to Settings › Chat. `profile`/`speakWhileWriting` are
 * fully controlled — a patch that fails leaves the rows showing whatever the caller still passes
 * in. English literals: Voicebox strings are not part of the i18n catalog.
 */
import * as Dropdown from "@radix-ui/react-dropdown-menu";
import { type ReactElement, useContext, useEffect, useState } from "react";
import { type VoiceboxApi, VoiceboxContext, type VoiceboxStatus } from "./voiceboxApi";
import {
  installedState,
  type SpeechEngine,
  VOICEBOX_RELEASES_URL,
  type VoicePackStatus,
} from "./voicePackApi";
import "./VoiceMenu.css";

export interface VoiceMenuProps {
  engine: SpeechEngine;
  /** This window's own read of the installed voice pack; null while unknown. */
  pack: VoicePackStatus | null;
  profile: string | null;
  speakWhileWriting: boolean;
  url: string;
  patch: (changes: Record<string, unknown>) => Promise<unknown>;
  openSettings: () => void;
  /** Opens a URL outside VIKI, from `VoicePackContext`'s services. Omitted hides "Get Voicebox". */
  openUrl?: (url: string) => void;
  onStatus?: (status: VoiceboxStatus) => void;
  /** Told of every open/close, so the caller can refresh its own pack read on open. */
  onOpenChange?: (open: boolean) => void;
  /** Undefined falls back to `VoiceboxContext`; an explicit `null` means no api even if a
   * context provider is present. */
  api?: VoiceboxApi | null;
}

const SYSTEM_VOICE = "system";
const PACK_VOICE = "pack";

type Outcome = "pending" | VoiceboxStatus | "error" | null;

/** The status line's text, or null when the rows speak for themselves. */
function statusLine(outcome: Outcome, url: string): string | null {
  if (outcome === null) return null;
  if (outcome === "pending") return "Checking Voicebox…";
  if (outcome === "error") return "Couldn't check Voicebox.";
  if (outcome.state === "no_settings") return "Voicebox settings aren't available here.";
  if (outcome.state === "unreachable") return `Voicebox isn't running at ${url}.`;
  if (outcome.state === "unexpected") return `Something other than Voicebox answered at ${url}.`;
  if (outcome.profiles.length === 0) return "Voicebox has no voices yet.";
  return null;
}

/** Whether "Check Voicebox" should offer another try. */
function canRecheck(outcome: Outcome): boolean {
  if (outcome === "error") return true;
  if (outcome === null || outcome === "pending") return false;
  return outcome.state === "unreachable" || outcome.state === "unexpected";
}

/** Whether "Get Voicebox" is worth offering: the own app flatly didn't answer. */
function canGetVoicebox(outcome: Outcome): boolean {
  return (
    outcome !== null &&
    outcome !== "pending" &&
    outcome !== "error" &&
    outcome.state === "unreachable"
  );
}

/** Whether "Natural voices…" (the link to Settings) earns its row: the pack isn't already
 * installed, and isn't flatly unsupported on this platform. */
function showPackLink(pack: VoicePackStatus | null): boolean {
  if (pack === null) return true;
  if (installedState(pack.state)) return false;
  return !(pack.state === "unsupported" && pack.reason === "platform");
}

export function VoiceMenu({
  engine,
  pack,
  profile,
  speakWhileWriting,
  url,
  patch,
  openSettings,
  openUrl,
  onStatus,
  onOpenChange,
  api,
}: VoiceMenuProps): ReactElement {
  const contextApi = useContext(VoiceboxContext);
  const resolvedApi = api === undefined ? contextApi : api;
  const [open, setOpenState] = useState(false);
  const [outcome, setOutcome] = useState<Outcome>(null);
  const [tick, setTick] = useState(0);

  function setOpen(value: boolean): void {
    setOpenState(value);
    onOpenChange?.(value);
  }

  // Keyed on open/tick/engine only: a status call happens once per open, plus once per "Check
  // Voicebox" recheck — never merely because the api/onStatus identity changed while the menu
  // stays open, the same reasoning VoiceboxField's own mount-only check documents. Gated to the
  // Voicebox engine: never reach the user's own Voicebox app before they choose it.
  // biome-ignore lint/correctness/useExhaustiveDependencies: see comment above.
  useEffect(() => {
    if (!open || engine !== "voicebox" || !resolvedApi) {
      setOutcome(null);
      return;
    }
    const controller = new AbortController();
    let live = true;
    setOutcome("pending");
    resolvedApi.status(controller.signal).then(
      (status) => {
        if (!live) return;
        setOutcome(status);
        onStatus?.(status);
      },
      () => {
        if (live) setOutcome("error");
      },
    );
    return () => {
      live = false;
      controller.abort();
    };
  }, [open, tick, engine]);

  const profiles =
    outcome !== null && outcome !== "pending" && outcome !== "error" ? outcome.profiles : [];
  const line = engine === "voicebox" ? statusLine(outcome, url) : null;
  const names = new Intl.DisplayNames(["en"], { type: "language" });
  const packInstalled = pack !== null && installedState(pack.state);
  const speakWhileWritingEnabled =
    (engine === "pack" && packInstalled) || (engine === "voicebox" && profile !== null);
  const radioValue =
    engine === "system" ? SYSTEM_VOICE : engine === "pack" ? PACK_VOICE : (profile ?? SYSTEM_VOICE);

  function pickEngine(value: string): void {
    if (value === SYSTEM_VOICE) patch({ "voice.speech_engine": "system" }).catch(() => {});
    else if (value === PACK_VOICE) patch({ "voice.speech_engine": "pack" }).catch(() => {});
    else
      patch({ "voice.speech_engine": "voicebox", "voice.voicebox_profile": value }).catch(() => {});
  }

  function recheck(event: Event): void {
    event.preventDefault();
    setTick((value) => value + 1);
  }

  return (
    <Dropdown.Root open={open} onOpenChange={setOpen}>
      <Dropdown.Trigger
        className="voice-menu-trigger"
        aria-label="Voice options"
        onClick={() => {
          // Radix opens the trigger on pointerdown, which a plain click never synthesizes;
          // see ModelChip.tsx for the full reasoning this mirrors.
          if (!open) setOpen(true);
        }}
      >
        <svg className="ico" width="14" height="14" viewBox="0 0 16 16" aria-hidden="true">
          <path d="M4 6l4 4 4-4" />
        </svg>
      </Dropdown.Trigger>
      <Dropdown.Portal>
        <Dropdown.Content className="voice-menu menu-panel" side="top" align="end" sideOffset={8}>
          {line !== null && (
            <p className="voice-menu-status" role="status">
              {line}
            </p>
          )}
          {engine === "voicebox" && canRecheck(outcome) && (
            <Dropdown.Item className="mi" onSelect={recheck}>
              Check Voicebox
            </Dropdown.Item>
          )}
          {engine === "voicebox" && openUrl !== undefined && canGetVoicebox(outcome) && (
            <Dropdown.Item className="mi" onSelect={() => openUrl(VOICEBOX_RELEASES_URL)}>
              Get Voicebox
            </Dropdown.Item>
          )}
          <Dropdown.RadioGroup value={radioValue} onValueChange={pickEngine}>
            <Dropdown.RadioItem
              className={`mi${radioValue === SYSTEM_VOICE ? " on" : ""}`}
              value={SYSTEM_VOICE}
            >
              System voice
            </Dropdown.RadioItem>
            {packInstalled && (
              <Dropdown.RadioItem
                className={`mi${radioValue === PACK_VOICE ? " on" : ""}`}
                value={PACK_VOICE}
              >
                Natural voice
              </Dropdown.RadioItem>
            )}
            {engine === "voicebox" &&
              profiles.map((p) => (
                <Dropdown.RadioItem
                  key={p.id}
                  className={`mi${p.id === profile ? " on" : ""}`}
                  value={p.id}
                >
                  {`${p.name} (${names.of(p.language) ?? p.language})`}
                </Dropdown.RadioItem>
              ))}
            {engine === "voicebox" &&
              profile !== null &&
              !profiles.some((p) => p.id === profile) && (
                <Dropdown.RadioItem className="mi on" value={profile}>
                  Chosen Voicebox voice
                </Dropdown.RadioItem>
              )}
          </Dropdown.RadioGroup>
          {showPackLink(pack) && (
            <Dropdown.Item className="mi" onSelect={() => openSettings()}>
              Natural voices…
            </Dropdown.Item>
          )}
          <Dropdown.Separator className="voice-menu-sep" />
          <Dropdown.CheckboxItem
            className={`mi${speakWhileWriting ? " on" : ""}`}
            checked={speakWhileWriting}
            disabled={!speakWhileWritingEnabled}
            onCheckedChange={(checked) => {
              patch({ "voice.speak_while_writing": checked }).catch(() => {});
            }}
          >
            Speak while VIKI writes
            <Dropdown.ItemIndicator className="voice-menu-check" aria-hidden="true">
              <svg className="ico" width="12" height="12" viewBox="0 0 16 16" aria-hidden="true">
                <path d="M3 8.5l3 3 7-7" />
              </svg>
            </Dropdown.ItemIndicator>
          </Dropdown.CheckboxItem>
          <Dropdown.Item className="mi" onSelect={() => openSettings()}>
            Voice settings…
          </Dropdown.Item>
        </Dropdown.Content>
      </Dropdown.Portal>
    </Dropdown.Root>
  );
}
