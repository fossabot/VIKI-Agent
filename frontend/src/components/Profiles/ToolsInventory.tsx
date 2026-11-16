/**
 * The Agents view's Tools section: pick one running chat and show its live tool inventory — MCP
 * server rows, MCP tools, skills, memory files, built-in tools and custom agents as plain lists,
 * each falling back to its reason's text when the driver could not observe that part. A server's
 * status is shown as reported; the view never adds a trust annotation of its own.
 */
import { type ReactElement, useCallback, useEffect, useRef, useState } from "react";
import {
  type InventoryApi,
  InventoryApiError,
  type InventoryReason,
  type McpServerRow,
  type McpServerStatus,
  type ToolInventory,
} from "../../clients/conversations/inventoryApi";
import "./AgentsView.css";
import "./CapabilityContext.css";

export interface ToolsInventoryChat {
  sessionId: string;
  title: string;
}

export interface ToolsInventoryProps {
  api: InventoryApi;
  chats: readonly ToolsInventoryChat[];
}

type Load = { inventory: ToolInventory } | { notRunning: true } | { failed: true };

const REASON_TEXT: Record<InventoryReason, string> = {
  not_observed: "No turn has ended yet.",
  absent: "The reply did not include this.",
  unrecognized: "The reply had an unexpected shape.",
  timeout: "The request timed out.",
  unavailable: "This is unavailable.",
};

const STATUS_TEXT: Record<McpServerStatus, string> = {
  connected: "Connected",
  failed: "Failed",
  "needs-auth": "Needs authorization",
  pending: "Pending",
  disabled: "Disabled",
};

const WAY_ON = "Run /mcp in a terminal, or claude mcp.";

// An app server is defined in a profile, so its way on is that profile, not /mcp.
const APP_WAY_ON = "Open its profile in Agents → Profiles and check its MCP servers.";

// The live-name prefix of the app's own servers (src/viki_agent/profiles/mcp_config.py's SERVER_PREFIX).
const APP_SERVER_PREFIX = "viki_";

function ToolsServerRow({ server }: { server: McpServerRow }): ReactElement {
  // Plugin servers share the scope "dynamic"; the CLI's source tells them apart, and a CLI that
  // sends no source never runs an app server.
  const appServer = server.source === "dynamic" && server.name.startsWith(APP_SERVER_PREFIX);
  const scope = appServer ? "app server" : server.scope;
  const wayOn = server.status === "needs-auth" || server.status === "failed";
  return (
    <div className="tools-server">
      <p>{server.name}</p>
      <p>
        <span className="tools-status" data-status={server.status}>
          {STATUS_TEXT[server.status]}
        </span>
        {scope ? ` · ${scope}` : ""}
        {server.egress ? ` · ${server.egress}` : ""}
      </p>
      <p className="capability-context-ref">
        {server.source === null ? "Source not reported" : `Source: ${server.source}`}
      </p>
      {server.server_name ? (
        <p>{[server.server_name, server.server_version].filter(Boolean).join(" ")}</p>
      ) : null}
      {server.tools_omitted ? (
        <p>tools omitted</p>
      ) : server.tools && server.tools.length > 0 ? (
        <p>{server.tools.join(", ")}</p>
      ) : null}
      {wayOn ? <p>{appServer ? APP_WAY_ON : WAY_ON}</p> : null}
    </div>
  );
}

function ListPart({
  label,
  items,
  reason,
}: {
  label: string;
  items: readonly string[] | null;
  reason: InventoryReason | undefined;
}): ReactElement {
  return (
    <section>
      <h4>{label}</h4>
      {items === null ? (
        <p>{reason ? REASON_TEXT[reason] : ""}</p>
      ) : items.length === 0 ? (
        <p>None</p>
      ) : (
        <ul>
          {items.map((item, index) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: a plain read-only list refreshed whole on every load; no item carries its own stable id.
            <li key={index}>{item}</li>
          ))}
        </ul>
      )}
    </section>
  );
}

