/**
 * A settings page built from the document: General, Chat and Privacy. Each row
 * saves on its own — one `patch({[key]: value})` per change, no Apply button — because every key
 * here is independent and a page-wide Save would make a stale revision cost the user four edits
 * instead of one. Appearance is the deliberate exception (its controls preview live,
 * so they need Apply/Cancel).
 *
 * Where each refusal lands, and why:
 * - `400 invalid_settings` → the row that carries the key, through its `field.<code>` row. The
 *   backend answers per key, so anything page-wide would hide which control to fix.
 * - `409 read_only` → the same row, with the read-only sentence. Nothing is committed; the
 *   notice above the page already explains the state.
 * - `409 stale_revision` / `409 command_conflict` → handed up to `SettingsView`, which owns the
 *   review panel: the panel needs the user's typed values, which only the page knows, and the
 *   document, which only the view may adopt.
 * - `413 settings_too_large` → a page-level error, with `extra.current_bytes` read by key (never
 *   spread: a member of `extra` can be a path the backend chose).
 * - Anything else → the row, through `errorText`. The server's own `message` is never rendered.
 *
 * A row's "Saved" stays until that row changes again. A timer that cleared it would be
 * one more thing to fake in every test, and a status that disappears on its own is a status the
 * user can miss.
 */

import { type ReactElement, useContext, useRef, useState } from "react";
import { ModelConnectionsContext } from "../../clients/app/modelConnectionsApi";
import {
  type SettingsPatchResult,
  type SettingsView,
  ShellApiError,
} from "../../clients/app/shellApi";
import type { SettingsPage } from "../../commands";
import { en, type Key } from "../../i18n/index";
import { errorText, t, tf } from "../../i18n/t";
import {
  DictationSettingsPanel,
  useDictationStatus,
  voiceOptions,
} from "../../voice/DictationSettingsPanel";
import { DictationContext } from "../../voice/dictationContext";
import { speechEngine } from "../../voice/voicePackApi";
import { AvatarField } from "../Avatar/AvatarField";
import { Check, Spinner } from "../Loading/Loading";
import { ModelDefaultField } from "./ModelDefaultField";
import { PackVoicesField } from "./PackVoicesField";
import { keyDesc, keyLabel, keysOnPage, pageDescription, sectionsOf, tabLabel } from "./pages";
import { ReadAloudVoicesField } from "./ReadAloudVoicesField";
import { type RowStatus, SettingRow } from "./SettingRow";
import { VoiceboxField } from "./VoiceboxField";
import { VoiceEngineField } from "./VoiceEngineField";

/** The two avatar keys, each mapped to the identity key that names the picture: the display name
 * for the user's own avatar, the assistant name for the assistant's. */
const AVATAR_NAME_KEY: Readonly<Record<string, string>> = {
  "identity.user_avatar_id": "identity.display_name",
  "identity.assistant_avatar_id": "identity.assistant_name",
};

export interface DocumentPageProps {
  page: SettingsPage;
  settings: SettingsView;
  /** The `<h2>`'s id, so the `<section>` can name itself with it. */
  headingId: string;
  /** The read-only notice's id when the document is read-only, else null. */
  readOnlyNoticeId: string | null;
  patch: (changes: Record<string, unknown>) => Promise<SettingsPatchResult>;
  /** A 409 the review panel owns, with the changes that caused it. */
  onConflict: (error: ShellApiError, changes: Record<string, unknown>) => void;
}

/** The keys whose choices depend on what is installed; the `voice.read_aloud_*` keys are not. */
const DICTATION_KEYS: ReadonlySet<string> = new Set([
  "voice.dictation_language",
  "voice.model",
  "voice.backend",
]);

/** The backend's per-key codes all have a `field.<code>` row; a code with no such row
 * falls back to its `error.<code>` row, and then to the generic sentence. */
function fieldText(code: string): string {
  const field = `field.${code}`;
  if (field in en) return t(field as Key);
  return errorText(new ShellApiError(400, code));
}

