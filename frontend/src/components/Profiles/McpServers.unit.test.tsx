import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useEffect, useState } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { ProfileApiError } from "../../clients/profiles/profileApi";
import { Field } from "../Field/Field";
import { McpServers } from "./McpServers";

afterEach(() => {
  cleanup();
});

const TWO_SERVERS: Record<string, unknown> = {
  servers: [
    { name: "alpha", transport: "stdio", command: "npx", args: ["-y"], url: null, note: null },
    {
      name: "beta",
      transport: "http",
      command: null,
      args: [],
      url: "https://example.test",
      note: null,
    },
  ],
};

const TWO_HTTP_SERVERS: Record<string, unknown> = {
  servers: [
    {
      name: "alpha",
      transport: "http",
      command: null,
      args: [],
      url: "https://example.test/alpha",
      note: null,
    },
    {
      name: "beta",
      transport: "http",
      command: null,
      args: [],
      url: "https://example.test/beta",
      note: null,
    },
  ],
};

function eightServers(): Record<string, unknown> {
  return {
    servers: Array.from({ length: 8 }, (_, index) => ({
      name: `s${index}`,
      transport: "stdio",
      command: "npx",
      args: [],
      url: null,
      note: null,
    })),
  };
}

it("adds a stdio server and emits its configuration", () => {
  const onChange = vi.fn();
  render(
    <McpServers
      value={null}
      onChange={onChange}
      disabled={false}
      error={null}
      evaluation="Not evaluated yet"
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Add server" }));
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "echo" } });
  fireEvent.change(screen.getByLabelText("Command"), { target: { value: "npx" } });
  fireEvent.change(screen.getByLabelText("Arguments, one per line"), {
    target: { value: "-y\nserver" },
  });
  fireEvent.change(screen.getByLabelText("Note"), { target: { value: "why" } });
  expect(onChange).toHaveBeenLastCalledWith({
    servers: [
      {
        name: "echo",
        transport: "stdio",
        command: "npx",
        args: ["-y", "server"],
        url: null,
        note: "why",
      },
    ],
  });
});

it("adds an http server and emits its configuration", () => {
  const onChange = vi.fn();
  render(
    <McpServers value={null} onChange={onChange} disabled={false} error={null} evaluation="x" />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Add server" }));
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "search" } });
  fireEvent.change(screen.getByLabelText("Transport"), { target: { value: "http" } });
  fireEvent.change(screen.getByLabelText("URL"), { target: { value: "https://example.test/mcp" } });
  expect(onChange).toHaveBeenLastCalledWith({
    servers: [
      {
        name: "search",
        transport: "http",
        command: null,
        args: [],
        url: "https://example.test/mcp",
        note: null,
      },
    ],
  });
});

it("removes a server, keeping the rest", () => {
  const onChange = vi.fn();
  render(
    <McpServers
      value={TWO_SERVERS}
      onChange={onChange}
      disabled={false}
      error={null}
      evaluation="x"
    />,
  );
  const removeButtons = screen.getAllByRole("button", { name: "Remove" });
  fireEvent.click(removeButtons[0] as HTMLElement);
  expect(onChange).toHaveBeenLastCalledWith({
    servers: [
      {
        name: "beta",
        transport: "http",
        command: null,
        args: [],
        url: "https://example.test",
        note: null,
      },
    ],
  });
});

const ONE_SERVER: Record<string, unknown> = {
  servers: [{ name: "alpha", transport: "stdio", command: "npx", args: [], url: null, note: null }],
};

