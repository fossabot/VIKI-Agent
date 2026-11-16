/**
 * The Composer's MCP servers button and its dialog: a session-scoped view onto the running chat's
 * MCP servers (src/clients/conversations/inventoryApi.ts), with per-server enable/disable and reconnect (src/clients/conversations/mcpServersApi.ts).
 * English-only, like the rest of the interface: a repo-wide test refuses Japanese UI text.
 *
 * The dialog loads on open and after every mutation; closing it aborts whatever GET is still in
 * flight (useEffect cleanup) and a mutation's own answer, once the dialog is gone, is dropped
 * rather than applied (`mountedRef`). A single `pending` flag disables every switch and Retry
 * button for the one request in flight at a time — the server itself is the source of truth for
 * each row's status, so there is nothing to gain from letting two changes race.
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
import {
  type McpServersApi,
  McpServersApiError,
  type McpServersErrorCode,
} from "../../clients/conversations/mcpServersApi";
import { t } from "../../i18n/t";
import { useModalFocus } from "../Dialog/useModalFocus";
import { Loading } from "../Loading/Loading";
import { McpSetup } from "../Mcp/McpSetup";
import "./McpServers.css";

const NOT_RUNNING_TEXT =
  "Claude Code is not running for this chat yet. Its MCP servers show here after you send a message.";

const STATUS_TEXT: Record<McpServerStatus, string> = {
  connected: "Connected",
  failed: "Failed",
  "needs-auth": "Needs authorization",
  pending: "Pending",
  disabled: "Disabled",
};

// src/clients/conversations/inventoryApi.ts's own reason vocabulary (ToolsInventory.tsx's REASON_TEXT, English half only).
const REASON_TEXT: Record<InventoryReason, string> = {
  not_observed: "No turn has ended yet.",
  absent: "The reply did not include this.",
  unrecognized: "The reply had an unexpected shape.",
  timeout: "The request timed out.",
  unavailable: "This is unavailable.",
};

function reasonText(reason: InventoryReason | undefined): string {
  return reason ? REASON_TEXT[reason] : REASON_TEXT.unavailable;
}

// One line of fixed copy per refusal a switch or Retry can get. A code outside this
// map (unauthorized, forbidden_origin, invalid_input, overloaded, not_found, cancelled,
// invalid_response, unavailable) falls back to the generic line below.
const MUTATION_ERROR_TEXT: Partial<Record<McpServersErrorCode, string>> = {
  not_running: "Claude Code is not running for this chat.",
  busy: "Available when this reply finishes.",
  unknown_server: "This chat's Claude Code does not list that server.",
  managed: "This server comes from a profile. Change it there.",
  not_approved: "Approve this server with /mcp in a terminal first.",
  blocked: "Your organization's policy blocks this server.",
  refused: "Claude Code did not change this server.",
  not_connected: "The server is on but did not connect. Try Retry.",
  timeout: "Claude Code did not answer in time. The change may still apply.",
};

const GENERIC_MUTATION_ERROR = "Something went wrong changing this server. Try again.";

function mutationErrorText(error: unknown): string {
  if (error instanceof McpServersApiError) {
    return MUTATION_ERROR_TEXT[error.code] ?? GENERIC_MUTATION_ERROR;
  }
  return GENERIC_MUTATION_ERROR;
}

// src/viki_agent/profiles/mcp_config.py's SERVER_PREFIX for the app's own servers (ToolsInventory.tsx's own constant).
const APP_SERVER_PREFIX = "viki_";

/** Plugin and app servers share the scope "dynamic"; the CLI's source and the name's own prefix
 * are what tell an app server apart (ToolsInventory.tsx's identical check). */
function isAppServer(row: McpServerRow): boolean {
  return row.source === "dynamic" && row.name.startsWith(APP_SERVER_PREFIX);
}

const SCOPE_LABEL: Record<string, string> = {
  claudeai: "claude.ai",
  user: "User",
  project: "Project",
  local: "Local",
  enterprise: "Enterprise",
};

function groupLabel(row: McpServerRow): string {
  if (isAppServer(row)) return "Profile";
  if (row.scope === null) return "Other";
  return SCOPE_LABEL[row.scope] ?? row.scope;
}

/** Rows grouped by `groupLabel`, each group's own members in arrival order, and the groups
 * themselves in the order their first member appeared — a plain `Map` already keeps both. */
function groupServers(rows: readonly McpServerRow[]): Array<[string, McpServerRow[]]> {
  const groups = new Map<string, McpServerRow[]>();
  for (const row of rows) {
    const label = groupLabel(row);
    const members = groups.get(label);
    if (members) members.push(row);
    else groups.set(label, [row]);
  }
  return [...groups.entries()];
}

interface ServerRowProps {
  row: McpServerRow;
  pending: boolean;
  running: boolean;
  error: string | undefined;
  onToggle: (enabled: boolean) => void;
  onRetry: () => void;
}

