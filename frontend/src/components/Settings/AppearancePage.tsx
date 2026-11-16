/**
 * The appearance and accessibility settings page: the one
 * settings page with a live preview and its own Apply / Cancel / Restore defaults, instead of
 * the general/chat/privacy pages' save-on-change (`DocumentPage`).
 *
 * Renders inside the settings panel — the root is `<section className="settings-page">`
 * containing rows built from the shared `.settings-row*` class contract. Its controls keep
 * their own draft values so preview changes can be applied or cancelled together.
 *
 * Deliberately smaller than `DocumentPage`'s rows: no per-row "reset to inherited" button (the
 * page-level "Restore defaults" already resets all four keys at once, and a second, per-row
 * reset would leave open whether it patches immediately or only touches the preview — an
 * ambiguity with no reason to invent).
 *
 * `SettingsView.tsx` renders this page as `children` — the one branch of its own `body()` that
 * is reachable with `settings === null` (its own null guard only covers the `DOCUMENT_PAGES`
 * branch), so this component carries its own loading guard, matching that same pattern. It also
 * means no `readOnlyNoticeId` prop reaches this page (that id is scoped to the pages
 * `SettingsView` builds itself); a read-only document is therefore read from the snapshot here
 * and named through this page's own hidden reason span, not a prop.
 */
import { type ReactElement, useEffect, useId, useMemo, useRef, useState } from "react";
import { ShellApiError } from "../../clients/app/shellApi";
import type { Key } from "../../i18n/index";
import { en } from "../../i18n/index";
import { errorText, t } from "../../i18n/t";
import type { ShellStore } from "../../state/shell";
import { useSnapshot } from "../../state/viki";
import { Loading, Spinner } from "../Loading/Loading";
import { type AppearanceValues, appearanceFromSettings } from "./appearance";
import { pageDescription } from "./pages";

export interface AppearancePageProps {
  store: ShellStore;
  /** The live preview App applies on top of the committed settings;
   * `null` means "nothing to override — render the committed document". */
  onPreview: (values: AppearanceValues | null) => void;
}

type AppearanceKey = keyof AppearanceValues;
const KEYS: readonly AppearanceKey[] = ["theme", "density", "font_scale", "reduce_motion"];

/** `AppearanceValues`' fields to the settings document's own dotted keys. */
const DOCUMENT_KEY: Readonly<Record<AppearanceKey, string>> = {
  theme: "appearance.theme",
  density: "appearance.density",
  font_scale: "accessibility.font_scale",
  reduce_motion: "accessibility.reduce_motion",
};

const LABEL_KEY: Readonly<Record<AppearanceKey, Key>> = {
  theme: "settings.key.appearance_theme.label",
  density: "settings.key.appearance_density.label",
  font_scale: "settings.key.accessibility_font_scale.label",
  reduce_motion: "settings.key.accessibility_reduce_motion.label",
};

const DESC_KEY: Readonly<Record<AppearanceKey, Key>> = {
  theme: "settings.key.appearance_theme.desc",
  density: "settings.key.appearance_density.desc",
  font_scale: "settings.key.accessibility_font_scale.desc",
  reduce_motion: "settings.key.accessibility_reduce_motion.desc",
};

/** `components/Settings/pages.ts`'s `valueLabel` convention, reimplemented here rather than
 * imported (see the module doc): `settings.value.<key, dots as underscores>.<String(value)>`,
 * falling back to the raw token when the catalog has no such row. */
function valueLabel(documentKey: string, value: unknown): string {
  const candidate = `settings.value.${documentKey.replace(/\./g, "_")}.${String(value)}`;
  return candidate in en ? t(candidate as Key) : String(value);
}

/** The row meta line: the source label when source is not "default" and the applies
 * label when it is not "now", joined " · " — "" when neither holds, so the row renders no meta
 * element at all. */
function rowMeta(source: string, applies: string): string {
  const parts: string[] = [];
  if (source !== "default") parts.push(t(`common.source.${source}` as Key));
  if (applies !== "now") parts.push(t(`common.applies.${applies}` as Key));
  return parts.join(" · ");
}

interface RowProps {
  appKey: AppearanceKey;
  value: string | number;
  source: string;
  applies: string;
  allowed: readonly unknown[];
  disabled: boolean;
  /** The read-only reason span's id when the document is read-only, else null (a refused
   * control names its reason through `aria-describedby`). */
  readOnlyReasonId: string | null;
  onChange: (value: string | number) => void;
}

