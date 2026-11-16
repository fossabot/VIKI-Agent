import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import {
  type InventoryApi,
  InventoryApiError,
  type McpServerRow,
  type ToolInventory,
} from "../../clients/conversations/inventoryApi";
import { ToolsInventory, type ToolsInventoryChat } from "./ToolsInventory";

afterEach(cleanup);

const chat = (overrides: Partial<ToolsInventoryChat> = {}): ToolsInventoryChat => ({
  sessionId: "s1",
  title: "Alpha",
  ...overrides,
});

function inventory(overrides: Partial<ToolInventory> = {}): ToolInventory {
  return {
    observed_at: "2026-09-28T00:00:00.000Z",
    mcp_servers: [],
    mcp_tools: [{ name: "mcp__viki_echo__echo", server: "viki_echo", loaded: true }],
    skills: ["example-skill"],
    memory_files: [{ path: "%VAULT%/CLAUDE.md", type: "Project" }],
    system_tools: ["Read"],
    agents: [{ type: "Explore", source: "built-in" }],
    reasons: {},
    ...overrides,
  };
}

function fakeApi(get: InventoryApi["get"]): InventoryApi {
  return { get };
}

const server = (overrides: Partial<McpServerRow> = {}): McpServerRow => ({
  name: "viki_echo",
  status: "connected",
  scope: "user",
  source: "user",
  server_name: null,
  server_version: null,
  egress: null,
  tools: null,
  tools_omitted: false,
  ...overrides,
});

it("no running chat: the empty line, and no select", () => {
  render(<ToolsInventory api={fakeApi(vi.fn())} chats={[]} />);
  expect(screen.getByText("No running chat")).toBeTruthy();
  expect(screen.queryByRole("combobox")).toBeNull();
});

it("selecting a chat loads its inventory and lists the parts", async () => {
  const get = vi.fn(() => Promise.resolve(inventory()));
  render(<ToolsInventory api={fakeApi(get)} chats={[chat()]} />);
  expect(get).not.toHaveBeenCalled();
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "s1" } });
  expect(await screen.findByText("mcp__viki_echo__echo")).toBeTruthy();
  expect(screen.getByText("example-skill")).toBeTruthy();
  expect(screen.getByText("%VAULT%/CLAUDE.md (Project)")).toBeTruthy();
  expect(screen.getByText("Read")).toBeTruthy();
  expect(screen.getByText("Explore (built-in)")).toBeTruthy();
  expect(get).toHaveBeenCalledExactlyOnceWith("s1", expect.anything());
});

it("names the selected session and renders observation and source values as reported text", async () => {
  const observed = "<time>reported observation</time>";
  const get = vi.fn(async () =>
    inventory({ observed_at: observed, mcp_servers: [server({ source: "plugin" })] }),
  );
  const { container } = render(
    <ToolsInventory
      api={fakeApi(get)}
      chats={[chat(), chat({ sessionId: "s2", title: "Beta" })]}
    />,
  );
  expect(screen.getByRole("heading", { name: "Session inventory" })).toBeTruthy();
  expect(
    screen.getByText("Choose a running chat to inspect its reported tools and connections."),
  ).toBeTruthy();
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "s2" } });
  const context = within(
    await screen.findByRole("region", { name: "Session observation" }, { timeout: 50 }),
  );
  expect(context.getByRole("heading", { name: "Observed in this session" })).toBeTruthy();
  expect(context.getByText("Beta")).toBeTruthy();
  expect(context.getByText(`Observed: ${observed}`)).toBeTruthy();
  expect(screen.getByText("Source: plugin")).toBeTruthy();
  expect(container.querySelector("time")).toBeNull();
  expect(get).toHaveBeenCalledExactlyOnceWith("s2", expect.anything());
});

it("missing observation and server source stay distinct from an empty inventory", async () => {
  const get = vi.fn(async () =>
    inventory({
      observed_at: null,
      mcp_servers: [server({ source: null })],
      skills: null,
      reasons: { skills: "not_observed" },
    }),
  );
  render(<ToolsInventory api={fakeApi(get)} chats={[chat()]} />);
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "s1" } });
  expect(
    await screen.findByText("Observation time not reported", {}, { timeout: 50 }),
  ).toBeTruthy();
  expect(screen.getByText("Source not reported")).toBeTruthy();
  expect(screen.getByText("No turn has ended yet.")).toBeTruthy();
  expect(screen.getByText("Connected").parentElement?.textContent).toBe("Connected · user");
});

it("a null part shows its reason as text", async () => {
  const get = vi.fn(() =>
    Promise.resolve(inventory({ skills: null, reasons: { skills: "timeout" } })),
  );
  render(<ToolsInventory api={fakeApi(get)} chats={[chat()]} />);
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "s1" } });
  expect(await screen.findByText("The request timed out.")).toBeTruthy();
});

