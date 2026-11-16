import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import {
  type InventoryApi,
  InventoryApiError,
  type McpServerRow,
  type ToolInventory,
} from "../../clients/conversations/inventoryApi";
import { InventoryCapabilities } from "./InventoryCapabilities";

afterEach(cleanup);

const chats = [
  { sessionId: "alpha", title: "Alpha" },
  { sessionId: "beta", title: "Beta" },
] as const;
const server = (overrides: Partial<McpServerRow> = {}): McpServerRow => ({
  name: "workspace-connection",
  status: "connected",
  scope: "dynamic",
  source: "dynamic",
  server_name: null,
  server_version: null,
  egress: null,
  tools: [],
  tools_omitted: false,
  ...overrides,
});
const inventory = (overrides: Partial<ToolInventory> = {}): ToolInventory => ({
  observed_at: "2026-10-12T01:00:00Z",
  mcp_servers: [],
  mcp_tools: [],
  system_tools: [],
  skills: [],
  memory_files: [],
  agents: [],
  reasons: {},
  ...overrides,
});
function selectChat(id = "alpha"): void {
  fireEvent.change(screen.getByRole("combobox", { name: "Running chat" }), {
    target: { value: id },
  });
}
function deferred() {
  let resolve: (value: ToolInventory) => void = () => {};
  const promise = new Promise<ToolInventory>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

it("reads only the selected chat and shows its exact tool evidence with search counts", async () => {
  const get = vi.fn(() =>
    Promise.resolve(
      inventory({
        system_tools: ["Read"],
        mcp_tools: [{ name: "search-workspace", server: "workspace-connection", loaded: false }],
        mcp_servers: [server({ source: "project", scope: "local", status: "needs-auth" })],
      }),
    ),
  );
  render(<InventoryCapabilities api={{ get }} chats={chats} category="tools" />);
  expect(get).not.toHaveBeenCalled();
  selectChat();
  const row = await screen.findByRole("button", { name: /search-workspace/ });
  expect(get).toHaveBeenCalledWith("alpha", expect.any(AbortSignal));
  expect(screen.getByText(/2 of 2 reported tools/)).toBeTruthy();
  fireEvent.click(row);
  const detail = screen.getByRole("article", { name: "search-workspace" });
  expect(within(detail).getByText("project")).toBeTruthy();
  expect(within(detail).getByText("local")).toBeTruthy();
  expect(within(detail).getByText("needs-auth")).toBeTruthy();
  expect(within(detail).getByText("Not loaded")).toBeTruthy();
  fireEvent.change(screen.getByRole("searchbox", { name: "Search tools" }), {
    target: { value: "Read" },
  });
  expect(screen.queryByRole("button", { name: /search-workspace/ })).toBeNull();
  expect(screen.getByText(/1 of 2 reported tools/)).toBeTruthy();
  expect(screen.queryByRole("switch")).toBeNull();
});

it("requires explicit plugin source, not dynamic scope or a plugin-looking name", async () => {
  const get = vi.fn(() =>
    Promise.resolve(
      inventory({
        mcp_servers: [
          server(),
          server({ name: "plugin-looking", source: null }),
          server({ name: "actual-plugin", source: "plugin", scope: "project", status: "disabled" }),
        ],
      }),
    ),
  );
  render(<InventoryCapabilities api={{ get }} chats={chats} category="plugins" />);
  selectChat();
  fireEvent.click(await screen.findByRole("button", { name: /actual-plugin/ }));
  expect(screen.queryByRole("button", { name: /workspace-connection/ })).toBeNull();
  expect(screen.queryByRole("button", { name: /plugin-looking/ })).toBeNull();
  expect(screen.getByText(/1 of 1 reported plugin sources/)).toBeTruthy();
  expect(screen.getByText(/Source not reported for 1 connection/)).toBeTruthy();
  const detail = screen.getByRole("article", { name: "actual-plugin" });
  expect(within(detail).getByText("plugin")).toBeTruthy();
  expect(within(detail).getByText("project")).toBeTruthy();
  expect(within(detail).getByText("disabled")).toBeTruthy();
});

it("distinguishes unobserved connections from an observed empty list", async () => {
  const get = vi
    .fn()
    .mockResolvedValueOnce(
      inventory({ mcp_servers: null, reasons: { mcp_servers: "not_observed" }, observed_at: null }),
    )
    .mockResolvedValueOnce(inventory());
  render(<InventoryCapabilities api={{ get }} chats={chats} category="connectors" />);
  selectChat();
  expect(await screen.findByText("No turn has ended yet.")).toBeTruthy();
  expect(screen.getByText(/Connection count unavailable/)).toBeTruthy();
  expect(screen.queryByText("No connections reported in this session.")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Refresh inventory" }));
  expect(await screen.findByText("No connections reported in this session.")).toBeTruthy();
  expect(screen.getByText(/0 of 0 reported connections/)).toBeTruthy();
});

it("retains unknown tool-part reasons alongside the known list", async () => {
  const get = vi.fn(() =>
    Promise.resolve(
      inventory({
        system_tools: ["Read"],
        mcp_tools: null,
        reasons: { mcp_tools: "unrecognized" },
      }),
    ),
  );
  render(<InventoryCapabilities api={{ get }} chats={chats} category="tools" />);
  selectChat();
  expect(await screen.findByRole("button", { name: /Read/ })).toBeTruthy();
  expect(screen.getByText("MCP tools: The reply had an unexpected shape.")).toBeTruthy();
  expect(screen.getByText(/Partial observation/)).toBeTruthy();
});

it("shows a connection's exact strings as text and preserves omitted tools", async () => {
  const get = vi.fn(() =>
    Promise.resolve(
      inventory({
        mcp_servers: [
          server({
            name: "<img src=x onerror=bad>",
            source: "<project>",
            scope: null,
            tools_omitted: true,
            server_name: "Example",
            server_version: "1.2",
            egress: "remote",
            tools: ["hidden-tool"],
          }),
        ],
      }),
    ),
  );
  const { container } = render(
    <InventoryCapabilities api={{ get }} chats={chats} category="connectors" />,
  );
  selectChat();
  fireEvent.click(await screen.findByRole("button", { name: /<img src=x onerror=bad>/ }));
  const detail = screen.getByRole("article", { name: "<img src=x onerror=bad>" });
  expect(within(detail).getByText("<project>")).toBeTruthy();
  expect(within(detail).getByText("Not reported")).toBeTruthy();
  expect(within(detail).getByText("Tools omitted by provider")).toBeTruthy();
  expect(within(detail).getByText("Example")).toBeTruthy();
  expect(within(detail).getByText("1.2")).toBeTruthy();
  expect(within(detail).getByText("remote")).toBeTruthy();
  expect(screen.queryByText("hidden-tool")).toBeNull();
  expect(container.querySelector("img")).toBeNull();
});

it("aborts old selections and rejects late responses even if the API ignores abort", async () => {
  const first = deferred();
  const second = deferred();
  const get = vi.fn<InventoryApi["get"]>((id) => (id === "alpha" ? first.promise : second.promise));
  render(<InventoryCapabilities api={{ get }} chats={chats} category="tools" />);
  selectChat();
  const firstSignal = get.mock.calls[0]?.[1] as AbortSignal | undefined;
  selectChat("beta");
  expect(firstSignal?.aborted).toBe(true);
  await act(async () => {
    second.resolve(inventory({ system_tools: ["BetaTool"] }));
  });
  await screen.findByRole("button", { name: /BetaTool/ });
  await act(async () => {
    first.resolve(inventory({ system_tools: ["AlphaTool"] }));
  });
  expect(screen.queryByRole("button", { name: /AlphaTool/ })).toBeNull();
  expect(screen.getByRole("button", { name: /BetaTool/ })).toBeTruthy();
});

it("does not reuse a row selection across chats or refreshes with different source rows", async () => {
  const get = vi
    .fn<InventoryApi["get"]>()
    .mockResolvedValueOnce(
      inventory({
        mcp_servers: [
          server({ name: "AlphaFirst" }),
          server({ name: "AlphaSecond", source: "plugin" }),
        ],
      }),
    )
    .mockResolvedValueOnce(
      inventory({
        mcp_servers: [
          server({ name: "BetaFirst", source: "project" }),
          server({ name: "BetaSecond", source: "user" }),
        ],
      }),
    )
    .mockResolvedValueOnce(
      inventory({
        mcp_servers: [
          server({ name: "NewFirst", source: "local" }),
          server({ name: "NewSecond", source: "plugin" }),
        ],
      }),
    );
  render(<InventoryCapabilities api={{ get }} chats={chats} category="connectors" />);
  selectChat();
  fireEvent.click(await screen.findByRole("button", { name: /AlphaSecond/ }));
  expect(screen.getByRole("article", { name: "AlphaSecond" })).toBeTruthy();
  selectChat("beta");
  await screen.findByRole("button", { name: /BetaFirst/ });
  expect(screen.getByRole("article", { name: "BetaFirst" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: /BetaSecond/ }));
  fireEvent.click(screen.getByRole("button", { name: "Refresh inventory" }));
  await screen.findByRole("button", { name: /NewFirst/ });
  expect(screen.getByRole("article", { name: "NewFirst" })).toBeTruthy();
});

it("clears selected-chat evidence on removal and aborts its request", async () => {
  const first = deferred();
  const get = vi.fn<InventoryApi["get"]>(() => first.promise);
  const api = { get };
  const { rerender } = render(<InventoryCapabilities api={api} chats={chats} category="tools" />);
  selectChat();
  const signal = get.mock.calls[0]?.[1];
  rerender(<InventoryCapabilities api={api} chats={[chats[1]]} category="tools" />);
  expect(signal?.aborted).toBe(true);
  await act(async () => {
    first.resolve(inventory({ system_tools: ["RemovedTool"] }));
  });
  expect(screen.queryByRole("button", { name: /RemovedTool/ })).toBeNull();
  expect((screen.getByRole("combobox") as HTMLSelectElement).value).toBe("");
});

it("replaces API ownership without accepting the previous owner's late answer", async () => {
  const first = deferred();
  const second = deferred();
  const oldGet = vi.fn<InventoryApi["get"]>(() => first.promise);
  const newGet = vi.fn<InventoryApi["get"]>(() => second.promise);
  const { rerender } = render(
    <InventoryCapabilities api={{ get: oldGet }} chats={chats} category="tools" />,
  );
  selectChat();
  const signal = oldGet.mock.calls[0]?.[1];
  rerender(<InventoryCapabilities api={{ get: newGet }} chats={chats} category="tools" />);
  expect(signal?.aborted).toBe(true);
  expect(newGet).toHaveBeenCalledWith("alpha", expect.any(AbortSignal));
  await act(async () => {
    second.resolve(inventory({ system_tools: ["NewOwnerTool"] }));
  });
  await screen.findByRole("button", { name: /NewOwnerTool/ });
  await act(async () => {
    first.resolve(inventory({ system_tools: ["OldOwnerTool"] }));
  });
  expect(screen.queryByRole("button", { name: /OldOwnerTool/ })).toBeNull();
});

it("aborts unmount and distinguishes not-running refusal from other failures", async () => {
  const first = deferred();
  const get = vi.fn<InventoryApi["get"]>(() => first.promise);
  const { unmount } = render(
    <InventoryCapabilities api={{ get }} chats={chats} category="tools" />,
  );
  selectChat();
  const signal = get.mock.calls[0]?.[1];
  unmount();
  expect(signal?.aborted).toBe(true);
  await act(async () => {
    first.resolve(inventory());
  });
  render(
    <InventoryCapabilities
      api={{ get: () => Promise.reject(new InventoryApiError(409, "not_running")) }}
      chats={chats}
      category="tools"
    />,
  );
  selectChat();
  expect(await screen.findByText("This chat is not running.")).toBeTruthy();
});
