/**
 * The `data` view: four panels in one column — Locations, Export, Import, Reset
 * — always mounted together (nothing here is a tab switch). `nav.panel` names which panel's own
 * heading receives focus and a scroll-into-view on entry, exactly as `nav.page` does for
 * Settings; the view's own `<h1>` carries the "Back to chat" pattern every non-chat view uses,
 * reusing the `shell.nav.back_to_chat` catalog row.
 *
 * Entry focus is entirely this view's own: with a panel named, that panel's `<h2>` gets it;
 * otherwise the view's own `<h1>` does, on mount. App never focuses anything on a switch into
 * `data` — two effects racing for focus would lose the Export heading to a parent-level h1
 * focus on `data.export_diagnostics` navigations.
 */
import {
  type ReactElement,
  type ReactNode,
  type RefObject,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import type {
  DataLocations,
  ResetResult,
  ShellApi,
  ShellApiError,
} from "../../clients/app/shellApi";
import type { DataPanel, Navigation } from "../../commands";
import { t } from "../../i18n/t";
import type { ShellStore } from "../../state/shell";
import type { VaultKnowledgeApi } from "../../state/vaultKnowledgeApi";
import { BackupPanel } from "./BackupPanel";
import "./Data.css";
import { ExportPanel, type PathFieldProps } from "./ExportPanel";
import { ImportPanel } from "./ImportPanel";
import { LocationsPanel } from "./LocationsPanel";
import { ResetPanel } from "./ResetPanel";

export type RenderPathField = (props: PathFieldProps) => ReactNode;
export type RenderConflict = (error: ShellApiError) => ReactNode;

export interface DataViewProps {
  store: ShellStore;
  api: ShellApi | null;
  /** Which panel's own heading owns entry focus; absent means the view's own `<h1>` does. */
  panel?: DataPanel;
  /** Mirrors App's own `navigate`: a partial `Navigation` merged into the current one. Used for
   * the one cross-view jump this view makes — "Back to chat" → `{ view: "chat" }`. */
  onNavigate: (nav: Partial<Navigation>) => void;
  renderBack?: (onBack: () => void) => ReactNode;
  renderPathField: RenderPathField;
  renderConflict: RenderConflict;
  /** Absent hides the Backup panel. */
  knowledgeApi?: VaultKnowledgeApi | undefined;
  onReset?: ((result: ResetResult) => void) | undefined;
}

// Callers import this component as `DataView`;
// it shadows the unrelated global `DataView` (a typed-array view constructor this file never
// uses).
// biome-ignore lint/suspicious/noShadowRestrictedNames: see above
export function DataView({
  store,
  api,
  panel,
  onNavigate,
  renderBack,
  renderPathField,
  renderConflict,
  knowledgeApi,
  onReset,
}: DataViewProps): ReactElement {
  const backupHeadingRef = useRef<HTMLHeadingElement>(null);
  const h1Ref = useRef<HTMLHeadingElement>(null);
  const locationsHeadingRef = useRef<HTMLHeadingElement>(null);
  const exportHeadingRef = useRef<HTMLHeadingElement>(null);
  const importHeadingRef = useRef<HTMLHeadingElement>(null);
  const resetHeadingRef = useRef<HTMLHeadingElement>(null);
  const [edgeProfilePath, setEdgeProfilePath] = useState<string | null>(null);

  const handleLocationsLoaded = useCallback((locations: DataLocations) => {
    for (const category of locations.categories)
      if (category.id === "app_data")
        for (const item of category.items)
          if (item.kind === "edge_profile") setEdgeProfilePath(item.path);
  }, []);

  useEffect(() => {
    const refs: Record<DataPanel, RefObject<HTMLHeadingElement | null>> = {
      locations: locationsHeadingRef,
      export: exportHeadingRef,
      import: importHeadingRef,
      reset: resetHeadingRef,
    };
    const heading = panel ? refs[panel].current : null;
    // jsdom (every test here) has no `scrollIntoView`; real browsers do, and Chromium is the
    // only one this app ever runs in.
    if (heading) {
      heading.scrollIntoView?.({ block: "start" });
      heading.focus();
    } else {
      h1Ref.current?.focus();
    }
  }, [panel]);

  return (
    <div className="data-view">
      <header className="data-view-header">
        {renderBack?.(() => onNavigate({ view: "chat" })) ?? (
          <button type="button" className="btn txt sm" onClick={() => onNavigate({ view: "chat" })}>
            {t("shell.nav.back_to_chat")}
          </button>
        )}
        <h1 ref={h1Ref} tabIndex={-1}>
          {t("data.title")}
        </h1>
      </header>
      <LocationsPanel api={api} headingRef={locationsHeadingRef} onLoaded={handleLocationsLoaded} />
      <ExportPanel api={api} renderPathField={renderPathField} headingRef={exportHeadingRef} />
      {knowledgeApi && (
        <BackupPanel
          knowledgeApi={knowledgeApi}
          store={store}
          renderPathField={renderPathField}
          headingRef={backupHeadingRef}
        />
      )}
      <ImportPanel
        api={api}
        store={store}
        renderConflict={renderConflict}
        headingRef={importHeadingRef}
      />
      <ResetPanel
        api={api}
        store={store}
        headingRef={resetHeadingRef}
        edgeProfilePath={edgeProfilePath}
        onReset={onReset}
      />
    </div>
  );
}
