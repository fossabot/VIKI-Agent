/**
 * McpServersButton and its dialog: open/close, focus and grouped rows; a toggle sends the right
 * enabled value and the dialog re-reads; no session id and a 409 both read "not running yet";
 * Retry, the needs-auth copy, app-server rows and running disabling the switches; the filter,
 * an empty list and a null list; and an outside mousedown closing the dialog and returning
 * focus to its button while one inside it does not. More tests pin the request races: closing
 * aborts the in-flight GET, a mutation's answer arriving after close is dropped, and the same
 * answer arriving after the dialog has moved to a different session — gone to null, or
 * switched to a second session — must not act on the session it was sent for.
 */
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, expect, test, vi } from "vitest";
import {
  type InventoryApi,
  InventoryApiError,
  type McpServerRow,
  type ToolInventory,
} from "../../clients/conversations/inventoryApi";
import type { McpServersApi } from "../../clients/conversations/mcpServersApi";
import { McpServersButton } from "./McpServers";

afterEach(cleanup);

function server(overrides: Partial<McpServerRow> = {}): McpServerRow {
  return {
    name: "alpha",
    status: "connected",
    scope: "user",
    source: "user",
    server_name: null,
    server_version: null,
    egress: null,
    tools: null,
    tools_omitted: false,
    ...overrides,
  };
}

function inventory(overrides: Partial<ToolInventory> = {}): ToolInventory {
  return {
    observed_at: null,
    mcp_servers: [],
    mcp_tools: null,
    skills: null,
    memory_files: null,
    system_tools: null,
    agents: null,
    reasons: {},
    ...overrides,
  };
}

function fakeInventoryApi(get: InventoryApi["get"]): InventoryApi {
  return { get };
}

function fakeMcpApi(overrides: Partial<McpServersApi> = {}): McpServersApi {
  return {
    setEnabled: vi.fn(() => Promise.resolve()),
    reconnect: vi.fn(() => Promise.resolve()),
    ...overrides,
  };
}

function openDialog(): void {
  fireEvent.click(screen.getByRole("button", { name: "MCP servers" }));
}

test("offers setup before Claude Code is running without a server request", () => {
  const get = vi.fn(() => Promise.resolve(inventory()));
  render(
    <McpServersButton
      inventoryApi={fakeInventoryApi(get)}
      api={fakeMcpApi()}
      sessionId={null}
      running={false}
    />,
  );
  openDialog();
  fireEvent.click(screen.getByRole("button", { name: "Add an MCP server" }));
  expect(screen.getByRole("heading", { name: "Atlassian · Jira and Confluence" })).toBeTruthy();
  expect(screen.queryByText(/Claude Code saves each choice/)).toBeNull();
  expect(get).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Back to servers" }));
  expect(screen.getByRole("status").textContent).toContain("not running");
});

test("setup keyboard navigation skips collapsed examples and reaches expanded copy buttons", () => {
  render(
    <McpServersButton
      inventoryApi={fakeInventoryApi(vi.fn(() => Promise.resolve(inventory())))}
      api={fakeMcpApi()}
      sessionId={null}
      running={false}
    />,
  );
  openDialog();
  fireEvent.click(screen.getByRole("button", { name: "Add an MCP server" }));
  const examples = screen.getByRole("button", { name: "Custom servers and JSON" });
  const back = screen.getByRole("button", { name: "Back to servers" });
  screen.getByRole("button", { name: "Copy Atlassian setup command" }).focus();
  fireEvent.keyDown(window, { key: "Tab" });
  expect(document.activeElement).toBe(examples);
  fireEvent.keyDown(window, { key: "Tab" });
  expect(document.activeElement).toBe(back);
  fireEvent.click(examples);
  examples.focus();
  fireEvent.keyDown(window, { key: "Tab" });
  expect(document.activeElement).toBe(
    screen.getByRole("button", { name: "Copy HTTP server JSON" }),
  );
  fireEvent.keyDown(window, { key: "Tab" });
  expect(document.activeElement).toBe(
    screen.getByRole("button", { name: "Copy stdio server JSON" }),
  );
});

// Opening and closing ---------------------------------------------------------------------------

test("without either API nothing renders", () => {
  const { container } = render(<McpServersButton sessionId="s1" running={false} />);
  expect(container.firstChild).toBeNull();
});

