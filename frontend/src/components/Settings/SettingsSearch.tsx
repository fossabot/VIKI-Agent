/**
 * Search setting names, labels, descriptions and page summaries with a substring match.
 * Page summaries also make pages without document keys, such as Providers, discoverable.
 *
 * Folding is `toLocaleLowerCase("en")`, the UI's language: the Turkish dotless-i rule is the
 * reason the locale is passed at all rather than left to the host's default.
 *
 * The results do not replace the page: they are listed under the field, grouped by page title
 * with an "Open this page" button each, and the nav and the page keep working underneath. The
 * field carries `data-region="settings-search"` so a caller can find it without this component
 * owning a ref prop. Nothing dispatches `settings.search`: the command table keeps the id,
 * and the app menu offers Settings itself instead.
 */

import { type ReactElement, useMemo } from "react";
import type { KeySpec } from "../../clients/app/shellApi";
import type { SettingsPage } from "../../commands";
import { t, tf } from "../../i18n/t";
import {
  HIDDEN_KEYS,
  keyLabel,
  pageDescription,
  pageOfKey,
  SETTINGS_PAGES,
  searchText,
  tabHint,
  tabLabel,
} from "./pages";
import { tabId } from "./SettingsNav";

export interface SettingsSearchProps {
  keys: Readonly<Record<string, KeySpec>>;
  query: string;
  onQuery: (query: string) => void;
  onOpenPage: (page: SettingsPage) => void;
}

export interface SearchGroup {
  page: SettingsPage;
  keys: string[];
}

/** Every page that has a hit, in the nav's own order, with its hits in the document's order. */
export function searchSettings(
  keys: Readonly<Record<string, KeySpec>>,
  query: string,
): SearchGroup[] {
  const needle = query.trim().toLocaleLowerCase("en");
  if (needle === "") return [];
  const hits = Object.keys(keys).filter(
    (key) => !HIDDEN_KEYS.has(key) && searchText(key).toLocaleLowerCase("en").includes(needle),
  );
  return SETTINGS_PAGES.map((page) => ({
    page,
    keys: hits.filter((key) => pageOfKey(key) === page),
  })).filter(
    (group) =>
      group.keys.length > 0 ||
      `${tabLabel(group.page)} ${tabHint(group.page)} ${pageDescription(group.page)}`
        .toLocaleLowerCase("en")
        .includes(needle),
  );
}

export function SettingsSearch({
  keys,
  query,
  onQuery,
  onOpenPage,
}: SettingsSearchProps): ReactElement {
  const groups = useMemo(() => searchSettings(keys, query), [keys, query]);
  const count = groups.reduce((total, group) => total + Math.max(1, group.keys.length), 0);
  const searching = query.trim() !== "";

  return (
    <div className="settings-search">
      <input
        type="search"
        className="settings-search-input"
        data-region="settings-search"
        aria-label={t("settings.search.label")}
        placeholder={t("settings.search.placeholder")}
        spellCheck={false}
        autoComplete="off"
        maxLength={256}
        value={query}
        onChange={(event) => onQuery(event.target.value)}
      />
      <p className="settings-search-count" role="status">
        {searching
          ? count === 0
            ? t("settings.search.empty")
            : tf("settings.search.results", { count })
          : ""}
      </p>
      {searching && count > 0 && (
        <div className="settings-search-results">
          {groups.map((group) => (
            <section className="settings-search-group" key={group.page}>
              <h3 className="settings-search-group-title">{tabLabel(group.page)}</h3>
              <p className="settings-row-desc">{pageDescription(group.page)}</p>
              {group.keys.length > 0 && (
                <ul>
                  {group.keys.map((key) => (
                    <li key={key} data-setting-key={key}>
                      {keyLabel(key)}
                    </li>
                  ))}
                </ul>
              )}
              <button
                type="button"
                className="btn alt sm"
                onClick={() => {
                  onOpenPage(group.page);
                  // The results unmount with this button inside them, which would drop focus on
                  // <body> and announce nothing. The nav's tab for the opened page survives the
                  // re-render, so focus lands on the thing the click actually selected.
                  document.getElementById(tabId(group.page))?.focus();
                }}
              >
                {t("settings.search.open_page")}
              </button>
            </section>
          ))}
        </div>
      )}
    </div>
  );
}
