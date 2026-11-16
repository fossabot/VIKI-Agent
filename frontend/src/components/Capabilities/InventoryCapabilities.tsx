import { type ReactElement, useCallback, useEffect, useId, useRef, useState } from "react";
import {
  type InventoryApi,
  InventoryApiError,
  type InventoryReason,
  type McpServerRow,
  type ToolInventory,
} from "../../clients/conversations/inventoryApi";
import { Loading } from "../Loading/Loading";
import type { ToolsInventoryChat } from "../Profiles/ToolsInventory";
import "./InventoryCapabilities.css";

export interface InventoryCapabilitiesProps {
  api: InventoryApi;
  chats: readonly ToolsInventoryChat[];
  category: "tools" | "connectors" | "plugins";
}

type Row = {
  key: string;
  name: string;
  origin: string;
  server?: McpServerRow;
  connection?: string;
  loaded?: boolean;
};
type Load = { api: InventoryApi; sessionId: string } & (
  | { inventory: ToolInventory }
  | { error: "not_running" | "unavailable" }
);
const REASONS: Record<InventoryReason, string> = {
  not_observed: "No turn has ended yet.",
  absent: "The reply did not include this.",
  unrecognized: "The reply had an unexpected shape.",
  timeout: "The request timed out.",
  unavailable: "This is unavailable.",
};

function reportedRows(
  inventory: ToolInventory,
  category: InventoryCapabilitiesProps["category"],
): Row[] {
  if (category !== "tools") {
    return (inventory.mcp_servers ?? [])
      .filter((server) => category !== "plugins" || server.source === "plugin")
      .map((server, index) => ({
        key: `server-${index}`,
        name: server.name,
        origin: "MCP server",
        server,
      }));
  }
  return [
    ...(inventory.system_tools ?? []).map((name, index) => ({
      key: `system-${index}`,
      name,
      origin: "Built-in tool inventory",
    })),
    ...(inventory.mcp_tools ?? []).map((tool, index) => ({
      key: `mcp-${index}`,
      name: tool.name,
      origin: "MCP tool inventory",
      loaded: tool.loaded,
      connection: tool.server,
      server: inventory.mcp_servers?.find((server) => server.name === tool.server),
    })),
  ];
}

