import { useEffect, useState } from "react";
import type { BotRow } from "../../clients/messaging/botsApi";
import { t } from "../../i18n/t";
import { useIdentity } from "../Avatar/identity";
import type { BotChatActivityProps } from "./BotChatActivity";
import { BotFace } from "./BotFace";
import { BotMemoryDialog } from "./BotMemoryDialog";
import { botName } from "./team";
import "./BotRoster.css";
import "./BotsView.css";

export function BotChatBar({
  bot,
  main,
  role,
  memory,
  onManage,
}: {
  bot: BotRow;
  main: boolean;
  role?: string;
  memory?: BotChatActivityProps["memory"];
  onManage(): void;
}) {
  const { profile, unresolved } = useIdentity();
  const revision = !unresolved && profile ? profile.revision : null;
  const canViewMemory =
    memory?.botId === bot.bot_id &&
    memory.workspaceId === memory.activeWorkspaceId &&
    bot.workspace?.policy === memory.workspacePolicy;
  const kept = memory?.workspacePolicy === "ordinary" ? "vault" : "app";
  // Open only for the Bot, chat and workspace it was opened for, as `BotChatActivity` does.
  const memoryContext = JSON.stringify([
    bot.bot_id,
    memory?.sessionId,
    memory?.botId,
    memory?.workspaceId,
    memory?.activeWorkspaceId,
    memory?.workspacePolicy,
  ]);
  const [memoryOpen, setMemoryOpen] = useState<{
    context: string;
    api: NonNullable<BotChatActivityProps["memory"]>["api"];
  } | null>(null);
  useEffect(() => {
    setMemoryOpen((open) =>
      open && (open.context !== memoryContext || open.api !== memory?.api) ? null : open,
    );
  }, [memory?.api, memoryContext]);
  const isMemoryOpen =
    canViewMemory && memoryOpen?.context === memoryContext && memoryOpen.api === memory?.api;
  return (
    <>
      <nav className="bot-chat-bar" aria-label={t("bots.chat")}>
        <BotFace
          profileId={bot.profile_id}
          identity={bot.profile?.identity ?? null}
          name={botName(bot)}
          size={28}
          decorative
        />
        <span className="bot-chat-label">
          <span className="bot-chat-name">
            <strong>{botName(bot)}</strong>
            {role ? <span className="bots-role">{role}</span> : null}
          </span>
          <span className="bot-chat-meta">
            <span>{t(main ? "bots.chat.main" : "bots.thread")}</span>
            {revision !== null ? ` · r${revision}` : null}
          </span>
        </span>
        <button type="button" className="btn txt sm" onClick={onManage}>
          {t("bots.chat.manage")}
        </button>
        {canViewMemory ? (
          <button
            type="button"
            className="btn txt sm"
            onClick={() => memory && setMemoryOpen({ context: memoryContext, api: memory.api })}
          >
            {t("bots.chat.memory")}
          </button>
        ) : null}
      </nav>
      {memory && isMemoryOpen ? (
        <BotMemoryDialog
          api={memory.api}
          workspaceId={memory.workspaceId}
          bot={{ bot_id: bot.bot_id, name: botName(bot) }}
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
