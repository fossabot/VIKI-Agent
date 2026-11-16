/** Japanese IME-safe editor mechanics. Enter submits once, Shift+Enter keeps a newline,
 * and ongoing typing survives asynchronous handoff. Only an unchanged logical editor clears
 * after a successful callback. A controlled editor delegates clearing to its revision owner.
 */
import {
  type ChangeEvent,
  type KeyboardEvent,
  type ReactElement,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import type { Command, ModelOption } from "../../protocol";
import { trimTrailingNewlines } from "../../state/editors";
import type { ExcerptApi, VaultExcerptPart } from "../../state/excerptApi";
import { DictationStrip, MicButton } from "../../voice/DictationControls";
import { DictationContext } from "../../voice/dictationContext";
import type { DictationControls } from "../../voice/dictationTypes";
import { dictationNotice } from "../../voice/dictationWords";
import type { HandsFreeBridge } from "../../voice/handsFree";
import { useDictation } from "../../voice/useDictation";
import { ActivityOrb, type ActivityState } from "../ActivityOrb/ActivityOrb";
import { useActivityPresentation } from "../ActivityOrb/useActivityPresentation";
import { useIdentity } from "../Avatar/identity";
import { ComposerBeam } from "./ComposerBeam";
import { ExcerptChips } from "./ExcerptChips";
import { isComposingKeydown } from "./ime";
import { ModelChip } from "./ModelChip";
import type { ChipNote } from "./modelChoice";
import { SlashMenu } from "./SlashMenu";
import "./Composer.css";

/** The characters a Japanese keyboard can produce where the user meant to start a command. */
const SLASH_PREFIXES = ["/", "／", "・"];
/** The prefixes that make the text a command on send; "・" only starts a bullet there. */
const COMMAND_PREFIXES = ["/", "／"];

export type ComposerActivity = { state: ActivityState; label: string; since?: number };

const ACTIVITY_CAPTIONS: Record<ActivityState, string> = {
  idle: "Idle",
  active: "Working",
  waiting: "Waiting",
  offline: "Offline",
  error: "Attention",
};

interface HandsFreeFinal {
  text: string;
  serial: number;
  editorKey: string | undefined;
}

export interface ComposerProps {
  activity?: ComposerActivity;
  /** Rendered in `.composer-activity`, in place of the `ActivityOrb`. Omitted, the orb renders
   * as it always has. */
  activityLead?: ReactNode;
  attachmentControl?: ReactNode;
  attachmentStrip?: ReactNode;
  hasAttachments?: boolean;
  onFiles?: (files: readonly File[], source: "paste" | "drop") => void;
  editorKey?: string;
  /** A retained split-pane composer cancels its capture when another pane takes focus. */
  focused?: boolean;
  /** The caller admits capture for this editor, independently of its first focus gesture. */
  dictationAllowed?: boolean;
  value?: string;
  onEdit?: (text: string, start: number, end: number) => void;
  selection?: { start: number; end: number };
  onSelection?: (start: number, end: number) => void;
  submitDisabled?: boolean;
  running: boolean;
  /** While `running`, joined by a Steer button that sends right away instead of waiting: Enter
   * and the button then run `attemptSend` without its usual refusal for `running`. A text that
   * starts with a known command is never steered — it still waits for the reply to end. */
  steer?: boolean;
  /** Success may clear an uncontrolled editor; controlled clearing belongs to the caller. */
  onSend: (text: string) => Promise<boolean>;
  onStop: () => void;
  /** The textarea's accessible name. Default "Message"; LauncherChat passes
   * "Message VIKI". Nothing else about the box changes with it. */
  label?: string;
  /** Replaces the textarea's default placeholder ("Talk to {assistant}… / for skills", or
   * "Talk… / for skills" while the identity is unresolved). Omitted, that default shows. */
  placeholder?: string;
  /** The textarea's `maxLength`. Omitted, the box keeps no length limit. */
  maxLength?: number;
  /** The Send button's `aria-label`. Omitted, it stays "Send". */
  sendLabel?: string;
  /** Prefill pushed in by App (a gate pick); a new non-empty value replaces the box. */
  draft?: string;
  /** Optional recovery prefill, applied only to an empty editor. Live submission retains text. */
  refused?: string;
  /** The previous prompt, for ArrowUp recall; "" when there is none. */
  lastPrompt?: string;
  /** The `/` menu's own list (already filtered of terminal-only commands and /clear by App).
   * Omit, or pass an empty array, and the menu never opens — tests that skip the menu do that. */
  commands?: readonly Command[];
  /** Rendered first inside `.composer-actions`, ahead of `mcpControl`. Omitted, nothing renders
   * here. */
  recipient?: ReactNode;
  /** Rendered inside `.composer-actions`, ahead of the model chip and after `recipient` (if
   * given) — the MCP servers trigger (McpServersButton) is App's only current use. Omitted, it
   * renders nothing. */
  mcpControl?: ReactNode;
  /** The chip renders only with a non-empty `models`. Omit it, alone or with all four related props
   * below (as tests that skip the chip do, and as App does for a profile session, which still
   * passes the four related props, inert, so Model/Effort stay display-only), and the chip does
   * not render. */
  models?: readonly ModelOption[];
  model?: string;
  effort?: string;
  onModel?: (value: string) => void;
  onEffort?: (value: string) => void;
  /** The last model/effort pick's own pending/applied/failed state (useModelChoice,
   * modelChoice.ts), passed straight through to the chip's note slot. */
  modelNote?: ChipNote | null;
  /** Keep the model menu inert until its session can accept a choice. */
  modelDisabledReason?: string | undefined;
  /** Why the caller disables Send (`submitDisabled`); shown verbatim in the blocked-Enter notice. */
  blockedReason?: string | undefined;
  /** Vault excerpts that go with the next message. Chips render, above the box, only with all
   * four excerpt props and at least one part. `onSend` still carries the text alone: the parts
   * travel on this prop, and the caller reads them from its own state when it builds the turn. */
  excerpts?: readonly VaultExcerptPart[];
  onExcerptsChange?: (
    parts: VaultExcerptPart[] | ((current: readonly VaultExcerptPart[]) => VaultExcerptPart[]),
  ) => void;
  excerptApi?: ExcerptApi;
  excerptWorkspaceId?: string;
  /** Hands-free: starts a capture when the loop listens and sends its unedited final. */
  handsFree?: HandsFreeBridge;
  /** Rendered after the mic, ahead of Send/Stop: the round voice toggle and its menu. Given
   * whether it currently stands in Send's place (empty, idle, nothing attached or excerpted, and
   * no reply running or send in flight) so it can take on Send's own look there. Omitted, nothing
   * renders here and Send behaves as it always has. */
  voiceControl?: (primary: boolean) => ReactNode;
}

/** The IME signal a plain keydown carries: either flag alone means a candidate window is open. */
function isComposingEvent(event: KeyboardEvent<HTMLTextAreaElement>): boolean {
  return isComposingKeydown(event.nativeEvent);
}

/** The cap on the box's height, as a fraction of what the user can actually see. The single
 * place it is written.
 *
 * The appearance setting scales the page with `html { zoom: var(--font-scale) }`. Measured in
 * real Edge (e2e/scenarios/shell/narrow-views.test.ts): Chromium's standardized root zoom divides NEITHER
 * `document.documentElement.clientHeight` NOR `window.innerHeight` — both stay the window's own
 * pixels — while `scrollHeight` and the `style.height` written from this are in the zoomed
 * element's local pixels. The cap therefore carries a scale term; without it the box reaches
 * 60% of the visible window at font scale 1.5. */
const MAX_HEIGHT_FRACTION = 0.4;

/** True once the text is nothing but a prefix character plus an unbroken token — the state in
 * which SlashMenu makes sense to show at all. Any whitespace means the user has moved on. */
function isComposingSlashToken(text: string): boolean {
  return SLASH_PREFIXES.some((prefix) => text.startsWith(prefix)) && !/\s/.test(text);
}

/** True when the leading token names one of the commands this session offers — the service's own
 * reading (`session._command`), where a leading path such as "/notes/x.md" is not a command. The
 * fullwidth prefix stays in: it names no command there, so including it only ever over-blocks. */
function startsWithCommand(text: string, commands: readonly Command[] | undefined): boolean {
  const prefix = COMMAND_PREFIXES.find((candidate) => text.startsWith(candidate));
  if (prefix === undefined) return false;
  const token = text.slice(prefix.length).split(/\s/)[0];
  return commands?.some((command) => command.name === token) === true;
}

/** The working line's clock: mounted only once `since` is known, ticks itself once a second
 * and says nothing before 10s of elapsed time, then `12 s` and `1 min 5 s` past a minute.
 * `aria-hidden`: the strip's atomic announcement names the work, not a number that would
 * re-announce every second. */
function Elapsed({ since }: { since: number }): ReactElement | null {
  const [, retick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => retick((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, []);
  const seconds = Math.floor((Date.now() - since) / 1000);
  if (seconds < 10) return null;
  const text = seconds < 60 ? `${seconds} s` : `${Math.floor(seconds / 60)} min ${seconds % 60} s`;
  return (
    <span className="composer-activity-elapsed" aria-hidden="true">
      {text}
    </span>
  );
}

export function Composer({
  activity,
  activityLead,
  attachmentControl,
  attachmentStrip,
  hasAttachments = false,
  onFiles,
  editorKey,
  focused = true,
  dictationAllowed = true,
  value,
  onEdit,
  selection,
  onSelection,
  submitDisabled = false,
  running,
  steer,
  onSend,
  onStop,
  label = "Message",
  placeholder,
  maxLength,
  sendLabel,
  draft,
  refused,
  lastPrompt,
  commands,
  recipient,
  mcpControl,
  models,
  modelDisabledReason,
  model,
  effort,
  onModel,
  onEffort,
  modelNote,
  blockedReason,
  excerpts,
  onExcerptsChange,
  excerptApi,
  excerptWorkspaceId,
  handsFree,
  voiceControl,
}: ComposerProps): ReactElement {
  const { assistantName, unresolved } = useIdentity();
  const presence: ComposerActivity = activity ?? {
    state: blockedReason ? "waiting" : running ? "active" : "idle",
    label: blockedReason ? "Send unavailable" : running ? "Reply in progress" : "No reply running",
  };
  const beamRef = useRef<HTMLDivElement>(null);
  const presentation = useActivityPresentation(beamRef);
  const [localText, setLocalText] = useState("");
  const text = value ?? localText;
  const onEditRef = useRef(onEdit);
  onEditRef.current = onEdit;
  const setText = useCallback((next: string, start = next.length, end = start) => {
    setLocalText(next);
    onEditRef.current?.(next, start, end);
  }, []);
  const editSerial = useRef(0);
  const currentEditor = useRef(editorKey);
  if (currentEditor.current !== editorKey) {
    currentEditor.current = editorKey;
    editSerial.current += 1;
  }
  const sending = useRef(false);
  const [pending, setPending] = useState(false);
  // Why a blocked Enter did nothing. Cleared by the next edit, or whenever `running` or
  // `submitDisabled` changes.
  const [blockedNotice, setBlockedNotice] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const positionMenuRef = useRef<(() => void) | null>(null);
  // Set right before a programmatic setText (draft or recall) so the layout effect below
  // knows to focus the box and move the caret once the new value has committed to the DOM.
  const caretToEndRef = useRef(false);

  // Dictation runs only under the provider; without one the hook idles and the inert
  // glyph stays. It edits the draft through setText alone, so it cannot send a turn.
  const dictationApi = useContext(DictationContext);
  const composingRef = useRef(false);
  // Hands-free: the capture we started on the loop's `listening` edge, the
  // exact draft/editor and editSerial when its final landed (so later edits refuse auto-send), and a
  // final an IME composition is still holding when the capture's `done` already arrived.
  const handsFreeCaptureRef = useRef(false);
  const handsFreeFinalRef = useRef<HandsFreeFinal | null>(null);
  const handsFreePendingCompositionRef = useRef(false);
  // App can edit a controlled value directly (Quote into draft, for example), bypassing the DOM
  // change handler. Remember even a deviation later restored to the final; its own commit matches.
  useLayoutEffect(() => {
    const lastFinal = handsFreeFinalRef.current;
    if (lastFinal && text !== lastFinal.text) editSerial.current += 1;
  }, [text]);
  // Read right before the automatic Send: the mode may have ended in the render the capture did.
  const handsFreeActiveRef = useRef(false);
  handsFreeActiveRef.current = dictationAllowed && Boolean(handsFree?.active);
  const [dictation, dictationControls] = useDictation({
    api: dictationApi,
    text,
    // A dictated final is an edit: a send still in flight must not clear the box over it.
    setText: (next, start, end) => {
      editSerial.current += 1;
      if (handsFreeCaptureRef.current) {
        handsFreeFinalRef.current = { text: next, serial: editSerial.current, editorKey };
      }
      setText(next, start, end);
    },
    caret: () => textareaRef.current?.selectionEnd ?? text.length,
    composing: () => composingRef.current,
    focusBox: (caret) => {
      textareaRef.current?.focus();
      textareaRef.current?.setSelectionRange(caret, caret);
    },
    endOnFinal: Boolean(handsFree?.active),
  });
  const dictating = ["starting", "recording", "finalizing"].includes(dictation.phase);
  // biome-ignore lint/correctness/useExhaustiveDependencies: controls are stable for this mount.
  useLayoutEffect(() => {
    if (!focused || !dictationAllowed) dictationControls.cancel();
  }, [focused, dictationAllowed]);

  async function attemptHandsFreeSend(lastFinal: HandsFreeFinal): Promise<void> {
    const bridge = handsFree;
    if (!bridge) return;
    const hasContent = text.trim() !== "" || hasAttachments;
    const blocked =
      composingRef.current ||
      editSerial.current !== lastFinal.serial ||
      text !== lastFinal.text ||
      editorKey !== lastFinal.editorKey ||
      running ||
      sendDisabled ||
      sending.current ||
      !hasContent;
    if (blocked) {
      bridge.dispatch({ type: "sendRefused" });
      return;
    }
    if (!handsFreeActiveRef.current) return;
    const heard = trimTrailingNewlines(text);
    const message = bridge.message?.(heard) ?? heard;
    // The box holds exactly what is sent, as Wrap up's convention does: the draft saved with the
    // turn is then the box's own text, which clears once the send is accepted. A refusal puts the
    // words heard back, unless the box was edited meanwhile.
    if (message !== heard) {
      editSerial.current += 1;
      setText(message);
    }
    const sendSerial = editSerial.current;
    const refused = () => {
      bridge.dispatch({ type: "sendRefused" });
      if (message !== heard && editSerial.current === sendSerial) {
        editSerial.current += 1;
        setText(text);
      }
    };
    sending.current = true;
    setPending(true);
    try {
      const handedOff = await onSend(message);
      if (handedOff) {
        bridge.dispatch({ type: "sendAccepted" });
        if (value === undefined && editSerial.current === sendSerial) {
          editSerial.current += 1;
          setText("");
        }
      } else {
        refused();
      }
    } catch {
      refused();
    } finally {
      sending.current = false;
      setPending(false);
    }
  }

  // The capture's own outcome, read off `dictation.phase` going back to idle: a composition still
  // holding the final counts as heard (the open issue this freezes: captureDone then sendRefused,
  // once the composition commits too late to send), a landed final attempts the send, and no final
  // at all is reported empty. A deliberate exit (Escape) clears the ref first, so that outcome is
  // never reported (the reducer ignores these events once it is off, but the Composer test checks
  // the dispatch mock directly).
  const handsFreePrevPhaseRef = useRef(dictation.phase);
  // biome-ignore lint/correctness/useExhaustiveDependencies: only the phase transition itself should fire this; the body reads handsFree and attemptHandsFreeSend fresh off the render it runs in.
  useEffect(() => {
    const previous = handsFreePrevPhaseRef.current;
    handsFreePrevPhaseRef.current = dictation.phase;
    if (!handsFreeCaptureRef.current || dictation.phase !== "idle" || previous === "idle") return;
    handsFreeCaptureRef.current = false;
    const bridge = handsFree;
    if (!bridge) return;
    if (composingRef.current) {
      handsFreePendingCompositionRef.current = true;
      handsFreeFinalRef.current = null;
      bridge.dispatch({ type: "captureDone" });
      return;
    }
    const lastFinal = handsFreeFinalRef.current;
    handsFreeFinalRef.current = null;
    if (lastFinal === null) {
      bridge.dispatch({ type: "captureEmpty" });
      return;
    }
    bridge.dispatch({ type: "captureDone" });
    void attemptHandsFreeSend(lastFinal);
  }, [dictation.phase]);

  // The loop's `listening` edge starts one toggled capture (voice/handsFree.ts owns when
  // `listening` is true; this only opens the mic once per rising edge, and only once dictation
  // itself is ready, so no call races the status fetch `useDictation` kicks off on mount). This
  // runs after the outcome effect above, so the capture it just started is never read as the
  // outcome of this same render's idle phase. Its falling edge with that capture still open is the
  // mode ending some other way than this box's Esc (the strip's End, the toggle, a hidden window,
  // a session switch): the capture is cancelled, so nothing it heard is sent.
  const handsFreeWasListeningRef = useRef(false);
  // biome-ignore lint/correctness/useExhaustiveDependencies: dictationControls is stable for the component's life.
  useEffect(() => {
    const listening = dictationAllowed && Boolean(handsFree?.active && handsFree.listening);
    if (!listening) {
      handsFreeWasListeningRef.current = false;
      if (handsFreeCaptureRef.current) {
        handsFreeCaptureRef.current = false;
        handsFreeFinalRef.current = null;
        dictationControls.cancel();
      }
      return;
    }
    if (handsFreeWasListeningRef.current || dictation.phase !== "idle") return;
    handsFreeWasListeningRef.current = true;
    handsFreeCaptureRef.current = true;
    handsFreeFinalRef.current = null;
    handsFreePendingCompositionRef.current = false;
    dictationControls.toggle();
  }, [dictationAllowed, handsFree?.active, handsFree?.listening, dictation.phase]);

  // Escape dismisses the slash menu for this exact text only (see isComposingSlashToken above);
  // typing anything — even one more character of the same command name — re-evaluates it fresh.
  const [closedSlashText, setClosedSlashText] = useState<string | null>(null);
  const hasCommands = Boolean(commands && commands.length > 0);
  const slashMenuOpen =
    focused && hasCommands && isComposingSlashToken(text) && text !== closedSlashText;
  const hasModels = Boolean(models && models.length > 0);

  // Send waits for every chip to be `fresh`, and a command cannot carry an excerpt.
  const [chipsFresh, setChipsFresh] = useState(false);
  const hasExcerpts = Boolean(excerpts && excerpts.length > 0);
  const showChips = Boolean(
    excerpts && excerpts.length > 0 && onExcerptsChange && excerptApi && excerptWorkspaceId,
  );
  const chipsPending = showChips && !chipsFresh;
  const isCommand = startsWithCommand(text, commands);
  const commandWithExcerpt = hasExcerpts && isCommand;
  const sendDisabled = submitDisabled || chipsPending || commandWithExcerpt || dictating;
  // Steer (running && steer) sends right away instead of waiting; a command text still has to
  // wait for the reply to end, so it is never steered — Enter then shows the plain running notice.
  const steering = running && Boolean(steer) && !isCommand;
  const blockedByRunning = running && !steering;
  const steerDisabled =
    pending || sendDisabled || isCommand || (text.trim() === "" && !hasAttachments);
  // The round voice control stands in Send's place only when there is nothing else to do here:
  // an empty, idle box, nothing attached or excerpted, and no reply running or in flight.
  const primary =
    voiceControl !== undefined &&
    !running &&
    !pending &&
    text.trim() === "" &&
    !hasAttachments &&
    !hasExcerpts;

  // A new non-empty draft replaces the box; effect deps compare by value, so the same draft
  // seen again (the user re-picks the same row) is a no-op and never fights their typing.
  // The DOM value, not `text`, is the comparison: this passive effect runs after the commit, so
  // it reads the committed text without taking a dependency that would re-run it on every
  // keystroke. Arming the ref before a setText React would discard as equal would leave it
  // armed, and the user's next keystroke would jump the caret to the end.
  useEffect(() => {
    if (!draft || draft === textareaRef.current?.value) return;
    caretToEndRef.current = true;
    editSerial.current += 1;
    setText(draft);
  }, [draft, setText]);

  // Explicit recovery prefill must not replace current typing. Read the committed DOM value
  // without making the effect run again on each keystroke, as with the draft effect above.
  useEffect(() => {
    if (!refused || textareaRef.current?.value) return;
    caretToEndRef.current = true;
    editSerial.current += 1;
    setText(refused);
  }, [refused, setText]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: must rerun on every text change; the body clears the notice unconditionally, it never reads `text` itself.
  useEffect(() => {
    setBlockedNotice(null);
  }, [text]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: the body clears the notice unconditionally on any change to either flag; it never reads them itself.
  useEffect(() => {
    setBlockedNotice(null);
  }, [running, sendDisabled]);

  const initialSelection = useRef(selection);
  useLayoutEffect(() => {
    const caret = initialSelection.current;
    if (caret) textareaRef.current?.setSelectionRange(caret.start, caret.end);
  }, []);

  const resizeTextarea = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto"; // let scrollHeight reflect content, not the previous height
    // `|| window.innerHeight` is the never-laid-out fallback, not a second opinion: a document
    // that has had no layout reports clientHeight 0 (every jsdom test does), and a cap of 0
    // would collapse the box to nothing. A real window always answers with its own height.
    const visible = document.documentElement.clientHeight || window.innerHeight;
    // Both heights above are the window's own pixels; `scrollHeight` and the height written
    // below are the zoomed element's. Dividing by the root's own `--font-scale` is what makes
    // the fraction mean the same thing on screen at every scale (see MAX_HEIGHT_FRACTION). At
    // scale 1 the property is unset, `parseFloat("")` is NaN, and `|| 1` leaves this untouched.
    const scale =
      Number.parseFloat(document.documentElement.style.getPropertyValue("--font-scale")) || 1;
    let cap = Math.round((visible * MAX_HEIGHT_FRACTION) / scale);
    const slot = el.closest<HTMLElement>(".composer-slot");
    const conversation = slot?.closest<HTMLElement>(".conversation");
    if (slot && conversation?.clientHeight) {
      let chrome = 0;
      for (const child of conversation.querySelectorAll<HTMLElement>(":scope > *")) {
        if (child === slot || child.classList.contains("conversation-scroll")) continue;
        const style = getComputedStyle(child);
        if (style.position === "absolute" || style.position === "fixed") continue;
        chrome +=
          child.offsetHeight +
          (Number.parseFloat(style.marginTop) || 0) +
          (Number.parseFloat(style.marginBottom) || 0);
      }
      const style = getComputedStyle(slot);
      const padding =
        (Number.parseFloat(style.paddingTop) || 0) + (Number.parseFloat(style.paddingBottom) || 0);
      const pill = el.closest<HTMLElement>(".composer");
      // Keep wrapped controls in the baseline without counting a long draft as fixed chrome.
      const naturalPill = Math.max(56, (pill?.offsetHeight ?? 56) - (el.offsetHeight || 54) + 54);
      const core = padding + naturalPill;
      const transcript = conversation.querySelector<HTMLElement>(":scope > .conversation-scroll");
      const scrollMinimum = transcript
        ? Number.parseFloat(getComputedStyle(transcript).minHeight) || 0
        : 0;
      // Short panes can scroll vertically around the natural core and the message viewport.
      const maximum = Math.max(
        core,
        Math.floor(
          Math.min(
            conversation.clientHeight * 0.6,
            conversation.clientHeight - chrome - scrollMinimum,
          ),
        ),
      );
      const px = (value: string) => Number.parseFloat(value) || 0;
      const borderWidth = (width: string, style: string) =>
        style && style !== "none" && style !== "hidden" ? px(width) : 0;
      const margins = (node: HTMLElement) => {
        const style = getComputedStyle(node);
        return px(style.marginTop) + px(style.marginBottom);
      };
      let fixed = 0;
      for (const child of slot.querySelectorAll<HTMLElement>(":scope > *")) {
        const style = getComputedStyle(child);
        if (
          !child.offsetHeight ||
          style.display === "none" ||
          style.position === "absolute" ||
          style.position === "fixed"
        )
          continue;
        let height = child.offsetHeight;
        if (child.matches(".draft-recovery, .local-drafts")) {
          height =
            (child.querySelector<HTMLElement>(":scope > summary")?.offsetHeight ?? 0) +
            px(style.paddingTop) +
            px(style.paddingBottom) +
            borderWidth(style.borderTopWidth, style.borderTopStyle) +
            borderWidth(style.borderBottomWidth, style.borderBottomStyle);
        } else if (child.classList.contains("composer-wrap")) {
          height -= pill?.offsetHeight ?? 56;
        } else if (child.classList.contains("draft-conflict")) {
          const body = child.querySelector<HTMLElement>(":scope > pre");
          if (body) height -= body.offsetHeight + margins(body);
        }
        fixed += Math.max(0, height) + margins(child);
      }
      const minimum = Math.min(maximum, core + fixed);
      const requested = Number.parseFloat(slot.style.height);
      const budget = Number.isFinite(requested)
        ? Math.max(minimum, Math.min(maximum, requested))
        : maximum;
      const free = Math.max(0, budget - core - fixed);
      const bodies = slot.querySelectorAll<HTMLElement>(
        ":scope > .draft-recovery[open], :scope > .local-drafts[open], :scope > .draft-conflict > pre",
      );
      const count = Array.from(bodies).filter(
        (body) => body.offsetHeight > 0 && getComputedStyle(body).display !== "none",
      ).length;
      const growth = Math.min(
        Math.max(0, el.scrollHeight - 54),
        Math.max(0, cap - 54),
        Math.floor(free / (count + 1)),
      );
      const panelHeight = Math.max(54, count ? Math.floor((free - growth) / count) : 0);
      slot.style.setProperty("--composer-max-height", `${maximum}px`);
      slot.style.setProperty("--composer-min-height", `${minimum}px`);
      slot.style.setProperty("--composer-panel-height", `${panelHeight}px`);
      cap = Math.min(cap, 54 + growth);
    }
    el.style.height = `${Math.min(el.scrollHeight, cap)}px`;
    positionMenuRef.current?.();
  }, []);

  // Layout, not passive: height and pushed-draft caret changes land before paint.
  // biome-ignore lint/correctness/useExhaustiveDependencies: each text change resizes; callbacks never move the caret.
  useLayoutEffect(() => {
    resizeTextarea();
    const el = textareaRef.current;
    if (!el) return;
    if (caretToEndRef.current) {
      const end = el.value.length;
      el.focus();
      el.setSelectionRange(end, end);
      caretToEndRef.current = false;
    }
    const slot = el.closest<HTMLElement>(".composer-slot");
    return () => {
      slot?.style.removeProperty("--composer-min-height");
      slot?.style.removeProperty("--composer-max-height");
      slot?.style.removeProperty("--composer-panel-height");
    };
  }, [text, resizeTextarea]);

  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    let active = true;
    const slot = el.closest<HTMLElement>(".composer-slot");
    let followBottom = true;
    const trackScroll = () => {
      if (slot) followBottom = slot.scrollHeight - slot.clientHeight - slot.scrollTop <= 1;
    };
    const update = () => {
      if (!active || !el.isConnected || (slot && !slot.isConnected)) return;
      resizeTextarea();
      const focus = document.activeElement;
      const panelFocused =
        focus instanceof Element &&
        slot?.contains(focus) &&
        focus.closest(".draft-recovery, .local-drafts, .draft-conflict");
      if (followBottom && slot && !panelFocused) slot.scrollTop = slot.scrollHeight;
    };
    let width = el.clientWidth;
    let targets = new Set<Element>();
    const sizes = new Map<Element, { width: number; height: number }>();
    const observer =
      typeof ResizeObserver === "undefined"
        ? null
        : new ResizeObserver((entries) => {
            if (!active) return;
            const next =
              entries.find((entry) => entry.target === el)?.contentRect?.width ?? el.clientWidth;
            let changed = next !== width;
            width = next;
            for (const entry of entries) {
              if (entry.target === el || !targets.has(entry.target)) continue;
              const size = {
                width: entry.contentRect?.width ?? entry.target.clientWidth,
                height: entry.contentRect?.height ?? entry.target.clientHeight,
              };
              const prior = sizes.get(entry.target);
              if (prior?.width !== size.width || prior?.height !== size.height) changed = true;
              sizes.set(entry.target, size);
            }
            // Ignore unchanged dimensions and the textarea's own height notifications.
            if (changed) update();
          });
    const conversation = el.closest(".conversation");
    const wrap = el.closest(".composer-wrap");
    const bindTargets = () => {
      const next = new Set<Element>([el]);
      for (const node of [conversation, slot, wrap]) if (node) next.add(node);
      for (const child of conversation?.children ?? []) {
        if (!child.matches(".conversation-scroll, .composer-slot")) next.add(child);
      }
      for (const child of slot?.children ?? []) next.add(child);
      if (next.size === targets.size && Array.from(next).every((node) => targets.has(node))) return;
      if (targets.size) observer?.disconnect();
      targets = next;
      for (const node of sizes.keys()) if (!targets.has(node)) sizes.delete(node);
      for (const node of targets) observer?.observe(node);
    };
    bindTargets();
    const bounds = new MutationObserver(() => {
      if (!active) return;
      bindTargets();
      update();
    });
    if (conversation) bounds.observe(conversation, { childList: true });
    if (slot)
      bounds.observe(slot, {
        childList: true,
        subtree: true,
        characterData: true,
        attributes: true,
        attributeFilter: ["open", "hidden", "class"],
      });
    const appearance = new MutationObserver(update);
    appearance.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["style", "data-density"],
    });
    window.addEventListener("resize", update);
    slot?.addEventListener("scroll", trackScroll);
    const fonts = document.fonts;
    fonts?.addEventListener("loadingdone", update);
    fonts?.ready.then(update);
    update();
    return () => {
      active = false;
      observer?.disconnect();
      bounds.disconnect();
      appearance.disconnect();
      slot?.removeEventListener("scroll", trackScroll);
      window.removeEventListener("resize", update);
      fonts?.removeEventListener("loadingdone", update);
    };
  }, [resizeTextarea]);

  useLayoutEffect(() => {
    const el = textareaRef.current;
    if (!slashMenuOpen || !el) return;
    const input = el.closest<HTMLElement>(".composer");
    const slot = el.closest<HTMLElement>(".composer-slot");
    const conversation = slot?.closest<HTMLElement>(".conversation");
    const menu = el.closest(".composer-wrap")?.querySelector<HTMLElement>(".menu");
    if (!input || !slot || !conversation || !menu) return;
    let active = true;
    function position() {
      if (!active || !input || !slot || !conversation || !menu) return;
      const visible = document.documentElement.clientHeight || window.innerHeight;
      const scale =
        Number.parseFloat(document.documentElement.style.getPropertyValue("--font-scale")) || 1;
      const frame = conversation.getBoundingClientRect();
      const clip = slot.getBoundingClientRect();
      const rect = input.getBoundingClientRect();
      const start = Math.max(0, Math.min(visible, Math.max(frame.top, clip.top)));
      const end = Math.max(start, Math.min(visible, frame.bottom, clip.bottom));
      const top = Math.max(start, Math.min(rect.top, end));
      const left = Math.max(0, frame.left, Math.min(rect.left, frame.right, window.innerWidth));
      const width =
        Math.max(0, Math.min(rect.right, frame.right, window.innerWidth) - left) / scale;
      const room = Math.max(
        0,
        Math.min((visible * 0.6) / scale, (top - Math.max(0, frame.top)) / scale - 8),
      );
      menu.dataset.composerHosted = "";
      menu.style.left = `${left / scale}px`;
      menu.style.width = `${width}px`;
      menu.style.bottom = `${(visible - top) / scale + 8}px`;
      menu.style.maxHeight = `${room}px`;
    }
    positionMenuRef.current = position;
    position();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(position);
    for (const node of [input, slot, conversation]) observer?.observe(node);
    slot.addEventListener("scroll", position);
    conversation.addEventListener("scroll", position);
    return () => {
      active = false;
      positionMenuRef.current = null;
      observer?.disconnect();
      slot.removeEventListener("scroll", position);
      conversation.removeEventListener("scroll", position);
    };
  }, [slashMenuOpen]);

  // One line for each cause, in the order attemptSend checks them; sending.current alone (the
  // in-flight `pending` note) gets no notice of its own — that note already shows.
  function blockedMessage(): string {
    if (running && !sendDisabled) return "A reply is still running. Send when it ends.";
    return blockedReason ?? "You can't send yet. Check the notes around the message box.";
  }

  async function attemptSend(): Promise<void> {
    if (dictating) {
      setBlockedNotice(dictationNotice("finish_first"));
      return;
    }
    const hasContent = text.trim() !== "" || hasAttachments;
    if (blockedByRunning || sendDisabled || sending.current || !hasContent) {
      // A command with an excerpt already has its own standing line above the box.
      if (hasContent && (blockedByRunning || submitDisabled || chipsPending)) {
        setBlockedNotice(blockedMessage());
      }
      return;
    }
    const serial = editSerial.current;
    sending.current = true;
    setPending(true);
    try {
      const handedOff = await onSend(trimTrailingNewlines(text));
      if (value === undefined && handedOff && editSerial.current === serial) {
        editSerial.current += 1;
        setText("");
      }
    } catch {
      // A failed handoff leaves the local editor intact.
    } finally {
      sending.current = false;
      setPending(false);
    }
  }

  function handleChange(event: ChangeEvent<HTMLTextAreaElement>): void {
    editSerial.current += 1;
    setText(event.target.value, event.target.selectionStart, event.target.selectionEnd);
  }

  // The only rewrite of the user's own words: the prefix and the token they were filtering on
  // become "/<name> ". The trailing space is what makes isComposingSlashToken close the menu.
  function handleSlashPick(command: Command): void {
    caretToEndRef.current = true;
    editSerial.current += 1;
    setText(`/${command.name} `);
  }

  function handleSlashClose(): void {
    setClosedSlashText(text);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>): void {
    // No `slashMenuOpen` guard here: when the menu is open and wants this key, SlashMenu's own
    // capture-phase listener already stopped it from reaching this handler at all (see the
    // docstring above) — so whatever arrives here is this box's to read, menu or no menu.
    if (event.key === "Enter" && !event.shiftKey) {
      if (isComposingEvent(event)) return; // the IME's own Enter closes its candidate window
      event.preventDefault();
      void attemptSend();
      return;
    }
    if (event.key === "Escape") {
      if (isComposingEvent(event)) return;
      if (handsFree?.active) {
        // The first Esc leaves hands-free; it never stops the running turn. Marked
        // handled, so the window's own Esc listeners leave it alone.
        event.preventDefault();
        handsFreeCaptureRef.current = false;
        if (dictating) dictationControls.cancel();
        handsFree.dispatch({ type: "exit", reason: "esc" });
        return;
      }
      if (dictating) dictationControls.cancel();
      else if (running) onStop();
      return;
    }
    if (event.key === "ArrowUp") {
      if (isComposingEvent(event)) return;
      if (text === "" && lastPrompt) {
        event.preventDefault();
        caretToEndRef.current = true;
        editSerial.current += 1;
        setText(lastPrompt);
      }
    }
  }

  // With the loop on but not listening, the mic opens no capture of its own: a press is the loop's
  // `micPressed` (an interrupt while it speaks, ignored otherwise), and the listening edge above
  // opens the capture.
  const interrupt = () => handsFree?.dispatch({ type: "micPressed" });
  const micControls: DictationControls = !dictationAllowed
    ? { ...dictationControls, press: () => {}, release: () => {}, toggle: () => {} }
    : !focused
      ? { ...dictationControls, toggle: () => {} }
      : handsFree?.active && !handsFree.listening
        ? { ...dictationControls, press: interrupt, release: () => {}, toggle: interrupt }
        : dictationControls;

  return (
    <div className="composer-wrap" data-activity={presence.state}>
      {attachmentStrip}
      {showChips && excerpts && onExcerptsChange && excerptApi && excerptWorkspaceId ? (
        <ExcerptChips
          parts={excerpts}
          api={excerptApi}
          workspaceId={excerptWorkspaceId}
          onFreshChange={setChipsFresh}
          onRemove={(index) => onExcerptsChange(excerpts.filter((_, i) => i !== index))}
          onRefresh={(index, fresh) =>
            onExcerptsChange((current) =>
              current.map((part) => (part === excerpts[index] ? fresh : part)),
            )
          }
        />
      ) : null}
      {showChips && text.trim() === "" && !hasAttachments ? (
        <div className="composer-blocked jp" role="status">
          Enter a question or instruction to send an excerpt.
        </div>
      ) : null}
      {commandWithExcerpt ? (
        <div className="composer-blocked jp" role="status">
          Commands cannot be sent with excerpts.
        </div>
      ) : null}
      {pending ? (
        <div className="card-note jp" role="status">
          Waiting for the send response…
        </div>
      ) : null}
      {slashMenuOpen && commands && (
        <SlashMenu
          commands={commands}
          query={text.slice(1)}
          onPick={handleSlashPick}
          onClose={handleSlashClose}
        />
      )}
      {blockedNotice ? (
        <div className="composer-blocked jp" role="status">
          {blockedNotice}
        </div>
      ) : null}
      {dictationApi ? <DictationStrip view={dictation} controls={dictationControls} /> : null}
      <div
        className="composer-activity"
        role="status"
        aria-label="Chat activity"
        aria-atomic="true"
      >
        {activityLead ?? <ActivityOrb state={presence.state} />}
        <div className="composer-activity-copy">
          <div className="composer-activity-heading">
            <span className="composer-activity-name">{unresolved ? "Chat" : assistantName}</span>
            <span className="composer-activity-caption">{ACTIVITY_CAPTIONS[presence.state]}</span>
          </div>
          <div className="composer-activity-label">
            <span
              key={presence.label}
              className={
                presence.state === "active"
                  ? "composer-activity-text working-shimmer"
                  : "composer-activity-text"
              }
            >
              {presence.label}
            </span>
            {presence.since !== undefined ? <Elapsed since={presence.since} /> : null}
          </div>
        </div>
      </div>
      <div
        ref={beamRef}
        className="composer-beam-host"
        data-visible={presentation.visible}
        data-motion={presentation.motion ? "allow" : "reduce"}
      >
        <fieldset
          className="composer"
          aria-label="Chat composer"
          data-region="composer"
          onFocus={(event) => {
            // A short pane can scroll outside the slot; expose the whole focused control.
            event.target.scrollIntoView?.({ block: "nearest", inline: "nearest" });
          }}
          onDragOver={(event) => {
            if (onFiles && event.dataTransfer.types.includes("Files")) event.preventDefault();
          }}
          onDrop={(event) => {
            if (!onFiles || event.dataTransfer.files.length === 0) return;
            event.preventDefault();
            onFiles(Array.from(event.dataTransfer.files), "drop");
          }}
        >
          {attachmentControl ?? (
            <svg
              className="ico composer-icon"
              width="18"
              height="18"
              viewBox="0 0 16 16"
              aria-hidden="true"
            >
              <path d="M8 3v10M3 8h10" />
            </svg>
          )}
          <textarea
            ref={textareaRef}
            className="composer-box jp"
            rows={1}
            maxLength={maxLength}
            value={text}
            onChange={handleChange}
            onPaste={(event) => {
              if (!onFiles || event.clipboardData.files.length === 0) return;
              // Text beside the file is the user's paste: let the browser place it, and skip
              // the file handoff, or a normal copy-paste would swallow the text as a file.
              if (event.clipboardData.getData("text/plain") !== "") return;
              event.preventDefault();
              onFiles(Array.from(event.clipboardData.files), "paste");
            }}
            onSelect={(event) =>
              onSelection?.(event.currentTarget.selectionStart, event.currentTarget.selectionEnd)
            }
            onKeyDown={handleKeyDown}
            onCompositionStart={() => {
              composingRef.current = true;
            }}
            onCompositionEnd={() => {
              composingRef.current = false;
              dictationControls.compositionEnd();
              if (handsFreePendingCompositionRef.current) {
                handsFreePendingCompositionRef.current = false;
                // The held final committed only after the capture's `done` already arrived: too
                // late to count as a displayed, unedited final, so it is never sent.
                handsFree?.dispatch({ type: "sendRefused" });
              }
            }}
            placeholder={
              placeholder ??
              (unresolved ? "Talk… / for skills" : `Talk to ${assistantName}… / for skills`)
            }
            aria-label={label}
          />
          <div className="composer-actions">
            {recipient}
            {mcpControl}
            {hasModels && models && (
              <ModelChip
                models={models}
                model={model ?? ""}
                effort={effort ?? ""}
                running={running}
                onModel={onModel ?? (() => {})}
                onEffort={onEffort ?? (() => {})}
                note={modelNote ?? null}
                disabledReason={modelDisabledReason}
              />
            )}
            {dictationApi ? (
              <MicButton view={dictation} controls={micControls} />
            ) : (
              <svg
                className="ico composer-mic"
                width="18"
                height="18"
                viewBox="0 0 16 16"
                aria-hidden="true"
              >
                <rect x="5.5" y="1.5" width="5" height="8" rx="2.5" />
                <path d="M3 7.5a5 5 0 0 0 10 0M8 12.5v2" />
              </svg>
            )}
            {voiceControl?.(primary)}
            {running ? (
              <>
                {steer ? (
                  <button
                    type="button"
                    className="composer-steer"
                    aria-label="Steer"
                    title="Sends this now. Claude reads it after its current step."
                    disabled={steerDisabled}
                    onClick={() => void attemptSend()}
                  >
                    <svg
                      className="ico"
                      width="16"
                      height="16"
                      viewBox="0 0 16 16"
                      style={{ strokeWidth: 2 }}
                      aria-hidden="true"
                    >
                      <path d="M8 13V3M3.5 7.5 8 3l4.5 4.5" />
                    </svg>
                  </button>
                ) : null}
                <button
                  type="button"
                  className="composer-stop"
                  aria-label="Stop"
                  title="Stops the reply. Agents running in the background keep going."
                  onClick={onStop}
                >
                  <span className="composer-stop-dot" aria-hidden="true" />
                  Stop
                  <span className="kbd">Esc</span>
                </button>
              </>
            ) : !primary ? (
              <button
                type="button"
                className="composer-submit"
                aria-label={sendLabel ?? "Send"}
                disabled={pending || sendDisabled}
                onClick={() => void attemptSend()}
              >
                <svg
                  className="ico"
                  width="16"
                  height="16"
                  viewBox="0 0 16 16"
                  style={{ strokeWidth: 2 }}
                  aria-hidden="true"
                >
                  <path d="M8 13V3M3.5 7.5 8 3l4.5 4.5" />
                </svg>
              </button>
            ) : null}
          </div>
        </fieldset>
        {presence.state === "active" && presentation.motion ? (
          <ComposerBeam theme={presentation.theme} />
        ) : null}
      </div>
    </div>
  );
}