it("a 409 not_running shows its own line", async () => {
  const get = vi.fn(() => Promise.reject(new InventoryApiError(409, "not_running")));
  render(<ToolsInventory api={fakeApi(get)} chats={[chat()]} />);
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "s1" } });
  expect((await screen.findByRole("status")).textContent).toBe("This chat is not running.");
});

it("another failure shows the unavailable message", async () => {
  const get = vi.fn(() => Promise.reject(new Error("boom")));
  render(<ToolsInventory api={fakeApi(get)} chats={[chat()]} />);
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "s1" } });
  expect((await screen.findByRole("status")).textContent).toBe(
    "The tool inventory is unavailable.",
  );
});

it("Refresh reloads the selected chat", async () => {
  const get = vi.fn(() => Promise.resolve(inventory()));
  render(<ToolsInventory api={fakeApi(get)} chats={[chat()]} />);
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "s1" } });
  await screen.findByText("mcp__viki_echo__echo");
  expect(get).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  await vi.waitFor(() => expect(get).toHaveBeenCalledTimes(2));
});

it("Refresh is disabled with no chat selected", () => {
  render(<ToolsInventory api={fakeApi(vi.fn())} chats={[chat()]} />);
  expect((screen.getByRole("button", { name: "Refresh" }) as HTMLButtonElement).disabled).toBe(
    true,
  );
});

it.each([
  ["connected", "Connected"],
  ["failed", "Failed"],
  ["needs-auth", "Needs authorization"],
  ["pending", "Pending"],
  ["disabled", "Disabled"],
] as const)("a %s server shows its status chip and text", async (status, text) => {
  const get = vi.fn(() => Promise.resolve(inventory({ mcp_servers: [server({ status })] })));
  render(<ToolsInventory api={fakeApi(get)} chats={[chat()]} />);
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "s1" } });
  const chip = await screen.findByText(text);
  expect(chip.getAttribute("data-status")).toBe(status);
});

it("the recorded app server row reads as the app-server label", async () => {
  // The synthetic fixture's viki_echo row: scope and source both "dynamic".
  const get = vi.fn(() =>
    Promise.resolve(inventory({ mcp_servers: [server({ scope: "dynamic", source: "dynamic" })] })),
  );
  render(<ToolsInventory api={fakeApi(get)} chats={[chat()]} />);
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "s1" } });
  expect((await screen.findByText("Connected")).parentElement?.textContent).toBe(
    "Connected · app server",
  );
});

it.each([
  ["a plugin server", { name: "plugin:acme:search", scope: "dynamic", source: "plugin" }],
  ["an app-named server of another source", { scope: "dynamic", source: "plugin" }],
  ["a server from a CLI that sends no source", { scope: "dynamic", source: null }],
  ["a dynamic-sourced server without the app prefix", { name: "echo", source: "dynamic" }],
  [
    "a dynamic-sourced server with the app prefix inside its name",
    { name: "my_viki_x", source: "dynamic" },
  ],
])("%s shows its scope as sent, never the app-server label", async (_label, fields) => {
  const get = vi.fn(() =>
    Promise.resolve(inventory({ mcp_servers: [server({ scope: "dynamic", ...fields })] })),
  );
  render(<ToolsInventory api={fakeApi(get)} chats={[chat()]} />);
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "s1" } });
  expect((await screen.findByText("Connected")).parentElement?.textContent).toBe(
    "Connected · dynamic",
  );
  expect(screen.queryByText(/app server/)).toBeNull();
});

it("the agents part is labelled as custom agents", async () => {
  const get = vi.fn(() => Promise.resolve(inventory()));
  render(<ToolsInventory api={fakeApi(get)} chats={[chat()]} />);
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "s1" } });
  expect(await screen.findByRole("heading", { name: "Custom agents" })).toBeTruthy();
});

it.each(["needs-auth", "failed"] as const)("a %s server shows the way-on line", async (status) => {
  const get = vi.fn(() => Promise.resolve(inventory({ mcp_servers: [server({ status })] })));
  render(<ToolsInventory api={fakeApi(get)} chats={[chat()]} />);
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "s1" } });
  expect(await screen.findByText("Run /mcp in a terminal, or claude mcp.")).toBeTruthy();
});

it("a connected server shows no way-on line", async () => {
  const get = vi.fn(() =>
    Promise.resolve(inventory({ mcp_servers: [server({ status: "connected" })] })),
  );
  render(<ToolsInventory api={fakeApi(get)} chats={[chat()]} />);
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "s1" } });
  await screen.findByText("Connected");
  expect(screen.queryByText("Run /mcp in a terminal, or claude mcp.")).toBeNull();
});

