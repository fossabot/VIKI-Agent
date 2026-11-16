/** The one code folder a Bot may work in a copy of, chosen in Bot settings. The row is read from
 * the Bots API for this profile and workspace; every answer is dropped once either changes. */
import { type ReactElement, useEffect, useId, useRef, useState } from "react";
import type { WorkspaceJson } from "../../clients/app/shellApi";
import {
  type BotRow,
  type BotsApi,
  BotsApiError,
  type BotsErrorCode,
} from "../../clients/messaging/botsApi";
import type { Key } from "../../i18n";
import { t } from "../../i18n/t";
import { ConfirmDialog } from "../Dialog/ConfirmDialog";
import "./BotCodeFolder.css";

const REFUSALS: Partial<Record<BotsErrorCode, Key>> = {
  git_settings_changed: "bots.code_folder.git_settings_changed",
  code_folder_needs_ordinary: "bots.code_folder.needs_ordinary",
  code_folder_untrusted: "bots.code_folder.untrusted",
  code_folder_missing: "bots.code_folder.missing",
  code_folder_not_repository: "bots.code_folder.not_repository",
  code_folder_no_commits: "bots.code_folder.no_commits",
  code_folder_git_unavailable: "bots.code_folder.git_unavailable",
  conflict: "bots.code_folder.conflict",
};

export function BotCodeFolder({
  api,
  profileId,
  workspaceId,
  folders,
  disabled,
}: {
  api: BotsApi;
  profileId: string;
  workspaceId: string;
  folders: readonly WorkspaceJson[];
  disabled: boolean;
}): ReactElement | null {
  const id = useId();
  const scope = `${profileId}:${workspaceId}`;
  const [owner, setOwner] = useState({ api, scope });
  const [bot, setBot] = useState<BotRow | null>(null);
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<Key | null>(null);
  // The folder a change waits on for confirmation; undefined when no dialog is open.
  const [asking, setAsking] = useState<string | null | undefined>(undefined);
  const [reload, setReload] = useState(0);
  const action = useRef<AbortController | null>(null);

  if (owner.api !== api || owner.scope !== scope) {
    action.current?.abort();
    action.current = null;
    setOwner({ api, scope });
    setBot(null);
    setBusy(false);
    setOutcome(null);
    setAsking(undefined);
  }
  useEffect(
    () => () => {
      action.current?.abort();
    },
    [],
  );

  // biome-ignore lint/correctness/useExhaustiveDependencies: reload is an explicit read request
  useEffect(() => {
    if (!api.setCodeFolder) return;
    const controller = new AbortController();
    api.list(controller.signal).then(
      (list) => {
        if (controller.signal.aborted) return;
        setBot(
          list.bots.find(
            (row) => row.profile_id === profileId && row.workspace_id === workspaceId,
          ) ?? null,
        );
      },
      () => {
        // Without a Bot row there is nothing to offer; Add as Bot reports the same failed read.
      },
    );
    return () => controller.abort();
  }, [api, profileId, workspaceId, reload]);

  if (!bot) return null;
  const granted = bot.code_folder ?? null;
  const offered = folders.filter(
    (folder) => folder.kind === "code" && folder.persistent && folder.policy === "ordinary",
  );

  function send(target: string | null): void {
    if (!bot || busy || action.current || !api.setCodeFolder) return;
    const controller = new AbortController();
    action.current = controller;
    setBusy(true);
    setOutcome(null);
    const finish = (next: Key | null): void => {
      if (controller.signal.aborted) return;
      action.current = null;
      setBusy(false);
      setOutcome(next);
    };
    api.setCodeFolder(bot.bot_id, bot.state_revision, target, controller.signal).then(
      (answer) => {
        if (controller.signal.aborted) return;
        setBot(answer.bot);
        finish(null);
      },
      (error: unknown) => {
        if (controller.signal.aborted) return;
        const code = error instanceof BotsApiError ? error.code : null;
        finish((code && REFUSALS[code]) || "bots.code_folder.failed");
        // A stale revision is the only refusal a fresh read can fix.
        if (code === "conflict") setReload((value) => value + 1);
      },
    );
  }

  function choose(value: string): void {
    const target = value === "" ? null : value;
    if (busy || target === (granted?.workspace_id ?? null)) return;
    if (granted) setAsking(target);
    else send(target);
  }

  const hintId = `${id}-hint`;
  const permissionsId = `${id}-permissions`;
  // A grant whose folder is no longer offered stays the selected choice until it is changed.
  const unlisted =
    granted && !offered.some((folder) => folder.workspace_id === granted.workspace_id)
      ? granted
      : null;
  return (
    <>
      <details className="bot-code-folder">
        <summary>
          {t("bots.code_folder.title")}
          <span className="bot-code-folder-value">
            {granted?.name ?? t("bots.code_folder.none")}
          </span>
        </summary>
        <label className="bot-code-folder-field">
          {t("bots.code_folder.title")}
          <select
            value={granted?.workspace_id ?? ""}
            disabled={busy || disabled}
            aria-busy={busy}
            aria-describedby={`${hintId} ${permissionsId}`}
            onChange={(event) => choose(event.target.value)}
          >
            <option value="">{t("bots.code_folder.none")}</option>
            {unlisted ? <option value={unlisted.workspace_id}>{unlisted.name}</option> : null}
            {offered.map((folder) => (
              <option key={folder.workspace_id} value={folder.workspace_id}>
                {folder.name}
              </option>
            ))}
          </select>
        </label>
        <p id={hintId} className="bot-code-folder-note">
          {t("bots.code_folder.hint")}
        </p>
        <p id={permissionsId} className="bot-code-folder-note">
          {t("bots.code_folder.permissions")}
        </p>
        {outcome ? <p role="alert">{t(outcome)}</p> : null}
      </details>
      {asking !== undefined ? (
        <ConfirmDialog
          label={t("bots.code_folder.change_title")}
          body={t("bots.code_folder.change_body")}
          confirmLabel={t("common.button.apply")}
          onConfirm={() => {
            setAsking(undefined);
            send(asking);
          }}
          onCancel={() => setAsking(undefined)}
        />
      ) : null}
    </>
  );
}