test("opens a labelled dialog focused on the filter box, with grouped rows; Escape returns focus to the button", async () => {
  const get = vi.fn(() =>
    Promise.resolve(
      inventory({
        mcp_servers: [
          server({ name: "alpha", scope: "user" }),
          server({ name: "beta", scope: "claudeai" }),
        ],
      }),
    ),
  );
  render(
    <McpServersButton
      inventoryApi={fakeInventoryApi(get)}
      api={fakeMcpApi()}
      sessionId="s1"
      running={false}
    />,
  );
  const trigger = screen.getByRole("button", { name: "MCP servers" });
  trigger.focus();
  fireEvent.click(trigger);

  const dialog = screen.getByRole("dialog", { name: "MCP servers" });
  expect(within(dialog).getByRole("button", { name: "Close" })).toBeTruthy();
  const filterBox = within(dialog).getByPlaceholderText("Filter servers…");
  expect(document.activeElement).toBe(filterBox);
  expect(get).toHaveBeenCalledExactlyOnceWith("s1", expect.anything());

  expect(await within(dialog).findByText("User (1)")).toBeTruthy();
  expect(within(dialog).getByText("claude.ai (1)")).toBeTruthy();
  const alphaRow = within(dialog).getByText("alpha").closest("li") as HTMLElement;
  expect(within(alphaRow).getByText("Connected").getAttribute("data-status")).toBe("connected");
  expect(within(alphaRow).getByRole("switch")).toBeTruthy();

  fireEvent.keyDown(window, { key: "Escape" });
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(document.activeElement).toBe(trigger);
});

test("the close button closes the dialog", async () => {
  const get = vi.fn(() => Promise.resolve(inventory()));
  render(
    <McpServersButton
      inventoryApi={fakeInventoryApi(get)}
      api={fakeMcpApi()}
      sessionId="s1"
      running={false}
    />,
  );
  openDialog();
  const dialog = await screen.findByRole("dialog");
  fireEvent.click(within(dialog).getByRole("button", { name: "Close" }));
  expect(screen.queryByRole("dialog")).toBeNull();
});

// Turning a server on or off --------------------------------------------------------------------

test("turning a connected server off sends enabled:false, and the re-read shows Disabled", async () => {
  const get = vi.fn(() =>
    Promise.resolve(inventory({ mcp_servers: [server({ name: "alpha", status: "connected" })] })),
  );
  const setEnabled = vi.fn(() => Promise.resolve());
  render(
    <McpServersButton
      inventoryApi={fakeInventoryApi(get)}
      api={fakeMcpApi({ setEnabled })}
      sessionId="s1"
      running={false}
    />,
  );
  openDialog();
  await screen.findByText("alpha");
  get.mockImplementationOnce(() =>
    Promise.resolve(inventory({ mcp_servers: [server({ name: "alpha", status: "disabled" })] })),
  );

  fireEvent.click(screen.getByRole("switch", { name: "alpha" }));

  expect(setEnabled).toHaveBeenCalledExactlyOnceWith("s1", "alpha", false);
  expect(await screen.findByText("Disabled")).toBeTruthy();
  expect(get).toHaveBeenCalledTimes(2);
});

test("turning a disabled server on sends enabled:true", async () => {
  const get = vi.fn(() =>
    Promise.resolve(inventory({ mcp_servers: [server({ name: "alpha", status: "disabled" })] })),
  );
  const setEnabled = vi.fn(() => Promise.resolve());
  render(
    <McpServersButton
      inventoryApi={fakeInventoryApi(get)}
      api={fakeMcpApi({ setEnabled })}
      sessionId="s1"
      running={false}
    />,
  );
  openDialog();
  await screen.findByText("alpha");

  fireEvent.click(screen.getByRole("switch", { name: "alpha" }));

  expect(setEnabled).toHaveBeenCalledExactlyOnceWith("s1", "alpha", true);
});

// Without a running session ---------------------------------------------------------------------

test("no session id shows the not-running sentence and makes no request", async () => {
  const get = vi.fn();
  render(
    <McpServersButton
      inventoryApi={fakeInventoryApi(get)}
      api={fakeMcpApi()}
      sessionId={null}
      running={false}
    />,
  );
  openDialog();
  expect(
    await screen.findByText(
      "Claude Code is not running for this chat yet. Its MCP servers show here after you send a message.",
    ),
  ).toBeTruthy();
  expect(get).not.toHaveBeenCalled();
});

