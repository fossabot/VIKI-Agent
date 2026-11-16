/**
 * VIKI's chat: the launcher panel's own conversation. The caller renders the transcript for
 * `decision.launcher_session` (App's `renderMessagesFor`) and hands it in as `messages`; this
 * component only lays it out over a cut-down `Composer` — no attachment props — labelled
 * "Message VIKI" so it reads distinctly from the main window's own composer. Its mic comes from
 * the window's `DictationContext`, the same as the main composer's; its voice comes from the
 * `handsFree` and `voiceShortcut` props below — omitted, the composer carries no voice
 * control.
 *
 * `sessionId` is null before the first message in this launcher. The first send, and turning a
 * null id into a real one, are the caller's job (it calls `viki.actions.newSession()` and hands
 * the new id back down); this component never calls that itself. With no session yet, the body
 * shows a short empty line instead of `messages`, and both header actions stay disabled: there is
 * no chat yet to start over or open elsewhere.
 */
import type { ReactElement, ReactNode } from "react";
import { HandsFreeNotice, HandsFreeStrip, HandsFreeToggle } from "../../voice/HandsFreeControls";
import type { UseHandsFree } from "../../voice/useHandsFree";
import { Composer } from "../Composer/Composer";
import "./LauncherChat.css";

export interface LauncherChatProps {
  sessionId: string | null;
  /** The transcript to show, rendered as-is. Ignored (in favour of the empty line) while
   * `sessionId` is null. */
  messages: ReactNode;
  running: boolean;
  onSend: (text: string) => Promise<boolean>;
  onStop: () => void;
  /** Clears the current session so the next send starts a fresh chat. */
  onNewConversation: () => void;
  /** Opens this same chat in the main window. */
  onOpenInMain: () => void;
  /** VIKI's own hands-free loop, bound to this chat by the caller. Given, the composer gets
   * the voice toggle beside Send, its first-use notice and its status strip; omitted, this chat
   * offers no voice control. */
  handsFree?: UseHandsFree;
  /** The voice toggle's own keyboard shortcut (App's Alt+T), named in its tooltip and
   * `aria-keyshortcuts`. Ignored without `handsFree`. */
  voiceShortcut?: { aria: string; label: string };
}

export function LauncherChat({
  sessionId,
  messages,
  running,
  onSend,
  onStop,
  onNewConversation,
  onOpenInMain,
  handsFree,
  voiceShortcut,
}: LauncherChatProps): ReactElement {
  const hasSession = sessionId !== null;
  return (
    <div className="dloop-chat">
      <div className="dloop-chat-actions">
        <button
          type="button"
          className="btn txt sm dloop-chat-action"
          disabled={!hasSession}
          onClick={onNewConversation}
        >
          New conversation
        </button>
        <button
          type="button"
          className="btn txt sm dloop-chat-action"
          disabled={!hasSession}
          onClick={onOpenInMain}
        >
          Open in main window
        </button>
      </div>
      {/* data-scroll-host: Conversation.tsx's scrollHostOf names this the real scroll box for
          auto-follow and load-older, the way Shell's .conversation-scroll does for the main
          window — this box takes on none of that class's own CSS or Composer.tsx's sizing. */}
      <div className="dloop-chat-messages" data-scroll-host>
        {hasSession ? messages : <p className="dloop-chat-empty">Ask VIKI anything.</p>}
      </div>
      {handsFree ? <HandsFreeNotice handsFree={handsFree} /> : null}
      {handsFree ? <HandsFreeStrip handsFree={handsFree} /> : null}
      <Composer
        label="Message VIKI"
        running={running}
        onSend={onSend}
        onStop={onStop}
        {...(handsFree
          ? {
              handsFree: handsFree.bridge,
              voiceControl: (primary: boolean) => (
                <span className="hands-free-voice" data-primary={primary}>
                  <HandsFreeToggle
                    handsFree={handsFree}
                    primary={primary}
                    {...(voiceShortcut ? { shortcut: voiceShortcut } : {})}
                  />
                </span>
              ),
            }
          : {})}
      />
    </div>
  );
}