function ServerRow({
  row,
  pending,
  running,
  error,
  onToggle,
  onRetry,
}: ServerRowProps): ReactElement {
  return (
    <li className="mcp-server-row">
      <span className="mcp-server-name mono">{row.name}</span>
      <span className="mcp-server-status" data-status={row.status}>
        {STATUS_TEXT[row.status]}
      </span>
      {isAppServer(row) ? (
        <span className="mcp-server-managed">Managed by its profile</span>
      ) : (
        <input
          type="checkbox"
          role="switch"
          className="mcp-server-switch"
          aria-label={row.name}
          checked={row.status !== "disabled"}
          aria-checked={row.status !== "disabled"}
          disabled={pending || running}
          onChange={(event) => onToggle(event.target.checked)}
        />
      )}
      {row.status === "failed" ? (
        <button type="button" className="btn txt sm" disabled={pending} onClick={onRetry}>
          Retry
        </button>
      ) : null}
      {row.status === "needs-auth" ? (
        <p className="mcp-server-note">Sign in with /mcp in a terminal.</p>
      ) : null}
      {error ? (
        <p role="alert" className="mcp-server-error">
          {error}
        </p>
      ) : null}
    </li>
  );
}

type DialogLoad =
  | { kind: "idle" }
  | { kind: "pending" }
  | { kind: "ready"; inventory: ToolInventory }
  | { kind: "not-running" }
  | { kind: "failed" };

interface McpServersDialogProps {
  inventoryApi: InventoryApi;
  api: McpServersApi;
  sessionId: string | null;
  running: boolean;
  onClose: () => void;
}