it("removing the only server emits null", () => {
  const onChange = vi.fn();
  render(
    <McpServers
      value={ONE_SERVER}
      onChange={onChange}
      disabled={false}
      error={null}
      evaluation="x"
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Remove" }));
  expect(onChange).toHaveBeenLastCalledWith(null);
});

it("disables Add server at the eight-server cap", () => {
  render(
    <McpServers
      value={eightServers()}
      onChange={vi.fn()}
      disabled={false}
      error={null}
      evaluation="x"
    />,
  );
  expect((screen.getByRole("button", { name: "Add server" }) as HTMLButtonElement).disabled).toBe(
    true,
  );
});

it("disables every control when disabled", () => {
  render(
    <McpServers value={TWO_SERVERS} onChange={vi.fn()} disabled error={null} evaluation="x" />,
  );
  expect(screen.getByRole("group", { name: "MCP configuration" }).hasAttribute("disabled")).toBe(
    true,
  );
  expect((screen.getAllByLabelText("Name")[0] as HTMLInputElement).disabled).toBe(true);
  expect((screen.getAllByRole("button", { name: "Remove" })[0] as HTMLButtonElement).disabled).toBe(
    true,
  );
});

it("prevents Enter in a field from submitting its surrounding form", () => {
  const submit = vi.fn();
  render(
    <form
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <McpServers
        value={ONE_SERVER}
        onChange={vi.fn()}
        disabled={false}
        error={null}
        evaluation="x"
      />
    </form>,
  );
  const enter = fireEvent.keyDown(screen.getByLabelText("Name"), { key: "Enter", code: "Enter" });
  expect(enter).toBe(false);
  expect(submit).not.toHaveBeenCalled();
});

it("shows the evaluation text and stdio/http field visibility", () => {
  render(
    <McpServers
      value={TWO_SERVERS}
      onChange={vi.fn()}
      disabled={false}
      error={null}
      evaluation="supported (profile)"
    />,
  );
  expect(screen.getByText("supported (profile)")).not.toBeNull();
  expect(screen.getAllByLabelText("Command")).toHaveLength(1);
  expect(screen.getAllByLabelText("URL")).toHaveLength(1);
});

it("places a per-server error under its field, with aria-invalid and aria-describedby", () => {
  render(
    <McpServers
      value={TWO_HTTP_SERVERS}
      onChange={vi.fn()}
      disabled={false}
      error={{ field: "tools.mcp_configuration.servers[1].url", reason: "invalid_url" }}
      evaluation="x"
    />,
  );
  const [urlOne, urlTwo] = screen.getAllByLabelText("URL");
  const alert = screen.getByRole("alert");
  expect(alert.textContent).toBe(
    "Use https://, or http:// to 127.0.0.1, localhost or [::1], with no user name, query or fragment.",
  );
  expect(urlOne?.getAttribute("aria-invalid")).toBeNull();
  expect(urlTwo?.getAttribute("aria-invalid")).toBe("true");
  expect(urlTwo?.getAttribute("aria-describedby")).toBe(alert.id);
  // Only the erring field is marked, not its row's other fields.
  expect(document.querySelectorAll("[aria-invalid]")).toHaveLength(1);
});

it("shows an unrecognized reason as the fallback line", () => {
  render(
    <McpServers
      value={TWO_SERVERS}
      onChange={vi.fn()}
      disabled={false}
      error={{ field: "tools.mcp_configuration.servers[0].name", reason: "something_else" }}
      evaluation="x"
    />,
  );
  expect(screen.getByRole("alert").textContent).toBe("This value is not valid.");
});

it("shows an empty stdio command as invalid_length's empty-or-too-long copy", () => {
  render(
    <McpServers
      value={TWO_SERVERS}
      onChange={vi.fn()}
      disabled={false}
      error={{ field: "tools.mcp_configuration.servers[0].command", reason: "invalid_length" }}
      evaluation="x"
    />,
  );
  expect(screen.getByRole("alert").textContent).toBe("This value is empty or too long.");
});

it("shows a secret-shaped argument as secret_value's plain-text copy", () => {
  render(
    <McpServers
      value={TWO_SERVERS}
      onChange={vi.fn()}
      disabled={false}
      error={{ field: "tools.mcp_configuration.servers[0].args[0]", reason: "secret_value" }}
      evaluation="x"
    />,
  );
  expect(screen.getByRole("alert").textContent).toBe(
    "This looks like a secret, so it is refused. Server definitions are plain text; keep secrets out of them.",
  );
});

it("shows a list-level error at the top, tied to no field", () => {
  render(
    <McpServers
      value={TWO_SERVERS}
      onChange={vi.fn()}
      disabled={false}
      error={{ field: "tools.mcp_configuration", reason: "invalid_count" }}
      evaluation="x"
    />,
  );
  expect(screen.getByRole("alert").textContent).toBe(
    "At most 8 servers, and at most 32 arguments each.",
  );
  for (const name of screen.getAllByLabelText("Name")) {
    expect(name.getAttribute("aria-invalid")).toBeNull();
  }
});

it("shows a non-conforming stored value as read-only JSON and blocks editing", () => {
  const bad = { servers: "not an array" };
  const { container } = render(
    <McpServers value={bad} onChange={vi.fn()} disabled={false} error={null} evaluation="x" />,
  );
  expect(
    screen.getByText(
      "This stored configuration does not match the server schema, so it blocks launch. Clear it to save this profile.",
    ),
  ).not.toBeNull();
  expect(container.querySelector("pre")?.textContent).toBe(JSON.stringify(bad, null, 2));
  expect(screen.queryByLabelText("Name")).toBeNull();
});

it.each([null, { servers: "not an array" }])(
  "associates the group title and hint for stored value %j",
  (value) => {
    const { container } = render(
      <Field
        label="MCP servers"
        hint="These servers are saved with the profile."
        error="Group error"
        group
      >
        {(control) => (
          <McpServers
            value={value}
            onChange={vi.fn()}
            disabled={false}
            error={null}
            evaluation="x"
            groupLabelledBy={control["aria-labelledby"]}
            groupDescribedBy={control["aria-describedby"]}
          />
        )}
      </Field>,
    );
    const group = screen.getByRole("group", { name: "MCP servers" });
    const title = screen.getByText("MCP servers");
    const hint = screen.getByText("These servers are saved with the profile.");
    const error = screen.getByText("Group error");
    expect(group.getAttribute("aria-labelledby")).toBe(title.id);
    expect(group.getAttribute("aria-describedby")).toBe(`${hint.id} ${error.id}`);
    expect(container.querySelectorAll("fieldset")).toHaveLength(1);
    expect(container.querySelector("label[for]")).toBeNull();
    const ids = [...container.querySelectorAll("[id]")].map((element) => element.id);
    expect(new Set(ids).size).toBe(ids.length);
  },
);

it("Clear is enabled only when a value is stored, and emits null", () => {
  const onChange = vi.fn();
  const { rerender } = render(
    <McpServers
      value={TWO_SERVERS}
      onChange={onChange}
      disabled={false}
      error={null}
      evaluation="x"
    />,
  );
  const clear = screen.getByRole("button", { name: "Clear" }) as HTMLButtonElement;
  expect(clear.disabled).toBe(false);
  fireEvent.click(clear);
  expect(onChange).toHaveBeenLastCalledWith(null);

  rerender(
    <McpServers value={null} onChange={onChange} disabled={false} error={null} evaluation="x" />,
  );
  expect((screen.getByRole("button", { name: "Clear" }) as HTMLButtonElement).disabled).toBe(true);
});

/** A stateful host, standing in for ProfilesView: it feeds the value onChange emits back into
 * McpServers as its next value prop, the way the real editor does. externalValue simulates a
 * value change the editor makes on its own (a profile switch), not one this component emitted. */
function Host({
  initial,
  externalValue,
}: {
  initial: Record<string, unknown> | null;
  externalValue?: Record<string, unknown> | null;
}) {
  const [value, setValue] = useState(initial);
  useEffect(() => {
    if (externalValue !== undefined) setValue(externalValue);
  }, [externalValue]);
  return (
    <McpServers value={value} onChange={setValue} disabled={false} error={null} evaluation="x" />
  );
}

it("keeps a typed trailing empty Arguments line, instead of reading the value back from the parent", () => {
  render(<Host initial={null} />);
  fireEvent.click(screen.getByRole("button", { name: "Add server" }));
  fireEvent.change(screen.getByLabelText("Arguments, one per line"), {
    target: { value: "a\n" },
  });
  expect((screen.getByLabelText("Arguments, one per line") as HTMLTextAreaElement).value).toBe(
    "a\n",
  );
});

it("keeps a stdio Command across a switch to http and back, instead of reading the value back from the parent", () => {
  render(<Host initial={null} />);
  fireEvent.click(screen.getByRole("button", { name: "Add server" }));
  fireEvent.change(screen.getByLabelText("Command"), { target: { value: "foo" } });
  fireEvent.change(screen.getByLabelText("Transport"), { target: { value: "http" } });
  fireEvent.change(screen.getByLabelText("Transport"), { target: { value: "stdio" } });
  expect((screen.getByLabelText("Command") as HTMLInputElement).value).toBe("foo");
});

it("clears every server row when the parent changes value to null outside onChange", () => {
  const { rerender } = render(<Host initial={TWO_SERVERS} />);
  expect(screen.getAllByLabelText("Name")).toHaveLength(2);
  rerender(<Host initial={TWO_SERVERS} externalValue={null} />);
  expect(screen.queryByLabelText("Name")).toBeNull();
});

function startImport(source = '{"mcpServers":{"echo":{"command":"example-mcp"}}}'): void {
  fireEvent.click(screen.getByText("Import MCP JSON"));
  fireEvent.change(screen.getByLabelText("MCP JSON"), { target: { value: source } });
  fireEvent.click(screen.getByRole("button", { name: "Add from JSON" }));
}

it("appends imported servers to the draft and preserves empty arguments", async () => {
  const onChange = vi.fn();
  const incoming = {
    servers: [
      {
        name: "echo",
        transport: "stdio",
        command: "example-mcp",
        args: ["a b", ""],
        url: null,
        note: null,
      },
    ],
  };
  const onImport = vi.fn().mockResolvedValue(incoming);
  render(
    <McpServers
      value={ONE_SERVER}
      onChange={onChange}
      onImport={onImport}
      disabled={false}
      error={null}
      evaluation="x"
    />,
  );
  await act(async () => startImport());
  expect(onChange).toHaveBeenCalledExactlyOnceWith({
    servers: [...(ONE_SERVER.servers as unknown[]), ...incoming.servers],
  });
  expect(screen.getByRole("status").textContent).toContain("save the profile");
  expect((screen.getByLabelText("MCP JSON") as HTMLTextAreaElement).value).toBe("");
  expect(onImport).toHaveBeenCalledWith(expect.any(String), expect.any(AbortSignal));
});

it.each([
  [ONE_SERVER, "already in the draft"],
  [eightServers(), "exceed 8 servers"],
])("refuses an import that conflicts with the draft", async (value, message) => {
  const onChange = vi.fn();
  render(
    <McpServers
      value={value}
      onChange={onChange}
      onImport={vi.fn().mockResolvedValue(ONE_SERVER)}
      disabled={false}
      error={null}
      evaluation="x"
    />,
  );
  await act(async () => startImport());
  expect(screen.getByRole("alert").textContent).toContain(message);
  expect(onChange).not.toHaveBeenCalled();
});

it("reports unsupported fields without changing the draft or displaying server error text", async () => {
  const onChange = vi.fn();
  const failure = new ProfileApiError(400, "invalid_document", {
    field: "tools.mcp_configuration.servers[0]",
    reason: "unsupported_import",
  });
  render(
    <McpServers
      value={ONE_SERVER}
      onChange={onChange}
      onImport={vi.fn().mockRejectedValue(failure)}
      disabled={false}
      error={null}
      evaluation="x"
    />,
  );
  await act(async () => startImport());
  expect(screen.getByRole("alert").textContent).toContain("Configure other fields");
  expect(screen.queryByRole("status")).toBeNull();
  expect(onChange).not.toHaveBeenCalled();
});

it("aborts an import when the configuration changes and ignores a late result", async () => {
  let finish!: (value: Record<string, unknown>) => void;
  const onImport = vi.fn(
    (_json: string, _signal?: AbortSignal) =>
      new Promise<Record<string, unknown>>((resolve) => {
        finish = resolve;
      }),
  );
  const onChange = vi.fn();
  const { rerender } = render(
    <McpServers
      value={ONE_SERVER}
      onChange={onChange}
      onImport={onImport}
      disabled={false}
      error={null}
      evaluation="x"
    />,
  );
  startImport();
  expect(screen.getByRole("group", { name: "MCP configuration" }).hasAttribute("disabled")).toBe(
    true,
  );
  rerender(
    <McpServers
      value={null}
      onChange={onChange}
      onImport={onImport}
      disabled={false}
      error={null}
      evaluation="x"
    />,
  );
  expect(onImport.mock.calls[0]?.[1]?.aborted).toBe(true);
  await act(async () => finish(TWO_SERVERS));
  expect(onChange).not.toHaveBeenCalled();
  expect(screen.queryByLabelText("Name")).toBeNull();
});