test("a 409 not_running from GET shows the same sentence", async () => {
  const get = vi.fn(() => Promise.reject(new InventoryApiError(409, "not_running")));
  render(
    <McpServersButton
      inventoryApi={fakeInventoryApi(get)}
      api={fakeMcpApi()}
      sessionId="s1"
      running={false}
    />,
  );
  openDialog();
  expect(
    await screen.findByText(
      "Claude Code is not running for this chat yet. Its MCP servers show here after you send a message.",
    ),
  ).toBeTruthy();
});

// Retry, needs-auth and app servers -------------------------------------------------------------

test("Retry shows only on the failed row and calls reconnect", async () => {
  const get = vi.fn(() =>
    Promise.resolve(
      inventory({
        mcp_servers: [
          server({ name: "alpha", status: "failed" }),
          server({ name: "beta", status: "connected" }),
        ],
      }),
    ),
  );
  const reconnect = vi.fn(() => Promise.resolve());
  render(
    <McpServersButton
      inventoryApi={fakeInventoryApi(get)}
      api={fakeMcpApi({ reconnect })}
      sessionId="s1"
      running={false}
    />,
  );
  openDialog();
  await screen.findByText("alpha");
  const alphaRow = screen.getByText("alpha").closest("li") as HTMLElement;
  const betaRow = screen.getByText("beta").closest("li") as HTMLElement;
  expect(within(alphaRow).getByRole("button", { name: "Retry" })).toBeTruthy();
  expect(within(betaRow).queryByRole("button", { name: "Retry" })).toBeNull();

  fireEvent.click(within(alphaRow).getByRole("button", { name: "Retry" }));

  expect(reconnect).toHaveBeenCalledExactlyOnceWith("s1", "alpha");
});

test("a needs-auth row reads the sign-in line", async () => {
  const get = vi.fn(() =>
    Promise.resolve(inventory({ mcp_servers: [server({ status: "needs-auth" })] })),
  );
  render(
    <McpServersButton
      inventoryApi={fakeInventoryApi(get)}
      api={fakeMcpApi()}
      sessionId="s1"
      running={false}
    />,
  );
  openDialog();
  expect(await screen.findByText("Sign in with /mcp in a terminal.")).toBeTruthy();
});

test("an app-server row is grouped under Profile, shows 'Managed by its profile', and has no switch", async () => {
  const get = vi.fn(() =>
    Promise.resolve(
      inventory({
        mcp_servers: [server({ name: "viki_echo", source: "dynamic", scope: "dynamic" })],
      }),
    ),
  );
  render(
    <McpServersButton
      inventoryApi={fakeInventoryApi(get)}
      api={fakeMcpApi()}
      sessionId="s1"
      running={false}
    />,
  );
  openDialog();
  expect(await screen.findByText("Profile (1)")).toBeTruthy();
  expect(screen.getByText("Managed by its profile")).toBeTruthy();
  expect(screen.queryByRole("switch")).toBeNull();
});

test("switches are disabled while running", async () => {
  const get = vi.fn(() => Promise.resolve(inventory({ mcp_servers: [server({ name: "alpha" })] })));
  render(
    <McpServersButton
      inventoryApi={fakeInventoryApi(get)}
      api={fakeMcpApi()}
      sessionId="s1"
      running={true}
    />,
  );
  openDialog();
  const toggle = (await screen.findByRole("switch", { name: "alpha" })) as HTMLInputElement;
  expect(toggle.disabled).toBe(true);
});

// Filter and empty lists ------------------------------------------------------------------------

test("the filter matches case-insensitively and hides groups it empties", async () => {
  const get = vi.fn(() =>
    Promise.resolve(
      inventory({
        mcp_servers: [
          server({ name: "Alpha", scope: "user" }),
          server({ name: "beta", scope: "claudeai" }),
        ],
      }),
    ),
  );
  render(
    <McpServersButton
      inventoryApi={fakeInventoryApi(get)}
      api={fakeMcpApi()}
      sessionId="s1"
      running={false}
    />,
  );
  openDialog();
  await screen.findByText("Alpha");

  fireEvent.change(screen.getByPlaceholderText("Filter servers…"), { target: { value: "ALP" } });

  expect(screen.getByText("Alpha")).toBeTruthy();
  expect(screen.getByText("User (1)")).toBeTruthy();
  expect(screen.queryByText("beta")).toBeNull();
  expect(screen.queryByText("claude.ai (1)")).toBeNull();
});