export function InventoryCapabilities({
  api,
  chats,
  category,
}: InventoryCapabilitiesProps): ReactElement {
  const [selected, setSelected] = useState<string | null>(null);
  const [load, setLoad] = useState<Load | null>(null);
  const [search, setSearch] = useState({ category, value: "" });
  const [active, setActive] = useState<{
    category: InventoryCapabilitiesProps["category"];
    key: string;
    inventory: ToolInventory | null;
  }>({ category, key: "", inventory: null });
  const controllerRef = useRef<AbortController | null>(null);
  const detailId = useId();
  const chat = chats.find((chat) => chat.sessionId === selected);
  const sessionId = chat?.sessionId ?? null;
  const current = load?.api === api && load.sessionId === sessionId ? load : null;
  const inventory = current && "inventory" in current ? current.inventory : null;

  const runLoad = useCallback(
    (id: string) => {
      controllerRef.current?.abort();
      const controller = new AbortController();
      controllerRef.current = controller;
      setLoad(null);
      void api.get(id, controller.signal).then(
        (inventory) => {
          if (!controller.signal.aborted && controllerRef.current === controller) {
            setLoad({ api, sessionId: id, inventory });
          }
        },
        (error: unknown) => {
          if (!controller.signal.aborted && controllerRef.current === controller) {
            setLoad({
              api,
              sessionId: id,
              error:
                error instanceof InventoryApiError && error.code === "not_running"
                  ? "not_running"
                  : "unavailable",
            });
          }
        },
      );
    },
    [api],
  );
  useEffect(() => {
    if (sessionId === null) {
      controllerRef.current?.abort();
      setSelected(null);
      setLoad(null);
    } else runLoad(sessionId);
    return () => controllerRef.current?.abort();
  }, [sessionId, runLoad]);

  const label = category === "tools" ? "Tools" : category === "plugins" ? "Plugins" : "Connectors";
  const rows = inventory ? reportedRows(inventory, category) : [];
  const query = search.category === category ? search.value : "";
  const filtered = rows.filter((row) =>
    [row.name, row.origin, row.server?.source, row.server?.scope, row.server?.status].some(
      (value) => value?.toLowerCase().includes(query.trim().toLowerCase()),
    ),
  );
  const detail =
    filtered.find(
      (row) =>
        active.inventory === inventory && active.category === category && row.key === active.key,
    ) ?? filtered[0];
  const unknownParts = inventory
    ? (category === "tools"
        ? (["system_tools", "mcp_tools"] as const)
        : (["mcp_servers"] as const)
      ).filter((part) => inventory[part] === null)
    : [];
  const noun =
    category === "tools" ? "tools" : category === "plugins" ? "plugin sources" : "connections";
  const unreportedSources =
    inventory?.mcp_servers?.filter((server) => server.source === null).length ?? 0;
  const evidence = {
    Source: detail?.server?.source,
    Scope: detail?.server?.scope,
    Status: detail?.server?.status,
    ...(detail?.connection === undefined ? {} : { Connection: detail.connection }),
    ...(detail?.loaded === undefined
      ? {}
      : { "Tool availability": detail.loaded ? "Loaded" : "Not loaded" }),
    ...(detail?.server
      ? {
          Egress: detail.server.egress,
          "Server name": detail.server.server_name,
          "Server version": detail.server.server_version,
        }
      : {}),
  };
  const notice =
    sessionId === null ? (
      chats.length === 0 ? (
        "No running chat. Start a chat to observe its inventory."
      ) : (
        "Choose a running chat to inspect its reported inventory."
      )
    ) : current === null ? (
      <Loading inline announce={false}>
        Loading inventory…
      </Loading>
    ) : "error" in current ? (
      current.error === "not_running" ? (
        "This chat is not running."
      ) : (
        "The tool inventory is unavailable."
      )
    ) : null;

  return (
    <section className="inventory-capabilities" aria-label={`${label} inventory`}>
      <div className="inventory-capabilities-controls">
        <label>
          Running chat{" "}
          <select
            value={sessionId ?? ""}
            disabled={chats.length === 0}
            onChange={(event) => setSelected(event.target.value || null)}
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
          className="btn txt sm"
          type="button"
          disabled={sessionId === null}
          onClick={() => sessionId !== null && runLoad(sessionId)}
        >
          Refresh inventory
        </button>
      </div>
      {notice ? <p role="status">{notice}</p> : null}
      {inventory ? (
        <>
          <header className="inventory-capabilities-observation">
            <p>Observed in: {chat?.title}</p>
            <code>Session: {sessionId}</code>
            <p>
              {inventory.observed_at === null
                ? "Observation time not reported"
                : `Observed: ${inventory.observed_at}`}
            </p>
            <p>
              Reported session inventory. Manage provider capabilities with Claude; app MCP servers
              are configured in profiles.
            </p>
          </header>
          <div className="inventory-capabilities-toolbar">
            <label>
              Search {category}
              <input
                type="search"
                value={query}
                onChange={(event) => setSearch({ category, value: event.target.value })}
              />
            </label>
            <p role="status">
              {unknownParts.length > 0 && rows.length === 0
                ? category === "tools"
                  ? "Tool count unavailable"
                  : "Connection count unavailable"
                : `${filtered.length} of ${rows.length} reported ${noun}`}
              {unknownParts.length > 0 ? " · Partial observation" : ""}
            </p>
          </div>
          {unknownParts.map((part) => (
            <p key={part} className="inventory-capabilities-reason">
              {category === "tools"
                ? `${part === "system_tools" ? "Built-in tools" : "MCP tools"}: `
                : ""}
              {REASONS[inventory.reasons[part] ?? "unavailable"]}
            </p>
          ))}
          {category === "plugins" && unreportedSources > 0 ? (
            <p className="inventory-capabilities-reason">
              Source not reported for {unreportedSources} connection
              {unreportedSources === 1 ? "" : "s"}; plugin membership is unknown.
            </p>
          ) : null}
          {rows.length === 0 && unknownParts.length === 0 ? (
            <p>
              {category === "plugins"
                ? "No plugin sources reported in this session."
                : category === "tools"
                  ? "No tools reported in this session."
                  : "No connections reported in this session."}
            </p>
          ) : null}
          {rows.length > 0 && filtered.length === 0 ? <p>No matching {category}.</p> : null}
          <div className="inventory-capabilities-grid">
            <ul className="inventory-capabilities-list" aria-label={`Reported ${noun}`}>
              {filtered.map((row) => (
                <li key={row.key}>
                  <button
                    type="button"
                    aria-pressed={detail?.key === row.key}
                    onClick={() => setActive({ category, key: row.key, inventory })}
                  >
                    <strong>{row.name}</strong>
                    <small>{row.origin}</small>
                    <span>{row.server?.status ?? "Reported"}</span>
                  </button>
                </li>
              ))}
            </ul>
            {detail ? (
              <article className="inventory-capabilities-detail" aria-labelledby={detailId}>
                <span className="inventory-capabilities-badge">{detail.origin}</span>
                <h3 id={detailId}>{detail.name}</h3>
                <dl>
                  {Object.entries(evidence).map(([label, value]) => (
                    <div key={label}>
                      <dt>{label}</dt>
                      <dd>{value ?? "Not reported"}</dd>
                    </div>
                  ))}
                </dl>
                {detail.server ? (
                  <section aria-label="Reported server tools">
                    <h4>Server tools</h4>
                    {detail.server.tools_omitted ? (
                      <p>Tools omitted by provider</p>
                    ) : detail.server.tools === null ? (
                      <p>Tools not reported</p>
                    ) : detail.server.tools.length === 0 ? (
                      <p>No server tools reported</p>
                    ) : (
                      <ul>
                        {detail.server.tools.map((name, index) => (
                          // biome-ignore lint/suspicious/noArrayIndexKey: provider names can repeat in this read-only snapshot.
                          <li key={index}>{name}</li>
                        ))}
                      </ul>
                    )}
                  </section>
                ) : null}
              </article>
            ) : null}
          </div>
        </>
      ) : null}
    </section>
  );
}
