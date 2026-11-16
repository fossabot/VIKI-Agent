import { type ReactNode, useEffect, useId, useRef, useState } from "react";
import type { InventoryApi } from "../../clients/conversations/inventoryApi";
import type { NativeAssetsApi } from "../../clients/profiles/nativeAssetsApi";
import type { ProfileApi } from "../../clients/profiles/profileApi";
import type {
  ProfileLaunchApi,
  ProfileSessionIdentity,
} from "../../clients/profiles/profileLaunchApi";
import type { ToolsInventoryChat } from "../Profiles/ToolsInventory";
import { ConnectorSelection } from "./ConnectorSelection";
import { InventoryCapabilities } from "./InventoryCapabilities";
import { NativeCapabilities } from "./NativeCapabilities";
import "./CapabilitiesView.css";

const TABS = ["Skills", "Subagents", "Tools", "Connectors", "Plugins"] as const;
type Tab = (typeof TABS)[number];

export interface CapabilitiesViewProps {
  nativeApi?: NativeAssetsApi;
  inventoryApi?: InventoryApi;
  profileApi?: ProfileApi;
  launchApi?: ProfileLaunchApi;
  chats: readonly ToolsInventoryChat[];
  workspaceId?: string;
  onPrepared?(id: string, identity: ProfileSessionIdentity | null): void;
  onManageProfiles?(): void;
  onManageNative?(): void;
  native?: ReactNode;
  nativeVisit?: number;
}

export function CapabilitiesView(props: CapabilitiesViewProps) {
  const id = useId();
  const [tab, setTab] = useState<Tab>(props.nativeVisit ? "Subagents" : "Skills");
  const [connectorsVisited, setConnectorsVisited] = useState(false);
  const buttons = useRef<(HTMLButtonElement | null)[]>([]);
  useEffect(() => {
    if (props.nativeVisit) setTab("Subagents");
  }, [props.nativeVisit]);

  function select(next: Tab) {
    setTab(next);
    if (next === "Connectors") setConnectorsVisited(true);
  }

  return (
    <section className="capabilities-view" aria-label="Capabilities">
      <span className="capabilities-eyebrow">VIKI AGENT · CAPABILITIES</span>
      <header className="page-head">
        <h1 className="page-title">Shape what comes next.</h1>
        <p className="page-intro">
          Explore your tools and choose app-profile connectors for your next session.
        </p>
      </header>
      <div className="capabilities-tabs" role="tablist" aria-label="Capability categories">
        {TABS.map((name, index) => (
          <button
            key={name}
            ref={(node) => {
              buttons.current[index] = node;
            }}
            type="button"
            role="tab"
            id={`${id}-${name}`}
            aria-controls={`${id}-${name}-panel`}
            aria-selected={tab === name}
            tabIndex={tab === name ? 0 : -1}
            onClick={() => select(name)}
            onKeyDown={(event) => {
              let next: number;
              if (event.key === "ArrowRight") next = (index + 1) % TABS.length;
              else if (event.key === "ArrowLeft") next = (index + TABS.length - 1) % TABS.length;
              else if (event.key === "Home") next = 0;
              else if (event.key === "End") next = TABS.length - 1;
              else return;
              event.preventDefault();
              select(TABS[next] as Tab);
              buttons.current[next]?.focus();
            }}
          >
            {name}
          </button>
        ))}
      </div>
      {tab === "Subagents" && props.onManageNative ? (
        <button type="button" className="btn txt sm" onClick={props.onManageNative}>
          Browse Skills
        </button>
      ) : null}
      {tab !== "Connectors" && (
        <div role="tabpanel" id={`${id}-${tab}-panel`} aria-labelledby={`${id}-${tab}`}>
          {tab === "Skills" ? (
            props.nativeApi ? (
              <NativeCapabilities api={props.nativeApi} onManage={props.onManageNative} />
            ) : (
              <p>Native skill discovery is unavailable in this environment.</p>
            )
          ) : tab === "Subagents" ? (
            (props.native ?? <p>Native subagent discovery is unavailable in this environment.</p>)
          ) : props.inventoryApi ? (
            <InventoryCapabilities
              key={tab}
              api={props.inventoryApi}
              chats={props.chats}
              category={tab === "Tools" ? "tools" : "plugins"}
            />
          ) : (
            <p>Session inventory is unavailable in this environment.</p>
          )}
        </div>
      )}
      {connectorsVisited && (
        <div
          role="tabpanel"
          id={`${id}-Connectors-panel`}
          aria-labelledby={`${id}-Connectors`}
          hidden={tab !== "Connectors"}
        >
          {props.profileApi ? (
            <ConnectorSelection
              profileApi={props.profileApi}
              launchApi={props.launchApi}
              workspaceId={props.workspaceId}
              onPrepared={props.onPrepared}
              onManageProfiles={props.onManageProfiles}
            />
          ) : (
            <p>App-profile connector selection is unavailable in this environment.</p>
          )}
          <section className="capabilities-observed" aria-label="Observed connections">
            <h2>Connections observed in a running session</h2>
            {props.inventoryApi ? (
              <InventoryCapabilities
                api={props.inventoryApi}
                chats={props.chats}
                category="connectors"
              />
            ) : (
              <p>Session inventory is unavailable in this environment.</p>
            )}
          </section>
        </div>
      )}
    </section>
  );
}