it.each(["needs-auth", "failed"] as const)(
  "a %s app server shows the profile pointer and no /mcp line",
  async (status) => {
    const get = vi.fn(() =>
      Promise.resolve(
        inventory({ mcp_servers: [server({ status, scope: "dynamic", source: "dynamic" })] }),
      ),
    );
    render(<ToolsInventory api={fakeApi(get)} chats={[chat()]} />);
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "s1" } });
    expect(
      await screen.findByText("Open its profile in Agents → Profiles and check its MCP servers."),
    ).toBeTruthy();
    expect(screen.queryByText("Run /mcp in a terminal, or claude mcp.")).toBeNull();
  },
);

it("a failed plugin server keeps the /mcp way-on and shows no pointer", async () => {
  const get = vi.fn(() =>
    Promise.resolve(
      inventory({
        mcp_servers: [
          server({
            name: "plugin:acme:search",
            status: "failed",
            scope: "dynamic",
            source: "plugin",
          }),
        ],
      }),
    ),
  );
  render(<ToolsInventory api={fakeApi(get)} chats={[chat()]} />);
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "s1" } });
  expect(await screen.findByText("Run /mcp in a terminal, or claude mcp.")).toBeTruthy();
  expect(
    screen.queryByText("Open its profile in Agents → Profiles and check its MCP servers."),
  ).toBeNull();
});

it("a failed dynamic server without the app prefix keeps the /mcp way-on", async () => {
  const get = vi.fn(() =>
    Promise.resolve(
      inventory({
        mcp_servers: [
          server({ name: "echo", status: "failed", scope: "dynamic", source: "dynamic" }),
        ],
      }),
    ),
  );
  render(<ToolsInventory api={fakeApi(get)} chats={[chat()]} />);
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "s1" } });
  expect(await screen.findByText("Run /mcp in a terminal, or claude mcp.")).toBeTruthy();
  expect(
    screen.queryByText("Open its profile in Agents → Profiles and check its MCP servers."),
  ).toBeNull();
});

it("omitted tools shows its own text instead of the names", async () => {
  const get = vi.fn(() =>
    Promise.resolve(inventory({ mcp_servers: [server({ tools: [], tools_omitted: true })] })),
  );
  render(<ToolsInventory api={fakeApi(get)} chats={[chat()]} />);
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "s1" } });
  expect(await screen.findByText("tools omitted")).toBeTruthy();
});

it("tool names list when not omitted", async () => {
  const get = vi.fn(() =>
    Promise.resolve(inventory({ mcp_servers: [server({ tools: ["echo", "ping"] })] })),
  );
  render(<ToolsInventory api={fakeApi(get)} chats={[chat()]} />);
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "s1" } });
  expect(await screen.findByText("echo, ping")).toBeTruthy();
});

it("a server with no version renders only its name", async () => {
  const get = vi.fn(() =>
    Promise.resolve(
      inventory({ mcp_servers: [server({ server_name: "viki-echo", server_version: null })] }),
    ),
  );
  render(<ToolsInventory api={fakeApi(get)} chats={[chat()]} />);
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "s1" } });
  expect(await screen.findByText("viki-echo")).toBeTruthy();
});

it("an earlier chat's late answer never replaces a newer selection", async () => {
  let resolveAlpha: (value: ToolInventory) => void = () => {};
  let resolveBeta: (value: ToolInventory) => void = () => {};
  const alpha = new Promise<ToolInventory>((resolve) => {
    resolveAlpha = resolve;
  });
  const beta = new Promise<ToolInventory>((resolve) => {
    resolveBeta = resolve;
  });
  const get = vi.fn((sessionId: string) => (sessionId === "s1" ? alpha : beta));
  render(
    <ToolsInventory
      api={fakeApi(get)}
      chats={[chat(), chat({ sessionId: "s2", title: "Beta" })]}
    />,
  );
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "s1" } });
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "s2" } });
  await act(async () => {
    resolveBeta(inventory({ skills: ["beta-skill"] }));
    await Promise.resolve();
    await Promise.resolve();
  });
  await screen.findByText("beta-skill");
  await act(async () => {
    resolveAlpha(inventory({ skills: ["alpha-skill"] }));
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(screen.queryByText("alpha-skill")).toBeNull();
  expect(screen.getByText("beta-skill")).toBeTruthy();
});

it("a chat leaving chats clears the selection", async () => {
  const get = vi.fn(() => Promise.resolve(inventory()));
  const { rerender } = render(<ToolsInventory api={fakeApi(get)} chats={[chat()]} />);
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "s1" } });
  await screen.findByText("mcp__viki_echo__echo");
  rerender(
    <ToolsInventory api={fakeApi(get)} chats={[chat({ sessionId: "s2", title: "Beta" })]} />,
  );
  expect(screen.queryByText("mcp__viki_echo__echo")).toBeNull();
  expect((screen.getByRole("combobox") as HTMLSelectElement).value).toBe("");
});