test("a filter matching nothing shows 'No servers match.'", async () => {
  const get = vi.fn(() => Promise.resolve(inventory({ mcp_servers: [server({ name: "alpha" })] })));
  render(
    <McpServersButton
      inventoryApi={fakeInventoryApi(get)}
      api={fakeMcpApi()}
      sessionId="s1"
      running={false}
    />,
  );
  openDialog();
  await screen.findByText("alpha");

  fireEvent.change(screen.getByPlaceholderText("Filter servers…"), { target: { value: "zzz" } });

  expect(await screen.findByText("No servers match.")).toBeTruthy();
});

test("an empty server list reads 'This chat uses no MCP servers.'", async () => {
  const get = vi.fn(() => Promise.resolve(inventory({ mcp_servers: [] })));
  render(
    <McpServersButton
      inventoryApi={fakeInventoryApi(get)}
      api={fakeMcpApi()}
      sessionId="s1"
      running={false}
    />,
  );
  openDialog();
  expect(await screen.findByText("This chat uses no MCP servers.")).toBeTruthy();
});

test("a null server list shows its reason", async () => {
  const get = vi.fn(() =>
    Promise.resolve(inventory({ mcp_servers: null, reasons: { mcp_servers: "timeout" } })),
  );
  render(
    <McpServersButton
      inventoryApi={fakeInventoryApi(get)}
      api={fakeMcpApi()}
      sessionId="s1"
      running={false}
    />,
  );
  openDialog();
  expect(await screen.findByText("The request timed out.")).toBeTruthy();
});

// Request races (sonnet-high rung) ---------------------------------------------------------------

test("closing the dialog aborts its in-flight GET", async () => {
  let capturedSignal: AbortSignal | undefined;
  const get = vi.fn((_sid: string, signal?: AbortSignal) => {
    capturedSignal = signal;
    return new Promise<ToolInventory>(() => {});
  });
  render(
    <McpServersButton
      inventoryApi={fakeInventoryApi(get)}
      api={fakeMcpApi()}
      sessionId="s1"
      running={false}
    />,
  );
  openDialog();
  const dialog = screen.getByRole("dialog");
  expect(capturedSignal?.aborted).toBe(false);

  fireEvent.click(within(dialog).getByRole("button", { name: "Close" }));

  expect(capturedSignal?.aborted).toBe(true);
});

test("a setEnabled answer that lands after the dialog closed is dropped: no re-read follows it", async () => {
  const get = vi.fn(() => Promise.resolve(inventory({ mcp_servers: [server({ name: "alpha" })] })));
  let resolveSetEnabled: () => void = () => {};
  const setEnabled = vi.fn(
    () =>
      new Promise<void>((resolve) => {
        resolveSetEnabled = resolve;
      }),
  );
  render(
    <McpServersButton
      inventoryApi={fakeInventoryApi(get)}
      api={fakeMcpApi({ setEnabled })}
      sessionId="s1"
      running={false}
    />,
  );
  openDialog();
  await screen.findByText("alpha");

  fireEvent.click(screen.getByRole("switch", { name: "alpha" }));
  expect(setEnabled).toHaveBeenCalledTimes(1);
  const dialog = screen.getByRole("dialog");
  fireEvent.click(within(dialog).getByRole("button", { name: "Close" }));
  expect(screen.queryByRole("dialog")).toBeNull();

  resolveSetEnabled();
  await Promise.resolve();
  await Promise.resolve();

  // Only the first, pre-close load happened; the dropped mutation never triggered a re-read.
  expect(get).toHaveBeenCalledTimes(1);
});

test("under StrictMode's development remount, turning a server off still re-reads and re-enables the switches", async () => {
  const get = vi.fn(() =>
    Promise.resolve(inventory({ mcp_servers: [server({ name: "alpha", status: "connected" })] })),
  );
  const setEnabled = vi.fn(() => Promise.resolve());
  render(
    <StrictMode>
      <McpServersButton
        inventoryApi={fakeInventoryApi(get)}
        api={fakeMcpApi({ setEnabled })}
        sessionId="s1"
        running={false}
      />
    </StrictMode>,
  );
  openDialog();
  await screen.findByText("alpha");
  get.mockImplementationOnce(() =>
    Promise.resolve(inventory({ mcp_servers: [server({ name: "alpha", status: "disabled" })] })),
  );

  fireEvent.click(screen.getByRole("switch", { name: "alpha" }));

  expect(await screen.findByText("Disabled")).toBeTruthy();
  const toggle = screen.getByRole("switch", { name: "alpha" }) as HTMLInputElement;
  expect(toggle.disabled).toBe(false);
});