function AppearanceRow({
  appKey,
  value,
  source,
  applies,
  allowed,
  disabled,
  readOnlyReasonId,
  onChange,
}: RowProps): ReactElement {
  const documentKey = DOCUMENT_KEY[appKey];
  const controlId = `appearance-${appKey}`;
  const descId = `${controlId}-desc`;
  const describedBy = [descId, readOnlyReasonId]
    .filter((id): id is string => id !== null)
    .join(" ");
  const meta = rowMeta(source, applies);
  return (
    <div className="settings-row" data-setting-key={documentKey}>
      <div className="settings-row-label">
        <label htmlFor={controlId}>{t(LABEL_KEY[appKey])}</label>
        <p className="settings-row-desc" id={descId}>
          {t(DESC_KEY[appKey])}
        </p>
      </div>
      <div className="settings-row-control">
        <select
          id={controlId}
          className="settings-select"
          aria-describedby={describedBy}
          aria-disabled={disabled || undefined}
          title={readOnlyReasonId !== null ? t("error.read_only") : undefined}
          value={String(value)}
          onChange={(event) => {
            if (disabled) return;
            const index = allowed.findIndex(
              (candidate) => String(candidate) === event.target.value,
            );
            if (index >= 0) onChange(allowed[index] as string | number);
          }}
        >
          {allowed.map((candidate) => (
            <option key={String(candidate)} value={String(candidate)}>
              {valueLabel(documentKey, candidate)}
            </option>
          ))}
        </select>
        {meta !== "" && <p className="settings-row-meta">{meta}</p>}
      </div>
    </div>
  );
}