function McpServersDialog({
  inventoryApi,
  api,
  sessionId,
  running,
  onClose,
}: McpServersDialogProps): ReactElement {
  const dialogRef = useRef<HTMLDivElement>(null);
  const filterRef = useRef<HTMLInputElement>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const mountedRef = useRef(true);
  // Kept current every render so a mutation's trailing runLoad can tell, after its await, whether
  // the dialog has since moved on to a different session (or none).
  const sessionIdRef = useRef(sessionId);
  sessionIdRef.current = sessionId;
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const [load, setLoad] = useState<DialogLoad>({ kind: "idle" });
  const [filter, setFilter] = useState("");
  const [pending, setPending] = useState(false);
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
  const [setup, setSetup] = useState(false);

  useModalFocus({ active: true, dialogRef, initialFocusRef: filterRef, onDismiss: onClose });

  // Anchored above the composer, with no backdrop of its own to catch an outside click.
  useEffect(() => {
    function onOutsideMouseDown(event: MouseEvent): void {
      if (dialogRef.current && !dialogRef.current.contains(event.target as Node)) {
        onCloseRef.current();
      }
    }
    window.addEventListener("mousedown", onOutsideMouseDown);
    return () => window.removeEventListener("mousedown", onOutsideMouseDown);
  }, []);

  const runLoad = useCallback(
    (sid: string) => {
      controllerRef.current?.abort();
      const controller = new AbortController();
      controllerRef.current = controller;
      // A reload keeps whatever is already on screen until the new answer lands; only a first
      // load (nothing ready yet) blanks the panel to the loading line.
      setLoad((previous) => (previous.kind === "ready" ? previous : { kind: "pending" }));
      inventoryApi.get(sid, controller.signal).then(
        (inventory) => {
          if (controller.signal.aborted) return;
          setLoad({ kind: "ready", inventory });
        },
        (error: unknown) => {
          if (controller.signal.aborted) return;
          if (error instanceof InventoryApiError && error.code === "not_running") {
            setLoad({ kind: "not-running" });
          } else {
            setLoad({ kind: "failed" });
          }
        },
      );
    },
    [inventoryApi],
  );

  useEffect(() => {
    // A new session starts clean: an old session's pending mutation or row errors are not its
    // business, and must not leave its switches locked or show its errors.
    setPending(false);
    setRowErrors({});
    if (sessionId === null) {
      controllerRef.current?.abort();
      setLoad({ kind: "idle" });
      return;
    }
    runLoad(sessionId);
  }, [sessionId, runLoad]);

  // Closing the dialog unmounts it (the button below holds `open` state): this abort is what
  // "closing aborts the GET" means in practice, on top of the per-call abort runLoad does for a
  // reload that supersedes an earlier one still in flight.
  useEffect(() => {
    mountedRef.current = true; // StrictMode's development remount runs the cleanup below once first
    return () => {
      mountedRef.current = false;
      controllerRef.current?.abort();
    };
  }, []);

  async function mutate(name: string, action: (sid: string) => Promise<void>): Promise<void> {
    if (pending || sessionId === null) return;
    const sid = sessionId;
    setPending(true);
    setRowErrors((previous) => {
      if (!(name in previous)) return previous;
      const next = { ...previous };
      delete next[name];
      return next;
    });
    let failure: string | null = null;
    try {
      await action(sid);
    } catch (error) {
      failure = mutationErrorText(error);
    }
    // A late answer applies only while the dialog still shows the session it was sent for: once
    // closed, or once the session has since moved on, there is nothing of this answer left to show.
    if (!mountedRef.current || sessionIdRef.current !== sid) return;
    if (failure !== null) {
      const message = failure;
      setRowErrors((previous) => ({ ...previous, [name]: message }));
    }
    setPending(false);
    runLoad(sid);
  }

  function handleToggle(row: McpServerRow, enabled: boolean): void {
    void mutate(row.name, (sid) => api.setEnabled(sid, row.name, enabled));
  }

  function handleRetry(row: McpServerRow): void {
    void mutate(row.name, (sid) => api.reconnect(sid, row.name));
  }

  function matchesFilter(row: McpServerRow): boolean {
    return row.name.toLowerCase().includes(filter.toLowerCase());
  }

  function renderGroups(servers: readonly McpServerRow[]): ReactElement {
    const sections = groupServers(servers)
      .map(([label, rows]) => [label, rows.filter(matchesFilter)] as const)
      .filter(([, rows]) => rows.length > 0);
    if (sections.length === 0) return <p role="status">No servers match.</p>;
    return (
      <>
        {sections.map(([label, rows]) => (
          <section key={label} className="mcp-servers-group">
            <h3>
              {label} ({rows.length})
            </h3>
            <ul>
              {rows.map((row) => (
                <ServerRow
                  key={row.name}
                  row={row}
                  pending={pending}
                  running={running}
                  error={rowErrors[row.name]}
                  onToggle={(enabled) => handleToggle(row, enabled)}
                  onRetry={() => handleRetry(row)}
                />
              ))}
            </ul>
          </section>
        ))}
      </>
    );
  }

  function body(): ReactElement {
    if (load.kind === "idle" || load.kind === "not-running") {
      return <p role="status">{NOT_RUNNING_TEXT}</p>;
    }
    if (load.kind === "pending") {
      return <Loading>Loading…</Loading>;
    }
    if (load.kind === "failed") {
      return <p role="status">The tool inventory is unavailable.</p>;
    }
    const { inventory } = load;
    if (inventory.mcp_servers === null) {
      return <p role="status">{reasonText(inventory.reasons.mcp_servers)}</p>;
    }
    if (inventory.mcp_servers.length === 0) {
      return <p role="status">This chat uses no MCP servers.</p>;
    }
    return renderGroups(inventory.mcp_servers);
  }

  return (
    <div
      ref={dialogRef}
      className="mcp-servers-dialog"
      role="dialog"
      aria-modal="true"
      aria-labelledby="mcp-servers-title"
      tabIndex={-1}
    >
      <div className="mcp-servers-dialog-head">
        <h2 id="mcp-servers-title">MCP servers</h2>
        <button
          type="button"
          className="btn txt sm mcp-servers-close"
          aria-label="Close"
          onClick={onClose}
        >
          <span aria-hidden="true">✕</span>
        </button>
      </div>
      {!setup ? (
        <div className="mcp-servers-toolbar">
          <input
            ref={filterRef}
            type="text"
            className="mcp-servers-filter"
            placeholder="Filter servers…"
            aria-label="Filter servers"
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
          />
          <button
            type="button"
            className="btn txt sm"
            disabled={sessionId === null || pending}
            onClick={() => {
              if (sessionId !== null) runLoad(sessionId);
            }}
          >
            Refresh
          </button>
        </div>
      ) : null}
      <div className="mcp-servers-list">{setup ? <McpSetup /> : body()}</div>
      <button type="button" className="btn alt sm" onClick={() => setSetup(!setup)}>
        {t(setup ? "mcp.setup.back" : "mcp.setup.title")}
      </button>
      {!setup ? (
        <p className="mcp-servers-note">
          {
            "Claude Code saves each choice for this chat's folder. New chats there, and /mcp in a terminal there, follow it."
          }
        </p>
      ) : null}
    </div>
  );
}

export interface McpServersButtonProps {
  inventoryApi?: InventoryApi;
  api?: McpServersApi;
  sessionId: string | null;
  running: boolean;
}

/** The trigger that sits before the model chip (Composer.tsx wires the position; this owns the
 * button and the dialog). Renders nothing without both APIs — Composer passes them only once a
 * chat is selected, so this never has to ask the question itself. */
export function McpServersButton({
  inventoryApi,
  api,
  sessionId,
  running,
}: McpServersButtonProps): ReactElement | null {
  const [open, setOpen] = useState(false);
  if (!inventoryApi || !api) return null;
  return (
    <>
      <button
        type="button"
        className="model-chip"
        aria-label="MCP servers"
        title="MCP servers"
        onClick={() => setOpen(true)}
      >
        MCP
      </button>
      {open ? (
        <McpServersDialog
          inventoryApi={inventoryApi}
          api={api}
          sessionId={sessionId}
          running={running}
          onClose={() => setOpen(false)}
        />
      ) : null}
    </>
  );
}
