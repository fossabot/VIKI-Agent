import { type ReactNode, useEffect, useRef, useState } from "react";
import {
  type BotDetail,
  type BotRow,
  type BotsApi,
  BotsApiError,
} from "../../clients/messaging/botsApi";
import { t, tf } from "../../i18n/t";
import type { VaultApi } from "../../state/vaultApi";
import { Loading } from "../Loading/Loading";
import { BotMemoryDialog } from "./BotMemoryDialog";
import { reasonText } from "./botReasons";
import "./BotsView.css";
import "./BotChatActivity.css";

export function botErrorText(error: unknown): string {
  if (!(error instanceof BotsApiError)) return "Bots are unavailable. Refresh Bots to try again.";
  switch (error.code) {
    case "conflict":
    case "command_conflict":
    case "profile_changed":
      return "This Bot changed. Refresh Bots before trying again.";
    case "busy":
      return "This conversation is busy. Wait or stop its current turn before trying again.";
    case "workspace_restart_required":
      return "Restart VIKI Agent to use this Bot's workspace.";
    case "transcript_missing":
      return "The saved conversation source is unavailable. Start a new conversation to continue.";
    case "profile_archived":
      return "This Bot's profile is archived. Restore it in Bots → Bot settings to continue.";
    case "profile_missing":
      return "This Bot's profile is unavailable. Refresh Bots or choose another profile.";
    case "delivery_unavailable":
      return "Mattermost delivery is unavailable. Refresh Bots and check Messaging.";
    case "capacity":
      return "The Bot limit was reached. Remove a Bot before creating another.";
    case "intake_timeout":
      return "The request timed out. Refresh Bots before trying again.";
    default:
      return "Bots are unavailable. Refresh Bots to try again.";
  }
}

export function BotConversationHelp() {
  return (
    <details className="bots-context-help">
      <summary>Conversation history and context</summary>
      <p>
        Saved history can exceed the model's active context. Claude manages compaction when enabled
        as context fills and may lose earlier details.
      </p>
      <p>
        Profile instructions and VIKI Agent's memory rule and Bot rules go to Claude Code as its
        system prompt each time the chat starts, so compaction does not drop them. Memory is
        separate from history and uses context when loaded. In every vault each Bot has its own
        memory folder: under bot-memory/ in an ordinary vault, otherwise in VIKI Agent's data folder
        on this computer.
      </p>
      <p>New conversation starts a fresh chat and keeps the previous conversation.</p>
    </details>
  );
}

const LABELS: Record<string, string> = {
  succeeded: "Succeeded",
  failed: "Failed",
  cancelled: "Cancelled",
  expired: "Expired",
  blocked: "Blocked",
  unknown: "Unknown",
  held: "Held",
  off: "Off",
  unavailable: "Unavailable",
  sending: "Sending",
  sent: "Sent",
};
const DELIVERY_REASONS: Record<string, string> = {
  none: "",
  off: "Delivery is off.",
  held: "Held by Decision's rules.",
  not_connected: "Mattermost is not connected.",
  workspace_mismatch: "This Bot uses a different workspace from the Messaging bridge.",
  stale_bot: "The Bot changed before delivery.",
  stale_consent: "Delivery consent changed before dispatch.",
  policy_changed: "The attention policy changed before dispatch.",
  app_closing: "VIKI Agent is closing.",
  send_failed: "Mattermost delivery failed.",
  send_unknown: "Mattermost delivery could not be confirmed.",
  unsupported_outcome: "This outcome is not sent to Mattermost.",
  capacity: "The delivery queue was full.",
};
export function BotDeliverySummary({
  bot,
  onOpenSession,
}: {
  bot: BotRow;
  onOpenSession(id: string): void;
}) {
  const receipt = bot.latest_delivery;
  return (
    <section className="bots-delivery-summary" aria-label="Latest routine result">
      {receipt ? (
        <>
          <p>{`Routine outcome: ${LABELS[receipt.outcome]}`}</p>
          <p>{`Mattermost delivery: ${LABELS[receipt.state]}`}</p>
          <p>{reasonText(receipt.reason)}</p>
          {DELIVERY_REASONS[receipt.delivery_reason] ? (
            <p>{DELIVERY_REASONS[receipt.delivery_reason]}</p>
          ) : null}
          <button
            type="button"
            className="btn alt"
            onClick={() => onOpenSession(receipt.session_id)}
          >
            Open latest run chat
          </button>
        </>
      ) : (
        <p>No routine delivery result yet.</p>
      )}
      <p className="bots-note">Bot notice decisions are separate from desktop brief statistics.</p>
    </section>
  );
}

export interface BotChatActivityProps {
  api: BotsApi;
  botId: string;
  sessionId: string;
  main: boolean;
  onOpenSession(sessionId: string): void;
  routines?: ReactNode;
  refreshKey?: string;
  onRefresh?(): void;
  memory?: {
    api: Pick<VaultApi, "entries" | "note" | "metadata">;
    sessionId: string;
    botId: string;
    workspaceId: string;
    activeWorkspaceId: string;
    workspacePolicy: string;
    onOpenInVault?(ref: string): void;
    onOpenUrl(url: string): void;
    onOpenInEditor?(ref: string): void;
    onDraftReplacement?(request: string): Promise<boolean>;
  };
}
type BotMemoryAccess = NonNullable<BotChatActivityProps["memory"]>;