export function AppearancePage({ store, onPreview }: AppearancePageProps): ReactElement {
  const snapshot = useSnapshot(store);
  const settings = snapshot.settings;
  const uid = useId();
  const headingId = `${uid}-title`;
  const readOnlyReasonId = `${uid}-readonly-reason`;

  const committed = useMemo(() => appearanceFromSettings(settings), [settings]);
  const [draft, setDraft] = useState<AppearanceValues>(committed);
  /** The keys the user has actually changed since the last Apply/Cancel/Restore — patched by
   * key, never by diffing draft against committed (a fresher document landing mid-edit must not
   * make Apply revert a key the user never touched). */
  const [touched, setTouched] = useState<ReadonlySet<AppearanceKey>>(new Set());
  const dirty = touched.size > 0;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ShellApiError | null>(null);
  const [notice, setNotice] = useState<"discarded" | "reloaded" | null>(null);
  /** Guards apply/restore synchronously: React state updates are batched, so two clicks inside
   * one event-handling tick both still see `busy === false` from the render that started them.
   * A ref is read and written outside that batching, so the second call sees the first's guard
   * immediately (house pattern: SettingRow.tsx's `send`/the reset button's onClick). */
  const busyRef = useRef(false);

  // Re-seed the draft from every fresh document: untouched keys follow `committed` (so a change
  // landing from elsewhere is never silently lost), touched keys keep the user's own edit.
  useEffect(() => {
    setDraft((previous) => ({
      theme: touched.has("theme") ? previous.theme : committed.theme,
      density: touched.has("density") ? previous.density : committed.density,
      font_scale: touched.has("font_scale") ? previous.font_scale : committed.font_scale,
      reduce_motion: touched.has("reduce_motion")
        ? previous.reduce_motion
        : committed.reduce_motion,
    }));
  }, [committed, touched]);

  // Leaving the page discards whatever preview is pending, without asking.
  // An effect (not a render-phase write) keeps the ref pointed at the latest `onPreview`, so the
  // unmount cleanup below calls it without making it a dependency of that cleanup itself.
  const onPreviewRef = useRef(onPreview);
  useEffect(() => {
    onPreviewRef.current = onPreview;
  });
  useEffect(() => {
    return () => onPreviewRef.current(null);
  }, []);

  if (settings === null) {
    return snapshot.settingsStatus === "error" ? (
      <p className="settings-state" role="status">
        {errorText(new ShellApiError(0, snapshot.errors.settings))}
      </p>
    ) : (
      <Loading className="settings-state">{t("common.state.loading")}</Loading>
    );
  }

  const readOnly = settings.read_only === true;
  const controlsDisabled = busy || readOnly;

  /** A `409` must not leave the user unable to ever save again: `stale_revision` adopts the
   * document the server actually holds so the next Apply carries its revision;
   * `command_conflict` carries no document, so the honest recovery is a fresh read. Either way
   * the draft stays dirty — the merge effect above keeps the user's own edits and only the
   * now-stale fields move — and the status line says what happened. */
  function handlePatchError(err: unknown): void {
    const apiErr = err instanceof ShellApiError ? err : new ShellApiError(0, "unavailable");
    setError(apiErr);
    if (apiErr.code === "stale_revision" && apiErr.current !== null) {
      store.adoptSettings(apiErr.current);
      setNotice("reloaded");
    } else if (apiErr.code === "command_conflict") {
      void store.refreshSettings();
      setNotice("reloaded");
    }
  }

  function change(key: AppearanceKey, value: string | number): void {
    if (controlsDisabled) return;
    const next = { ...draft, [key]: value };
    setDraft(next);
    setTouched((previous) => new Set(previous).add(key));
    setError(null);
    setNotice(null);
    onPreview(next);
  }

  function cancel(): void {
    if (busy || readOnly || !dirty) return;
    setDraft(committed);
    setTouched(new Set());
    setError(null);
    setNotice("discarded");
    onPreview(null);
  }

  function changedPatch(): Record<string, unknown> {
    const result: Record<string, unknown> = {};
    for (const key of touched) result[DOCUMENT_KEY[key]] = draft[key];
    return result;
  }

  async function apply(): Promise<void> {
    if (busyRef.current || readOnly || !dirty) return;
    const patch = changedPatch();
    busyRef.current = true;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await store.patch(patch);
      setTouched(new Set());
      onPreview(null);
    } catch (err) {
      handlePatchError(err);
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }

  async function restore(): Promise<void> {
    if (busyRef.current || readOnly) return;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await store.patch({
        "appearance.theme": null,
        "appearance.density": null,
        "accessibility.font_scale": null,
        "accessibility.reduce_motion": null,
      });
      setTouched(new Set());
      onPreview(null);
    } catch (err) {
      handlePatchError(err);
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }

  return (
    <section className="settings-page" aria-labelledby={headingId} aria-busy={busy}>
      <h2 className="settings-page-title" id={headingId}>
        {t("settings.tab.appearance")}
      </h2>
      <p className="settings-page-intro">{pageDescription("appearance")}</p>
      {readOnly && (
        <span id={readOnlyReasonId} className="sr-only">
          {t("error.read_only")}
        </span>
      )}
      <div className="settings-card">
        {KEYS.map((key) => (
          <AppearanceRow
            key={key}
            appKey={key}
            value={draft[key]}
            source={settings.sources[DOCUMENT_KEY[key]] ?? "default"}
            applies={settings.keys[DOCUMENT_KEY[key]]?.applies ?? "now"}
            allowed={settings.keys[DOCUMENT_KEY[key]]?.allowed ?? []}
            disabled={controlsDisabled}
            readOnlyReasonId={readOnly ? readOnlyReasonId : null}
            onChange={(value) => change(key, value)}
          />
        ))}
      </div>
      {error !== null && (
        <p className="settings-error" role="alert">
          {errorText(error)}
        </p>
      )}
      <div className="settings-actions">
        <button
          type="button"
          className="btn go sm"
          aria-disabled={controlsDisabled || !dirty || undefined}
          title={readOnly ? t("error.read_only") : undefined}
          aria-describedby={readOnly ? readOnlyReasonId : undefined}
          onClick={() => void apply()}
        >
          {t("common.button.apply")}
        </button>
        <button
          type="button"
          className="btn alt sm"
          aria-disabled={controlsDisabled || !dirty || undefined}
          title={readOnly ? t("error.read_only") : undefined}
          aria-describedby={readOnly ? readOnlyReasonId : undefined}
          onClick={cancel}
        >
          {t("common.button.cancel")}
        </button>
        <button
          type="button"
          className="btn alt sm"
          aria-disabled={controlsDisabled || undefined}
          title={readOnly ? t("error.read_only") : undefined}
          aria-describedby={readOnly ? readOnlyReasonId : undefined}
          onClick={() => void restore()}
        >
          {t("appearance.restore_defaults")}
        </button>
        {busy && (
          <span role="status" className="settings-row-saved">
            <Spinner className="spinner-late" />
            {t("common.state.saving")}
          </span>
        )}
        {!busy && notice !== null && (
          <span role="status" className="settings-row-saved">
            {t(notice === "discarded" ? "appearance.discarded" : "appearance.reloaded")}
          </span>
        )}
      </div>
    </section>
  );
}
