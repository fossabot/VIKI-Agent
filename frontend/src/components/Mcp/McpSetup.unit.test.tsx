import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { McpSetup } from "./McpSetup";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("copies Atlassian setup for the chosen scope without executing it", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  vi.stubGlobal("navigator", { clipboard: { writeText } });
  const fetcher = vi.fn();
  vi.stubGlobal("fetch", fetcher);
  render(<McpSetup />);
  for (const scope of ["local", "user", "project"]) {
    fireEvent.change(screen.getByLabelText("Use this server in"), { target: { value: scope } });
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Copy Atlassian setup command" })),
    );
    expect(writeText).toHaveBeenLastCalledWith(
      `claude mcp add --transport http --scope ${scope} atlassian https://mcp.atlassian.com/v2/mcp`,
    );
  }
  expect(fetcher).not.toHaveBeenCalled();
  expect(screen.getByText(/open \/mcp/)).toBeTruthy();
  expect(screen.getByText(/Start a new VIKI chat/)).toBeTruthy();
});

it("shows the start folder as text with a copy button and the rules for .mcp.json", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  vi.stubGlobal("navigator", { clipboard: { writeText } });
  render(<McpSetup start={{ path: "D:\\vaults\\notes", chatOnly: false }} />);
  const region = screen.getByRole("region", { name: "Where to put .mcp.json" });
  expect(within(region).getByText("D:\\vaults\\notes").tagName).toBe("CODE");
  await act(async () =>
    fireEvent.click(within(region).getByRole("button", { name: "Copy folder path" })),
  );
  expect(writeText).toHaveBeenCalledWith("D:\\vaults\\notes");
  expect(within(region).getByText(/Claude Code doesn't read their \.mcp\.json/)).toBeTruthy();
  expect(within(region).getByText(/A chat in a code folder starts in that folder/)).toBeTruthy();
  expect(within(region).getByText(/start a new chat/)).toBeTruthy();
  expect(within(region).getByText(/writes nothing there/)).toBeTruthy();
  expect(within(region).getByText(/Servers named viki_/)).toBeTruthy();
});

it("says what to do in chat-only mode and when the folder is unknown", () => {
  const { rerender } = render(<McpSetup start={{ path: null, chatOnly: true }} />);
  expect(screen.getByText(/In chat-only mode/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Copy folder path" })).toBeNull();
  rerender(<McpSetup start={{ path: null, chatOnly: false }} />);
  expect(screen.getByText(/New chats start in the folder under Your vault/)).toBeTruthy();
});

it("has no where-to-put region without a start", () => {
  render(<McpSetup />);
  expect(screen.queryByRole("region", { name: "Where to put .mcp.json" })).toBeNull();
});

it("copies Claude-compatible HTTP and stdio JSON and names the project file", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  vi.stubGlobal("navigator", { clipboard: { writeText } });
  render(<McpSetup />);
  fireEvent.click(screen.getByText("Custom servers and JSON"));
  expect(screen.getByText(/use \.mcp\.json/)).toBeTruthy();
  await act(async () =>
    fireEvent.click(screen.getByRole("button", { name: "Copy HTTP server JSON" })),
  );
  expect(JSON.parse(writeText.mock.calls[0]?.[0])).toEqual({
    mcpServers: { "my-server": { type: "http", url: "https://example.com/mcp" } },
  });
  await act(async () =>
    fireEvent.click(screen.getByRole("button", { name: "Copy stdio server JSON" })),
  );
  expect(JSON.parse(writeText.mock.calls[1]?.[0])).toEqual({
    mcpServers: { "local-tools": { command: "example-mcp", args: ["--stdio"] } },
  });
});