export function ToolsInventory({ api, chats }: ToolsInventoryProps): ReactElement {
  const [selected, setSelected] = useState<string | null>(null);
  const [load, setLoad] = useState<Load | null>(null);
  const controllerRef = useRef<AbortController | null>(null);

  const runLoad = useCallback(
    (sessionId: string) => {
      controllerRef.current?.abort();
      const controller = new AbortController();
      controllerRef.current = controller;
      setLoad(null);
      void api.get(sessionId, controller.signal).then(
        (inventory) => {
          if (controller.signal.aborted) return;
          setLoad({ inventory });
        },
        (error: unknown) => {
          if (controller.signal.aborted) return;
          if (error instanceof InventoryApiError && error.code === "not_running") {
            setLoad({ notRunning: true });
            return;
          }
          setLoad({ failed: true });
        },
      );
    },
    [api],
  );

  // A chat that leaves the running list clears the selection: its inventory would otherwise keep
  // showing a chat this view can no longer refresh.
  useEffect(() => {
    if (selected !== null && !chats.some((chat) => chat.sessionId === selected)) {
      setSelected(null);
    }
  }, [chats, selected]);

  useEffect(() => {
    if (selected === null) {
      controllerRef.current?.abort();
      setLoad(null);
      return;
    }
    runLoad(selected);
    return () => controllerRef.current?.abort();
  }, [selected, runLoad]);

  if (chats.length === 0) {
    return <p>No running chat</p>;
  }

  const inventory = load && "inventory" in load ? load.inventory : null;
  const selectedChat = chats.find((chat) => chat.sessionId === selected);

  return (
    <div className="tools-inventory">
      <header className="capability-context">
        <h3>Session inventory</h3>
        <p>Choose a running chat to inspect its reported tools and connections.</p>
      </header>
      <div>
        <label>
          Chat{" "}
          <select
            value={selected ?? ""}
            onChange={(event) =>
              setSelected(event.target.value.length > 0 ? event.target.value : null)
            }
          >
            <option value="">Choose a chat</option>
            {chats.map((chat) => (
              <option key={chat.sessionId} value={chat.sessionId}>
                {chat.title}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          disabled={selected === null}
          onClick={() => selected !== null && runLoad(selected)}
        >
          Refresh
        </button>
      </div>
      {load && "notRunning" in load ? <p role="status">This chat is not running.</p> : null}
      {load && "failed" in load ? <p role="status">The tool inventory is unavailable.</p> : null}
      {inventory ? (
        <>
          <section className="capability-context" aria-label="Session observation">
            <h4>Observed in this session</h4>
            <p className="capability-context-name">{selectedChat?.title}</p>
            <p className="capability-context-ref">
              {inventory.observed_at === null
                ? "Observation time not reported"
                : `Observed: ${inventory.observed_at}`}
            </p>
          </section>
          <section className="tools-server-list">
            <h4>MCP servers</h4>
            {inventory.mcp_servers === null ? (
              <p>
                {inventory.reasons.mcp_servers ? REASON_TEXT[inventory.reasons.mcp_servers] : ""}
              </p>
            ) : inventory.mcp_servers.length === 0 ? (
              <p>None</p>
            ) : (
              inventory.mcp_servers.map((server) => (
                <ToolsServerRow key={server.name} server={server} />
              ))
            )}
          </section>
          <ListPart
            label="MCP tools"
            items={
              inventory.mcp_tools === null ? null : inventory.mcp_tools.map((tool) => tool.name)
            }
            reason={inventory.reasons.mcp_tools}
          />
          <ListPart label="Skills" items={inventory.skills} reason={inventory.reasons.skills} />
          <ListPart
            label="Memory files"
            items={
              inventory.memory_files === null
                ? null
                : inventory.memory_files.map((file) => `${file.path} (${file.type})`)
            }
            reason={inventory.reasons.memory_files}
          />
          <ListPart
            label="Built-in tools"
            items={inventory.system_tools}
            reason={inventory.reasons.system_tools}
          />
          <ListPart
            label="Custom agents"
            items={
              inventory.agents === null
                ? null
                : inventory.agents.map((agent) => `${agent.type} (${agent.source})`)
            }
            reason={inventory.reasons.agents}
          />
        </>
      ) : null}
    </div>
  );
}