export function BotChatActivity({
  api,
  botId,
  sessionId,
  main,
  onOpenSession,
  routines,
  refreshKey,
  onRefresh,
  memory,
}: BotChatActivityProps) {
  const [loaded, setLoaded] = useState<{ key: string; detail: BotDetail } | null>(null);
  const [error, setError] = useState<{ key: string; text: string } | null>(null);
  const [refresh, setRefresh] = useState(0);
  const key = `${botId}:${sessionId}`;
  const current = useRef(key);
  current.current = key;
  // biome-ignore lint/correctness/useExhaustiveDependencies: refresh and refreshKey force an ownership/status read
  useEffect(() => {
    const controller = new AbortController();
    setError(null);
    api.get(botId, controller.signal).then(
      (detail) => {
        if (controller.signal.aborted || current.current !== key) return;
        if (detail.bot.bot_id !== botId) {
          setError({ key, text: "Bot activity is unavailable. Refresh to try again." });
          return;
        }
        setLoaded((previous) => {
          if (
            previous?.key === key &&
            previous.detail.bot.state_revision > detail.bot.state_revision
          )
            return previous;
          return { key, detail };
        });
      },
      (reason) => {
        if (!controller.signal.aborted && current.current === key)
          setError({ key, text: botErrorText(reason) });
      },
    );
    return () => controller.abort();
  }, [api, botId, key, refresh, refreshKey]);
  const detail = loaded?.key === key ? loaded.detail : null;
  function reload() {
    onRefresh?.();
    setRefresh((n) => n + 1);
  }
  const receipt = detail?.bot.latest_delivery;
  const canViewMemory = Boolean(
    memory &&
      memory.sessionId === sessionId &&
      memory.botId === botId &&
      memory.workspaceId === memory.activeWorkspaceId &&
      detail?.bot.bot_id === botId &&
      detail.bot.workspace_id === memory.workspaceId &&
      detail.bot.workspace?.policy === memory.workspacePolicy &&
      (!main || detail.bot.chat_session_id === sessionId),
  );
  const kept = memory?.workspacePolicy === "ordinary" ? "vault" : "app";
  const memoryContext = JSON.stringify([
    sessionId,
    botId,
    memory?.sessionId,
    memory?.botId,
    memory?.workspaceId,
    memory?.activeWorkspaceId,
    memory?.workspacePolicy,
  ]);
  const [memoryOpen, setMemoryOpen] = useState<{
    context: string;
    api: BotMemoryAccess["api"];
  } | null>(null);
  useEffect(() => {
    setMemoryOpen((open) =>
      open && (open.context !== memoryContext || open.api !== memory?.api || !canViewMemory)
        ? null
        : open,
    );
  }, [canViewMemory, memory?.api, memoryContext]);
  const isMemoryOpen =
    canViewMemory && memoryOpen?.context === memoryContext && memoryOpen.api === memory?.api;
  return (
    <>
      <section className="bot-chat-activity" aria-label="Bot activity">
        <div className="bot-chat-activity-head">
          <h2 className="lbl">Bot activity</h2>
          <div className="bot-chat-activity-actions">
            {canViewMemory ? (
              <button
                type="button"
                className="btn txt sm"
                onClick={() => memory && setMemoryOpen({ context: memoryContext, api: memory.api })}
              >
                {t("bots.memory.view")}
              </button>
            ) : null}
            <button type="button" className="btn txt sm" onClick={reload}>
              Refresh activity
            </button>
          </div>
        </div>
        {canViewMemory ? (
          <p className="bot-chat-memory-source">
            {kept === "vault"
              ? tf("bots.memory.source", { ref: `bot-memory/${botId}` })
              : t("bots.memory.keptApp")}
          </p>
        ) : null}
        {error?.key === key ? <p role="alert">{error.text}</p> : null}
        {!detail && error?.key !== key ? <Loading>Loading Bot activity…</Loading> : null}
        {main &&
        detail?.bot.chat_session_id !== undefined &&
        detail.bot.chat_session_id !== sessionId ? (
          <p>This Bot now uses a different conversation.</p>
        ) : null}
        {routines}
        {receipt ? (
          <section className="bot-chat-activity-run" aria-label="Latest routine result">
            <p>{`Routine outcome: ${LABELS[receipt.outcome]}`}</p>
            <button
              type="button"
              className="btn txt sm"
              onClick={() => onOpenSession(receipt.session_id)}
            >
              Open latest run chat
            </button>
            {receipt.state === "off" ? null : (
              <p>{`Mattermost delivery: ${LABELS[receipt.state]}`}</p>
            )}
          </section>
        ) : null}
      </section>
      {isMemoryOpen && memory ? (
        <BotMemoryDialog
          api={memory.api}
          workspaceId={memory.workspaceId}
          bot={{ bot_id: botId, name: detail?.bot.profile?.identity.name ?? "Bot" }}
          onClose={() => setMemoryOpen(null)}
          kept={kept}
          {...(kept === "vault" && memory.onOpenInVault
            ? { onOpenInVault: memory.onOpenInVault }
            : {})}
          onOpenUrl={memory.onOpenUrl}
          {...(memory.onOpenInEditor ? { onOpenInEditor: memory.onOpenInEditor } : {})}
          {...(memory.onDraftReplacement ? { onDraftReplacement: memory.onDraftReplacement } : {})}
        />
      ) : null}
    </>
  );
}
