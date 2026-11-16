/**
 * BetweenChats: the right rail's closed-by-default fold listing the newest messages your recent
 * chats received from other Claude Code sessions (src/viki_agent/conversations/between_chats.py,
 * GET /api/v2/between-chats). No request while the fold is closed; opening it reads at once and
 * again every `BETWEEN_CHATS_POLL_MS`, closing or unmounting aborts the in-flight read and stops
 * the loop (the same `stopped` + `AbortController` + chained `setTimeout` technique RunPeek.tsx
 * uses for its own polling).
 *
 * Untrusted text: `from_name` and `text` come from another process's own transcript. Both render
 * as plain text nodes only — no Markdown, link detection or raw HTML — so nothing a message
 * contains can inject markup or navigate the page.
 */
import { type ReactElement, useEffect, useState } from "react";
import type { BetweenChatsApi, ChatMessage } from "../../clients/conversations/betweenChatsApi";
import { whenLabel } from "../../state/routines";
import { Loading } from "../Loading/Loading";
import "./BetweenChats.css";

export const BETWEEN_CHATS_POLL_MS = 30_000;

type Status = "loading" | "ready" | "unavailable";

export interface BetweenChatsProps {
  api: BetweenChatsApi;
  /** A session id's chat label, the same lookup the caller gives every other rail section. */
  labelOf: (sessionId: string) => string;
  /** The receiving chat's session id, from a selected row. */
  onOpen: (sessionId: string) => void;
  nowMs: number;
}

function baseRowKey(message: ChatMessage): string {
  return `${message.to_session_id}:${message.at}:${message.from_session_id ?? message.from_name}:${message.text}`;
}

/** One key per message, stable and unique: a bare index would move keyboard focus onto a
 * different message when a poll prepends a row. Two messages whose base key matches (every
 * field, including text, equal) get an occurrence suffix (`#2`, `#3`, …) past the first. */
function rowKeys(messages: readonly ChatMessage[]): string[] {
  const seen = new Map<string, number>();
  return messages.map((message) => {
    const base = baseRowKey(message);
    const occurrence = (seen.get(base) ?? 0) + 1;
    seen.set(base, occurrence);
    return occurrence === 1 ? base : `${base}#${occurrence}`;
  });
}

export function BetweenChats({ api, labelOf, onOpen, nowMs }: BetweenChatsProps): ReactElement {
  const [open, setOpen] = useState(false);
  const [status, setStatus] = useState<Status>("loading");
  const [messages, setMessages] = useState<readonly ChatMessage[]>([]);
  const [partial, setPartial] = useState(false);

  useEffect(() => {
    if (!open) return;
    let stopped = false;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;

    function run(): void {
      if (stopped) return;
      api.list(controller.signal).then(
        (result) => {
          if (stopped) return;
          setStatus("ready");
          setMessages(result.messages);
          setPartial(result.partial);
          timer = setTimeout(run, BETWEEN_CHATS_POLL_MS);
        },
        () => {
          if (stopped) return;
          setStatus("unavailable");
          timer = setTimeout(run, BETWEEN_CHATS_POLL_MS);
        },
      );
    }

    setStatus("loading");
    run();

    return () => {
      stopped = true;
      controller.abort();
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [api, open]);

  function renderBody(): ReactElement | null {
    if (status === "loading")
      return (
        <Loading announce={false} className="between-chats-note">
          Loading…
        </Loading>
      );
    if (status === "unavailable") {
      return <p className="between-chats-note">Between chats is unavailable.</p>;
    }
    if (messages.length === 0) {
      return <p className="between-chats-note">No messages between chats.</p>;
    }
    const keys = rowKeys(messages);
    return (
      <>
        <ul className="between-chats-list">
          {messages.map((message, index) => {
            const from = message.from_session_id
              ? labelOf(message.from_session_id)
              : message.from_name;
            return (
              <li key={keys[index]}>
                <button
                  type="button"
                  className="between-chats-row"
                  onClick={() => onOpen(message.to_session_id)}
                >
                  <span className="between-chats-line">
                    {`${from} → ${labelOf(message.to_session_id)} · `}
                    <time dateTime={message.at}>{whenLabel(message.at, nowMs)}</time>
                  </span>
                  <span className="between-chats-line">{message.text}</span>
                </button>
              </li>
            );
          })}
        </ul>
        {partial ? <p className="between-chats-note">Some chats could not be read.</p> : null}
      </>
    );
  }

  return (
    <details className="between-chats" onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary className="lbl">Between chats</summary>
      <p className="between-chats-caption">
        Messages your recent chats received from other Claude Code sessions, newest first.
      </p>
      {renderBody()}
    </details>
  );
}
