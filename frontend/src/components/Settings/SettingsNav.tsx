/**
 * The settings view's page list: a `role="tablist"` of eight tabs in page order,
 * a 220 px column at 800 effective root pixels and above and wrapping tabs below it.
 *
 * Keyboard: the WAI-ARIA tabs pattern with automatic activation — one tab is in the tab order
 * (`tabIndex 0`), the arrows move focus and select in the same keystroke, Home/End jump to the
 * ends. Both axes are handled whatever the orientation: the nav is the same eight controls
 * laid out sideways, and a user who learned ArrowDown at 1440 px should not lose it at 400.
 *
 * `useLayoutMode` applies root font scale to the 800 px breakpoint. CSS keys navigation layout
 * off `aria-orientation`, which also tells a screen reader which arrows its user can reach for.
 */

import { type ReactElement, useRef } from "react";
import type { SettingsPage } from "../../commands";
import { t } from "../../i18n/t";
import { useLayoutMode } from "../Shell/useLayoutMode";
import { SETTINGS_PAGES, tabHint, tabLabel } from "./pages";

export interface SettingsNavProps {
  page: SettingsPage;
  onPage: (page: SettingsPage) => void;
  /** The `.settings-panel`'s id, named by every tab's `aria-controls`. */
  panelId: string;
}

/** The panel names the selected tab with this, so the two ids are written in one place. */
export function tabId(page: SettingsPage): string {
  return `settings-tab-${page}`;
}

const MOVES: Readonly<Record<string, number>> = {
  ArrowDown: 1,
  ArrowRight: 1,
  ArrowUp: -1,
  ArrowLeft: -1,
};

export function SettingsNav({ page, onPage, panelId }: SettingsNavProps): ReactElement {
  const listRef = useRef<HTMLDivElement>(null);
  const { leftDocked } = useLayoutMode();

  function move(index: number): void {
    const next = SETTINGS_PAGES[index];
    if (next === undefined) return;
    onPage(next);
    listRef.current?.querySelector<HTMLElement>(`[data-page="${next}"]`)?.focus();
  }

  return (
    <div
      ref={listRef}
      className="settings-nav"
      role="tablist"
      aria-label={t("settings.nav.label")}
      aria-orientation={leftDocked ? "vertical" : "horizontal"}
      onKeyDown={(event) => {
        // A candidate window's arrows belong to the IME, never to the tab strip.
        if (event.nativeEvent.isComposing || event.keyCode === 229) return;
        const from = SETTINGS_PAGES.indexOf(page);
        const step = MOVES[event.key];
        if (step !== undefined) {
          event.preventDefault();
          move((from + step + SETTINGS_PAGES.length) % SETTINGS_PAGES.length);
          return;
        }
        if (event.key === "Home") {
          event.preventDefault();
          move(0);
        } else if (event.key === "End") {
          event.preventDefault();
          move(SETTINGS_PAGES.length - 1);
        }
      }}
    >
      {SETTINGS_PAGES.map((candidate) => (
        <button
          key={candidate}
          type="button"
          role="tab"
          id={tabId(candidate)}
          data-page={candidate}
          className={`settings-tab${candidate === page ? " settings-tab-on" : ""}`}
          aria-selected={candidate === page}
          aria-controls={panelId}
          aria-describedby={`${tabId(candidate)}-hint`}
          tabIndex={candidate === page ? 0 : -1}
          onClick={() => onPage(candidate)}
        >
          <span className="settings-tab-label">{tabLabel(candidate)}</span>
          <span className="settings-tab-hint" id={`${tabId(candidate)}-hint`} aria-hidden="true">
            {tabHint(candidate)}
          </span>
        </button>
      ))}
    </div>
  );
}
