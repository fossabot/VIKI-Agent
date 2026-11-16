import { useEffect, useRef, useState } from "react";
import type { BotRow } from "../../clients/messaging/botsApi";
import type { Key } from "../../i18n";
import { t, tf } from "../../i18n/t";
import type { VaultApi } from "../../state/vaultApi";
import { Loading } from "../Loading/Loading";
import { BotMemoryDialog } from "./BotMemoryDialog";
import {
  type BotMemoryListing,
  type BotMemoryRow,
  botMemoryCard,
  listBotMemory,
} from "./botMemory";
import "./BotMemoryCard.css";

type MemoryApi = Pick<VaultApi, "entries" | "note" | "metadata">;

interface ReadyListing {
  value: BotMemoryListing;
  api: MemoryApi;
  workspaceId: string;
  botId: string;
}

export interface BotMemoryCardProps {
  api?: MemoryApi;
  workspaceId: string;
  bot: BotRow;
  /** The Bot's vault is the active workspace; without an api the memory cannot be read now. */
  vaultActive?: boolean;
  /** Where the memory lives: the vault's `bot-memory/` (default) or the app's own data folder. */
  kept?: "vault" | "app";
  disabled: boolean;
  onOpenNote(ref: string): void;
  onOpenUrl(url: string): void;
  onReview?(): Promise<void>;
  onForget?(): Promise<void>;
  onAskInMainChat?(bot: BotRow): Promise<void>;
  /** Passed to the viewer, which shows **Open in VS Code** only with it. */
  onOpenInEditor?(ref: string): void;
  /** Passed to the viewer, which shows **Edit** only with it. */
  onDraftReplacement?(request: string): Promise<boolean>;
}

const DETAIL_KEYS: Record<Exclude<BotMemoryRow["details"], null>, Key> = {
  absent: "bots.memory.metadata.absent",
  unsupported: "bots.memory.metadata.unsupported",
  truncated: "bots.memory.metadata.truncated",
  changed: "bots.memory.metadata.changed",
  unreadable: "bots.memory.metadata.unreadable",
  unknown: "bots.memory.metadata.unknown",
  notRead: "bots.memory.metadata.notRead",
};

