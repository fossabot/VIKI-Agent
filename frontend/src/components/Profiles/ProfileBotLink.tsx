import { useEffect, useRef, useState } from "react";
import { type BotRow, type BotsApi, BotsApiError } from "../../clients/messaging/botsApi";
import { t } from "../../i18n/t";

export function ProfileBotLink({
  api,
  profileId,
  revision,
  workspaceId,
  disabled,
  onOpen,
}: {
  api: BotsApi;
  profileId: string;
  revision: number;
  workspaceId: string;
  disabled: boolean;
  onOpen(botId: string): void;
}) {
  const [bots, setBots] = useState<readonly BotRow[] | null>(null);
  const [error, setError] = useState(false);
  const [busy, setBusy] = useState(false);
  const [reload, setReload] = useState(0);
  const action = useRef<AbortController | null>(null);
  const command = useRef<string | null>(null);
  const key = `${profileId}:${revision}:${workspaceId}`;
  const current = useRef(key);
  current.current = key;
  // biome-ignore lint/correctness/useExhaustiveDependencies: key cancels actions on identity changes; reload is an explicit read
  useEffect(() => {
    const controller = new AbortController();
    setBots(null);
    setError(false);
    setBusy(false);
    api.list(controller.signal).then(
      (value) => {
        if (!controller.signal.aborted) setBots(value.bots);
      },
      () => {
        if (!controller.signal.aborted) setError(true);
      },
    );
    return () => {
      controller.abort();
      action.current?.abort();
      action.current = null;
      command.current = null;
    };
  }, [api, key, reload]);
  const existing = bots?.find(
    (bot) => bot.profile_id === profileId && bot.workspace_id === workspaceId,
  );
  async function open() {
    if (disabled || action.current) return;
    if (existing) {
      onOpen(existing.bot_id);
      return;
    }
    const controller = new AbortController();
    action.current = controller;
    command.current ??= crypto.randomUUID();
    setBusy(true);
    setError(false);
    try {
      let bot: BotRow;
      try {
        bot = (
          await api.create(command.current, profileId, revision, workspaceId, controller.signal)
        ).bot;
      } catch (error) {
        if (!(error instanceof BotsApiError) || error.code !== "conflict") throw error;
        const found = (await api.list(controller.signal)).bots.find(
          (row) => row.profile_id === profileId && row.workspace_id === workspaceId,
        );
        if (!found) throw error;
        bot = found;
      }
      if (!controller.signal.aborted && current.current === key) onOpen(bot.bot_id);
    } catch {
      if (!controller.signal.aborted && current.current === key) setError(true);
    } finally {
      if (action.current === controller) {
        action.current = null;
        setBusy(false);
      }
    }
  }
  return (
    <div className="profile-bot-link">
      <button
        type="button"
        className="btn alt"
        disabled={disabled || busy || !bots}
        onClick={() => void open()}
      >
        {t(existing ? "bots.profile.open" : "bots.profile.add")}
      </button>
      {error ? (
        <p role="alert">
          {t("bots.profile.failed")}{" "}
          <button
            type="button"
            className="btn txt sm"
            disabled={busy || disabled}
            onClick={() => setReload((value) => value + 1)}
          >
            {t("common.button.refresh")}
          </button>
        </p>
      ) : null}
    </div>
  );
}
