/**
 * The settings view: the search field, the eight-page nav and one `.settings-panel`
 * holding either a page built from the document (General, Chat, Privacy) or whatever the
 * caller passes as `children` (the Appearance, Providers, Workspaces, Persona and
 * Diagnostics pages). This file imports none of those pages — the panel is a slot, and
 * `Settings.css`'s class contract is what they render against.
 *
 * What this component owns that the pages cannot:
 * - The review panel. A 409 arrives on one page but the answer is a document, and only the view
 *   may adopt one; the page hands the error and the user's changes up.
 * - The read-only notice, because every page's controls name it through `aria-describedby`.
 * - The `<h1 tabIndex={-1}>` and the "Back to chat" button every non-chat view carries.
 *   The heading takes focus when the view is entered and **not** when the page
 *   changes: the nav's arrow keys move focus to the tab they select, and a heading that grabbed
 *   it back would make the tab strip unusable from the keyboard.
 */

import { type ReactElement, type ReactNode, useEffect, useId, useRef, useState } from "react";
import { ShellApiError } from "../../clients/app/shellApi";
import type { Navigation, SettingsPage } from "../../commands";
import { errorText, t } from "../../i18n/t";
import type { ShellStore } from "../../state/shell";
import { useSnapshot } from "../../state/viki";
import { Loading } from "../Loading/Loading";
import { DocumentPage } from "./DocumentPage";
import { DOCUMENT_PAGES } from "./pages";
import { ReadOnlyNotice } from "./ReadOnlyNotice";
import { ReviewPanel } from "./ReviewPanel";
import { SettingsNav, tabId } from "./SettingsNav";
import { SettingsSearch } from "./SettingsSearch";
import "./Settings.css";

/** The class the pages render inside; exported for their CSS tests. */
export const SETTINGS_PANEL_CLASS = "settings-panel";

export interface SettingsViewProps {
  store: ShellStore;
  page: SettingsPage;
  onPage: (page: SettingsPage) => void;
  /** App's `navigate`: this view uses it only to leave for the chat. */
  onNavigate: (patch: Partial<Navigation>) => void;
  renderBack?: (onBack: () => void) => ReactNode;
  /** The panel's content for the pages this view does not build. */
  children?: ReactNode;
}

interface Conflict {
  error: ShellApiError;
  changes: Readonly<Record<string, unknown>>;
}

export function SettingsView({
  store,
  page,
  onPage,
  onNavigate,
  renderBack,
  children,
}: SettingsViewProps): ReactElement {
  const snapshot = useSnapshot(store);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const base = useId();
  const panelId = `${base}-panel`;
  const noticeId = `${base}-readonly`;
  const pageHeadingId = `${base}-page`;
  const [query, setQuery] = useState("");
  const [conflict, setConflict] = useState<Conflict | null>(null);

  useEffect(() => {
    headingRef.current?.focus();
  }, []);

  const settings = snapshot.settings;
  const readOnlyNoticeId = settings?.read_only === true ? noticeId : null;

  function reload(): void {
    const current = conflict?.error.current ?? null;
    // `command_conflict` carries no document, so the only honest reload is a fresh GET.
    if (current === null) void store.refreshSettings();
    else store.adoptSettings(current);
    setConflict(null);
  }

  function body(): ReactNode {
    // The other pages read capabilities and workspaces, never the document, so a settings
    // read that failed must not take them down with it — Diagnostics is exactly where the user
    // is sent when the app's own storage is the broken thing.
    if (!DOCUMENT_PAGES.includes(page)) return children;
    if (settings === null)
      return snapshot.settingsStatus === "error" ? (
        <p className="settings-state" role="status">
          {errorText(new ShellApiError(0, snapshot.errors.settings))}
        </p>
      ) : (
        <Loading className="settings-state">{t("common.state.loading")}</Loading>
      );
    return (
      <DocumentPage
        page={page}
        settings={settings}
        headingId={pageHeadingId}
        readOnlyNoticeId={readOnlyNoticeId}
        patch={store.patch}
        onConflict={(error, changes) => setConflict({ error, changes })}
      />
    );
  }

  return (
    <div className="settings-view" data-region="settings">
      <header className="settings-header">
        {renderBack?.(() => onNavigate({ view: "chat" })) ?? (
          <button
            type="button"
            className="btn txt sm settings-back"
            onClick={() => onNavigate({ view: "chat" })}
          >
            {t("shell.nav.back_to_chat")}
          </button>
        )}
        <div className="page-head">
          <h1 className="settings-title page-title" tabIndex={-1} ref={headingRef}>
            {t("settings.title")}
          </h1>
          <p className="page-intro">{t("settings.intro")}</p>
        </div>
        {settings !== null && (
          <SettingsSearch
            keys={settings.keys}
            query={query}
            onQuery={setQuery}
            onOpenPage={(target) => {
              onPage(target);
              setQuery("");
            }}
          />
        )}
      </header>
      <div className="settings-grid">
        <SettingsNav page={page} onPage={onPage} panelId={panelId} />
        <div
          className={SETTINGS_PANEL_CLASS}
          id={panelId}
          role="tabpanel"
          aria-labelledby={tabId(page)}
          tabIndex={-1}
        >
          {settings !== null && readOnlyNoticeId !== null && (
            <ReadOnlyNotice id={readOnlyNoticeId} reason={settings.reason} />
          )}
          {body()}
        </div>
      </div>
      {conflict !== null && (
        <ReviewPanel
          error={conflict.error}
          changes={conflict.changes}
          onReload={reload}
          onClose={() => setConflict(null)}
        />
      )}
    </div>
  );
}