test("a toggle that outlives the session going to null makes no further request and keeps the not-running sentence", async () => {
  const get = vi.fn(() =>
    Promise.resolve(inventory({ mcp_servers: [server({ name: "alpha", status: "connected" })] })),
  );
  let resolveSetEnabled: () => void = () => {};
  const setEnabled = vi.fn(
    () =>
      new Promise<void>((resolve) => {
        resolveSetEnabled = resolve;
      }),
  );
  const inventoryApi = fakeInventoryApi(get);
  const api = fakeMcpApi({ setEnabled });
  const { rerender } = render(
    <McpServersButton inventoryApi={inventoryApi} api={api} sessionId="s1" running={false} />,
  );
  openDialog();
  await screen.findByText("alpha");

  fireEvent.click(screen.getByRole("switch", { name: "alpha" }));
  expect(setEnabled).toHaveBeenCalledTimes(1);

  rerender(
    <McpServersButton inventoryApi={inventoryApi} api={api} sessionId={null} running={false} />,
  );
  expect(
    await screen.findByText(
      "Claude Code is not running for this chat yet. Its MCP servers show here after you send a message.",
    ),
  ).toBeTruthy();
  const callsBeforeResolve = get.mock.calls.length;

  resolveSetEnabled();
  await new Promise((resolve) => setTimeout(resolve, 0));

  expect(get).toHaveBeenCalledTimes(callsBeforeResolve);
  expect(
    screen.getByText(
      "Claude Code is not running for this chat yet. Its MCP servers show here after you send a message.",
    ),
  ).toBeTruthy();
});

test("a toggle that outlives a switch to another session leaves that session's own list showing", async () => {
  const s1Inventory = inventory({ mcp_servers: [server({ name: "alpha", status: "connected" })] });
  const s2Inventory = inventory({ mcp_servers: [server({ name: "gamma", status: "connected" })] });
  const get = vi.fn((sid: string) => Promise.resolve(sid === "s1" ? s1Inventory : s2Inventory));
  let resolveSetEnabled: () => void = () => {};
  const setEnabled = vi.fn(
    () =>
      new Promise<void>((resolve) => {
        resolveSetEnabled = resolve;
      }),
  );
  const inventoryApi = fakeInventoryApi(get);
  const api = fakeMcpApi({ setEnabled });
  const { rerender } = render(
    <McpServersButton inventoryApi={inventoryApi} api={api} sessionId="s1" running={false} />,
  );
  openDialog();
  await screen.findByText("alpha");

  fireEvent.click(screen.getByRole("switch", { name: "alpha" }));
  expect(setEnabled).toHaveBeenCalledExactlyOnceWith("s1", "alpha", false);

  rerender(
    <McpServersButton inventoryApi={inventoryApi} api={api} sessionId="s2" running={false} />,
  );
  expect(await screen.findByText("gamma")).toBeTruthy();

  resolveSetEnabled();
  await new Promise((resolve) => setTimeout(resolve, 0));

  expect(screen.getByText("gamma")).toBeTruthy();
  expect(screen.queryByText("alpha")).toBeNull();
  expect(get).toHaveBeenLastCalledWith("s2", expect.anything());
  expect(get.mock.calls.filter(([sid]) => sid === "s1")).toHaveLength(1);
  const toggle = screen.getByRole("switch", { name: "gamma" }) as HTMLInputElement;
  expect(toggle.disabled).toBe(false);
});

// Outside clicks --------------------------------------------------------------------------------

test("a mousedown outside the dialog closes it and returns focus to the MCP button", async () => {
  const get = vi.fn(() => Promise.resolve(inventory()));
  render(
    <McpServersButton
      inventoryApi={fakeInventoryApi(get)}
      api={fakeMcpApi()}
      sessionId="s1"
      running={false}
    />,
  );
  const trigger = screen.getByRole("button", { name: "MCP servers" });
  trigger.focus();
  fireEvent.click(trigger);
  await screen.findByRole("dialog");

  fireEvent.mouseDown(document.body);

  expect(screen.queryByRole("dialog")).toBeNull();
  expect(document.activeElement).toBe(trigger);
});

test("a mousedown inside the dialog leaves it open", async () => {
  const get = vi.fn(() => Promise.resolve(inventory({ mcp_servers: [server({ name: "alpha" })] })));
  render(
    <McpServersButton
      inventoryApi={fakeInventoryApi(get)}
      api={fakeMcpApi()}
      sessionId="s1"
      running={false}
    />,
  );
  openDialog();
  const panel = await screen.findByRole("dialog");

  fireEvent.mouseDown(panel);

  expect(screen.queryByRole("dialog")).toBeTruthy();
});