export function BotMemoryCard({
  api,
  workspaceId,
  bot,
  vaultActive = false,
  kept = "vault",
  disabled,
  onOpenNote,
  onOpenUrl,
  onReview,
  onForget,
  onAskInMainChat,
  onOpenInEditor,
  onDraftReplacement,
}: BotMemoryCardProps) {
  const [listing, setListing] = useState<ReadyListing | null>(null);
  const [readState, setReadState] = useState<"loading" | "error">("loading");
  const [attempt, setAttempt] = useState(0);
  const [dialog, setDialog] = useState(false);
  const [help, setHelp] = useState(false);
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  const current = useRef({ api, workspaceId, botId: bot.bot_id });
  current.current = { api, workspaceId, botId: bot.bot_id };
  const dialogScope = useRef({ api, workspaceId, botId: bot.bot_id });
  const ready =
    listing !== null &&
    listing.api === api &&
    listing.workspaceId === workspaceId &&
    listing.botId === bot.bot_id
      ? listing.value
      : null;

  useEffect(() => {
    const changed =
      dialogScope.current.api !== api ||
      dialogScope.current.workspaceId !== workspaceId ||
      dialogScope.current.botId !== bot.bot_id;
    dialogScope.current = { api, workspaceId, botId: bot.bot_id };
    if (changed) setDialog(false);
  }, [api, workspaceId, bot.bot_id]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: attempt is an explicit retry request
  useEffect(() => {
    if (!api) {
      setListing(null);
      return;
    }
    const controller = new AbortController();
    const isCurrent = () =>
      current.current.api === api &&
      current.current.workspaceId === workspaceId &&
      current.current.botId === bot.bot_id;
    setReadState("loading");
    void listBotMemory(api, workspaceId, bot.bot_id, controller.signal, isCurrent, kept).then(
      (value) => {
        if (controller.signal.aborted || !isCurrent()) return;
        setListing({ value, api, workspaceId, botId: bot.bot_id });
        setReadState("loading");
      },
      () => {
        if (!controller.signal.aborted && isCurrent()) setReadState("error");
      },
    );
    return () => controller.abort();
  }, [api, kept, workspaceId, bot.bot_id, attempt]);

  async function run(action: () => Promise<void>): Promise<void> {
    if (disabled || busy || lock.current) return;
    lock.current = true;
    setBusy(true);
    try {
      await action();
    } catch {
      // The owning Bot action reports its own preparation or draft error.
    } finally {
      lock.current = false;
      setBusy(false);
    }
  }

  const name = bot.profile?.identity?.name ?? "Bot";
  const sourceRef = `bot-memory/${bot.bot_id}`;
  const preview = ready ? botMemoryCard(ready) : null;
  return (
    <section className="bot-memory-card" aria-labelledby={`bot-memory-${bot.bot_id}`}>
      <header>
        <h3 id={`bot-memory-${bot.bot_id}`}>{t("bots.memory")}</h3>
        {kept === "app" ? (
          <small>{t("bots.memory.keptApp")}</small>
        ) : (
          <code>{tf("bots.memory.source", { ref: sourceRef })}</code>
        )}
      </header>
      <p>{tf("bots.memory.own", { name })}</p>
      <button
        type="button"
        className="btn txt sm"
        aria-expanded={help}
        onClick={() => setHelp((value) => !value)}
      >
        {t("bots.memory.how")}
      </button>
      {help ? <p>{t("bots.memory.help")}</p> : null}
      {api ? (
        <>
          {ready && preview ? (
            <>
              {preview.rows.length === 0 && !ready.partial && ready.omitted === 0 ? (
                <p>{tf("bots.memory.empty", { name })}</p>
              ) : null}
              <ul className="bot-memory-card-list" aria-label={t("bots.memory.allFiles")}>
                {preview.rows.map((row) => (
                  <li key={row.ref}>
                    <span>{row.title}</span>
                    <code>{row.ref}</code>
                    {row.details ? <small>{t(DETAIL_KEYS[row.details])}</small> : null}
                  </li>
                ))}
              </ul>
              {preview.remaining > 0 ? (
                <p>{tf("bots.memory.remaining", { count: preview.remaining })}</p>
              ) : null}
              <button type="button" className="btn alt sm" onClick={() => setDialog(true)}>
                {t("bots.memory.view")}
              </button>
            </>
          ) : readState === "error" ? (
            <>
              <p role="alert">{t("bots.memory.error")}</p>
              <button
                type="button"
                className="btn alt sm"
                onClick={() => setAttempt((value) => value + 1)}
              >
                {t("bots.memory.retry")}
              </button>
            </>
          ) : (
            <Loading>{t("bots.memory.loading")}</Loading>
          )}
          {ready && (ready.partial || ready.omitted > 0) ? (
            <p className="bots-note" role="status">
              {t("bots.memory.partial")}
              {ready.omitted > 0 ? ` ${tf("bots.memory.omitted", { count: ready.omitted })}` : ""}
            </p>
          ) : null}
          {ready?.metadataPartial ? (
            <p className="bots-note" role="status">
              {t("bots.memory.metadata.unknown")}
            </p>
          ) : null}
          {onAskInMainChat ? (
            <>
              <button
                type="button"
                className="btn alt bot-memory-ask"
                aria-describedby={`bot-memory-ask-hint-${bot.bot_id}`}
                disabled={disabled || busy}
                onClick={() => void run(() => onAskInMainChat(bot))}
              >
                {tf("bots.memory.askChange", { name })}
              </button>
              <p className="bots-note" id={`bot-memory-ask-hint-${bot.bot_id}`}>
                {t("bots.memory.askChangeHint")}
              </p>
            </>
          ) : null}
          {onReview ? (
            <button
              type="button"
              className="btn alt"
              disabled={disabled || busy}
              onClick={() => void run(onReview)}
            >
              {t("bots.memory.review")}
            </button>
          ) : null}
          {onForget ? (
            <button
              type="button"
              className="btn alt"
              disabled={disabled || busy}
              onClick={() => void run(onForget)}
            >
              {t("bots.memory.forget")}
            </button>
          ) : null}
          <p className="bots-note">{t("bots.memory.requests")}</p>
          {dialog && ready ? (
            <BotMemoryDialog
              api={api}
              workspaceId={workspaceId}
              bot={{ bot_id: bot.bot_id, name }}
              kept={kept}
              onClose={() => setDialog(false)}
              {...(kept === "vault" ? { onOpenInVault: onOpenNote } : {})}
              onOpenUrl={onOpenUrl}
              {...(onOpenInEditor ? { onOpenInEditor } : {})}
              {...(onDraftReplacement ? { onDraftReplacement } : {})}
            />
          ) : null}
        </>
      ) : (
        <p className="bots-note">
          {t(vaultActive ? "bots.memory.unavailable" : "bots.memory.openVault")}
        </p>
      )}
    </section>
  );
}