export function DocumentPage({
  page,
  settings,
  headingId,
  readOnlyNoticeId,
  patch,
  onConflict,
}: DocumentPageProps): ReactElement {
  const [status, setStatus] = useState<Readonly<Record<string, RowStatus>>>({});
  const [errors, setErrors] = useState<Readonly<Record<string, string>>>({});
  const [pageError, setPageError] = useState<string | null>(null);
  const { known, other } = keysOnPage(page, settings.keys);
  const dictation = useContext(DictationContext);
  const modelConnections = useContext(ModelConnectionsContext);
  const voice = useDictationStatus(dictation, page === "chat");
  /** The tail of the save chain, so the next PATCH reads the revision the previous one committed. */
  const queue = useRef<Promise<void>>(Promise.resolve());
  /** The value each key has a PATCH queued or in flight for. */
  const dispatched = useRef(new Map<string, unknown>());

  /**
   * One gesture can run two save handlers before the first request answers: a reset button's
   * mousedown blurs the text field, and the focus fixup that follows a modal's `inert` does the
   * same. `store.patch` reads the revision at call time, so two concurrent patches carry the same
   * expected revision and the server refuses the second as `stale_revision` — one of the user's
   * two actions lost behind a "changed in another window" dialog they had no part in causing.
   * Chaining is enough: the second request is built after the first has committed its revision.
   * A repeat of the value a key already has queued is dropped outright (the blur that follows an
   * Enter-save is the same save, not a second one).
   */
  function enqueue(key: string, value: unknown): void {
    const pending = dispatched.current;
    if (pending.has(key) && Object.is(pending.get(key), value)) return;
    pending.set(key, value);
    queue.current = queue.current.then(async () => {
      await save(key, value);
      // Only the newest queued value for this key clears the mark; an older one settling later
      // must not reopen the door for the value still waiting.
      if (Object.is(pending.get(key), value)) pending.delete(key);
    });
  }

  async function save(key: string, value: unknown): Promise<void> {
    const changes = { [key]: value };
    setStatus((current) => ({ ...current, [key]: "saving" }));
    setErrors((current) => ({ ...current, [key]: "" }));
    setPageError(null);
    try {
      await patch(changes);
      setStatus((current) => ({ ...current, [key]: "saved" }));
      // A model or backend choice changes what the engine would do, so the status is read again.
      if (DICTATION_KEYS.has(key)) voice.refresh();
    } catch (error) {
      setStatus((current) => ({ ...current, [key]: null }));
      if (!(error instanceof ShellApiError)) {
        setErrors((current) => ({ ...current, [key]: t("error.unavailable") }));
        return;
      }
      if (error.code === "stale_revision" || error.code === "command_conflict") {
        onConflict(error, changes);
        return;
      }
      if (error.code === "invalid_settings") {
        const code = error.fields[key];
        setErrors((current) => ({
          ...current,
          [key]: code === undefined ? errorText(error) : fieldText(code),
        }));
        return;
      }
      if (error.code === "settings_too_large") {
        setPageError(tf("error.settings_too_large", { current_bytes: error.extra.current_bytes }));
        return;
      }
      setErrors((current) => ({ ...current, [key]: errorText(error) }));
    }
  }

  /** A key's own row, with the two avatar keys rendering an `AvatarField` instead of the ordinary
   * `SettingRow`: both avatar keys carry `kind: "string"` in the settings document, an id rather
   * than typed text, so the row intercepts them by key before `SettingRow`'s control-by-`kind`
   * dispatch would render a text field. */
  function row(key: string): ReactElement {
    if (key === "chat.default_model" && modelConnections !== null) {
      return fieldRow(key, (_label, labelId) => (
        <ModelDefaultField
          value={settings.effective[key]}
          labelId={labelId}
          disabled={readOnlyNoticeId !== null}
          onChange={(value) => enqueue(key, value)}
        />
      ));
    }
    const nameKey = AVATAR_NAME_KEY[key];
    if (nameKey !== undefined) return avatarRow(key, nameKey);
    if (key === "voice.read_aloud_voices") return voicesRow(key);
    if (key === "voice.speech_engine") return engineRow(key);
    if (key === "voice.pack_voices") return packVoicesRow(key);
    if (key === "voice.voicebox_profile") return voiceboxRow(key);
    const spec = settings.keys[key];
    if (spec === undefined) throw new Error(`no spec for ${key}`);
    const options = DICTATION_KEYS.has(key) ? voiceOptions(voice.status, key) : undefined;
    return (
      <SettingRow
        key={key}
        settingKey={key}
        spec={spec}
        value={settings.effective[key]}
        source={settings.sources[key] ?? "default"}
        readOnlyNoticeId={readOnlyNoticeId}
        status={status[key] ?? null}
        error={errors[key] ? errors[key] : null}
        onChange={enqueue}
        {...(options === undefined ? {} : { options })}
      />
    );
  }

  function avatarRow(key: string, nameKey: string): ReactElement {
    const nameValue = settings.effective[nameKey];
    const name = typeof nameValue === "string" ? nameValue : "";
    const idValue = settings.effective[key];
    const avatarId = typeof idValue === "string" ? idValue : null;
    return fieldRow(key, (label, labelId) => (
      <AvatarField
        label={label}
        labelId={labelId}
        name={name}
        accent="slate"
        shape="circle"
        avatarId={avatarId}
        disabled={false}
        readOnlyNoticeId={readOnlyNoticeId}
        fallback={false}
        emptyText={t("settings.value.empty")}
        onChange={(id) => enqueue(key, id)}
      />
    ));
  }

  function voicesRow(key: string): ReactElement {
    return fieldRow(key, (_label, labelId) => (
      <ReadAloudVoicesField
        value={settings.effective[key]}
        labelId={labelId}
        readOnlyNoticeId={readOnlyNoticeId}
        onChange={(next) => enqueue(key, next)}
      />
    ));
  }

  function engineRow(key: string): ReactElement {
    return fieldRow(key, (_label, labelId) => (
      <VoiceEngineField
        value={speechEngine(settings.effective)}
        labelId={labelId}
        readOnlyNoticeId={readOnlyNoticeId}
        onChange={(next) => enqueue(key, next)}
      />
    ));
  }

  function packVoicesRow(key: string): ReactElement {
    return fieldRow(key, (_label, labelId) => (
      <PackVoicesField
        value={settings.effective[key]}
        systemVoices={settings.effective["voice.read_aloud_voices"]}
        labelId={labelId}
        readOnlyNoticeId={readOnlyNoticeId}
        engine={speechEngine(settings.effective)}
        onChange={(next) => enqueue(key, next)}
      />
    ));
  }

  function voiceboxRow(key: string): ReactElement {
    const urlValue = settings.effective["voice.voicebox_url"];
    const url = typeof urlValue === "string" ? urlValue : "";
    return fieldRow(key, (_label, labelId) => (
      <VoiceboxField
        value={settings.effective[key]}
        url={url}
        labelId={labelId}
        readOnlyNoticeId={readOnlyNoticeId}
        engine={speechEngine(settings.effective)}
        onChange={(next) => enqueue(key, next)}
      />
    ));
  }

  /** The label, description, control, status and error of a row whose control is not a
   * `SettingRow`'s. */
  function fieldRow(
    key: string,
    control: (label: string, labelId: string) => ReactElement,
  ): ReactElement {
    const label = keyLabel(key);
    const desc = keyDesc(key);
    const labelId = `${key}-label`;
    return (
      <div className="settings-row" data-setting-key={key} key={key}>
        <div className="settings-row-label">
          <span id={labelId}>{label}</span>
          {desc !== "" && <p className="settings-row-desc">{desc}</p>}
          {settings.keys[key]?.personal === true && (
            <p className="settings-row-desc">{t("settings.row.personal_excluded")}</p>
          )}
        </div>
        <div className="settings-row-control">
          {control(label, labelId)}
          <div className="settings-row-status">
            {status[key] != null && (
              <span className="settings-row-saved" role="status">
                {status[key] === "saving" ? <Spinner className="spinner-late" /> : <Check />}
                {t(status[key] === "saving" ? "common.state.saving" : "common.state.saved")}
              </span>
            )}
          </div>
          {errors[key] && (
            <p className="settings-error" role="alert">
              {errors[key]}
            </p>
          )}
        </div>
      </div>
    );
  }

  const showOnboarding = page === "general" && "onboarding.completed" in settings.keys;
  const sections = sectionsOf(known);
  const dictationPanel =
    page === "chat" && dictation !== null ? (
      <DictationSettingsPanel
        api={dictation}
        status={voice.status}
        error={voice.error}
        onRetryGpu={voice.retryGpu}
      />
    ) : null;

  return (
    <section
      className="settings-page"
      aria-labelledby={headingId}
      // A region with a save in flight says so, so a screen reader does not read
      // half-updated rows as final.
      aria-busy={Object.values(status).includes("saving") || undefined}
    >
      <h2 className="settings-page-title" id={headingId}>
        {tabLabel(page)}
      </h2>
      {pageError !== null && (
        <p className="settings-error" role="alert">
          {pageError}
        </p>
      )}
      <p className="settings-page-intro">{pageDescription(page)}</p>
      {readOnlyNoticeId === null && <p className="settings-note">{t("settings.save.automatic")}</p>}
      {sections.map((section, index) => (
        <div className="settings-card" key={section.title ?? "untitled"}>
          {section.title !== null && (
            <div className="settings-card-head">
              <h3 className="settings-card-title">{section.title}</h3>
            </div>
          )}
          {section.keys.map(row)}
          {dictationPanel !== null && index === sections.length - 1 && dictationPanel}
        </div>
      ))}
      {dictationPanel !== null && sections.length === 0 && (
        <div className="settings-card">{dictationPanel}</div>
      )}
      {showOnboarding && (
        <div className="settings-card">
          <div className="settings-card-head">
            <h3 className="settings-card-title">Setup</h3>
          </div>
          <div className="settings-row" data-setting-key="onboarding.completed">
            <div className="settings-row-label">
              <span>{t("settings.general.onboarding.label")}</span>
              <p className="settings-row-desc">{t("settings.general.onboarding.desc")}</p>
            </div>
            <div className="settings-row-control">
              <button
                type="button"
                className="btn alt sm"
                aria-disabled={readOnlyNoticeId !== null || undefined}
                {...(readOnlyNoticeId === null ? {} : { "aria-describedby": readOnlyNoticeId })}
                onClick={() => {
                  if (readOnlyNoticeId !== null) return;
                  enqueue("onboarding.completed", null);
                }}
              >
                {t("settings.general.show_onboarding_again")}
              </button>
              <div className="settings-row-status">
                {status["onboarding.completed"] != null && (
                  <span className="settings-row-saved" role="status">
                    {status["onboarding.completed"] === "saving" ? (
                      <Spinner className="spinner-late" />
                    ) : (
                      <Check />
                    )}
                    {t(
                      status["onboarding.completed"] === "saving"
                        ? "common.state.saving"
                        : "common.state.saved",
                    )}
                  </span>
                )}
              </div>
              {errors["onboarding.completed"] && (
                <p className="settings-error" role="alert">
                  {errors["onboarding.completed"]}
                </p>
              )}
            </div>
          </div>
        </div>
      )}
      {other.length > 0 && (
        <div className="settings-card">
          <div className="settings-card-head">
            <h3 className="settings-card-title">{t("settings.page.other")}</h3>
          </div>
          {other.map(row)}
        </div>
      )}
    </section>
  );
}
