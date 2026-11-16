import { describe, expect, it } from "vitest";

import {
  emptyDraft,
  fieldIndex,
  MAX_MCP_SERVERS,
  type McpConfigurationV1,
  type McpServerV1,
  readConfiguration,
  writeConfiguration,
} from "./mcpServersModel";

const CONFIGURATION: McpConfigurationV1 = {
  servers: [
    {
      name: "context7",
      transport: "stdio",
      command: "npx",
      args: ["-y", "context7-mcp"],
      url: null,
      note: "tool docs",
    },
    {
      name: "search",
      transport: "http",
      command: null,
      args: [],
      url: "https://example.test/mcp",
      note: null,
    },
  ],
};

function nonNull<T>(value: T | null): T {
  if (value === null) throw new Error("expected a configuration, got null");
  return value;
}

describe("mcpServersModel", () => {
  it("admits at most eight mcp servers per profile", () => {
    expect(MAX_MCP_SERVERS).toBe(8);
  });

  it("starts each draft as stdio with every text field empty", () => {
    const first = emptyDraft();
    const second = emptyDraft();
    expect(first).toEqual({
      name: "",
      transport: "stdio",
      command: "",
      args: "",
      url: "",
      note: "",
    });
    expect(first).not.toBe(second);
  });

  it("reads records into drafts, null fields as empty strings", () => {
    const record = {
      name: "a",
      transport: "stdio" as const,
      command: null,
      args: ["x", "y"],
      url: null,
      note: null,
    };
    const drafts = readConfiguration({ servers: [record] });
    expect(drafts).toEqual([
      {
        name: "a",
        transport: "stdio",
        command: "",
        args: "x\ny",
        url: "",
        note: "",
        original: record,
      },
    ]);
  });

  it("rejects a configuration value that is not a plain object", () => {
    expect(readConfiguration(null)).toBeNull();
    expect(readConfiguration([CONFIGURATION])).toBeNull();
    expect(readConfiguration("tools.mcp_configuration")).toBeNull();
  });

  it("rejects a top level that holds more than the servers key", () => {
    expect(readConfiguration({ servers: [], extra: 1 })).toBeNull();
  });

  it("rejects a top level whose servers is not an array", () => {
    expect(readConfiguration({ servers: "stdio" })).toBeNull();
  });

  it("rejects a server that is not a plain object of exactly six fields", () => {
    expect(readConfiguration({ servers: [1] })).toBeNull();
    expect(
      readConfiguration({
        servers: [
          {
            name: "a",
            transport: "stdio",
            command: "c",
            args: [],
            url: null,
            note: null,
            schema: 1,
          },
        ],
      }),
    ).toBeNull();
  });

  it("rejects a server missing its note field", () => {
    expect(
      readConfiguration({
        servers: [
          {
            name: "a",
            transport: "stdio",
            command: "c",
            args: [],
            url: null,
          },
        ],
      }),
    ).toBeNull();
  });

  it("rejects an args list that holds a non-string", () => {
    expect(
      readConfiguration({
        servers: [
          {
            name: "a",
            transport: "stdio",
            command: "c",
            args: [1],
            url: null,
            note: null,
          },
        ],
      }),
    ).toBeNull();
  });

  it("rejects a transport that is neither stdio nor http", () => {
    expect(
      readConfiguration({
        servers: [
          {
            name: "a",
            transport: "sse",
            command: "c",
            args: [],
            url: null,
            note: null,
          },
        ],
      }),
    ).toBeNull();
  });

  it("reads an empty servers array as an empty draft list", () => {
    expect(readConfiguration({ servers: [] })).toEqual([]);
  });

  it("writes an empty draft list as no configuration", () => {
    expect(writeConfiguration([])).toBeNull();
  });

  it("writes a stdio draft with its typed command and no url", () => {
    const written = nonNull(
      writeConfiguration([
        {
          name: "a",
          transport: "stdio",
          command: "npx",
          args: "-y\ncontext7-mcp",
          url: "",
          note: "tool docs",
        },
      ]),
    );
    expect(written).toEqual({
      servers: [
        {
          name: "a",
          transport: "stdio",
          command: "npx",
          args: ["-y", "context7-mcp"],
          url: null,
          note: "tool docs",
        },
      ],
    });
  });

  it("splits args text on CRLF and drops the blank line to two args", () => {
    const written = nonNull(
      writeConfiguration([
        {
          name: "a",
          transport: "stdio",
          command: "c",
          args: "alpha\r\n\nbeta",
          url: "",
          note: "",
        },
      ]),
    );
    const stdio = written.servers[0];
    if (stdio === undefined) throw new Error("expected a stdio server");
    expect(stdio.args).toEqual(["alpha", "beta"]);
  });

  it("writes an http draft with a null command and its typed url", () => {
    const written = nonNull(
      writeConfiguration([
        {
          name: "w",
          transport: "http",
          command: "typed but ignored",
          args: "typed but ignored",
          url: "https://example.test/mcp",
          note: "",
        },
      ]),
    );
    expect(written.servers[0]).toEqual({
      name: "w",
      transport: "http",
      command: null,
      args: [],
      url: "https://example.test/mcp",
      note: null,
    });
  });

  it("writes an empty note as null and trims no other text", () => {
    const written = nonNull(
      writeConfiguration([
        {
          name: " spaced ",
          transport: "stdio",
          command: " cmd ",
          args: " a1 ",
          url: "",
          note: "",
        },
      ]),
    );
    expect(written.servers[0]).toEqual({
      name: " spaced ",
      transport: "stdio",
      command: " cmd ",
      args: [" a1 "],
      url: null,
      note: null,
    });
  });

  it("keeps the draft order and the record key order", () => {
    const written = nonNull(
      writeConfiguration([
        {
          name: "second",
          transport: "http",
          command: "",
          args: "",
          url: "https://second.test",
          note: "",
        },
        {
          name: "first",
          transport: "stdio",
          command: "c",
          args: "a",
          url: "",
          note: "",
        },
      ]),
    );
    expect(written.servers.map((server) => server.name)).toEqual(["second", "first"]);
    const first = written.servers[0];
    if (first === undefined) throw new Error("expected a server");
    expect(Object.keys(first)).toEqual(["name", "transport", "command", "args", "url", "note"]);
  });

  it("finds the server index and field of an element path", () => {
    expect(fieldIndex("tools.mcp_configuration.servers[0].name")).toEqual({
      index: 0,
      key: "name",
    });
  });

  it("keeps the server index for a nested args path", () => {
    expect(fieldIndex("tools.mcp_configuration.servers[2].args[3]")).toEqual({
      index: 2,
      key: "args",
    });
  });

  it("returns null for paths that are not an element field", () => {
    expect(fieldIndex("tools.mcp_configuration")).toBeNull();
    expect(fieldIndex("tools.mcp_configuration.servers")).toBeNull();
    expect(fieldIndex("tools.mcp_configuration.servers[1]")).toBeNull();
  });

  it("round-trips one stdio and one http server exactly", () => {
    expect(writeConfiguration(nonNull(readConfiguration(CONFIGURATION)))).toEqual(CONFIGURATION);
  });

  it("round-trips an untouched server with empty args and an empty note", () => {
    const configuration: McpConfigurationV1 = {
      servers: [
        {
          name: "empty-fields",
          transport: "stdio",
          command: "npx",
          args: [],
          url: null,
          note: "",
        },
      ],
    };
    expect(writeConfiguration(nonNull(readConfiguration(configuration)))).toEqual(configuration);
  });

  it("round-trips an untouched server holding a blank argument line", () => {
    const configuration: McpConfigurationV1 = {
      servers: [
        {
          name: "blank-arg",
          transport: "stdio",
          command: "npx",
          args: [""],
          url: null,
          note: "",
        },
      ],
    };
    expect(writeConfiguration(nonNull(readConfiguration(configuration)))).toEqual(configuration);
  });

  it("writes an untouched http server back byte for byte despite text in its hidden stdio fields", () => {
    // The stored key order is deliberately not the editor's: note first, name last.
    const stored: McpServerV1 = JSON.parse(
      '{"note":null,"url":"https://example.test/mcp","args":[],"command":null,"transport":"http","name":"search"}',
    );
    const [draft] = nonNull(readConfiguration({ servers: [stored] }));
    if (draft === undefined) throw new Error("expected a draft");
    const written = writeConfiguration([{ ...draft, command: "left over", args: "-y\nx" }]);
    expect(JSON.stringify(written)).toBe(JSON.stringify({ servers: [stored] }));
    expect(written?.servers[0]?.note).toBeNull();
  });

  it("writes an untouched stdio server back as stored despite text in its hidden url field", () => {
    const stored: McpServerV1 = JSON.parse(
      '{"note":"n","url":null,"args":["-y"],"command":"npx","transport":"stdio","name":"kept"}',
    );
    const [draft] = nonNull(readConfiguration({ servers: [stored] }));
    if (draft === undefined) throw new Error("expected a draft");
    const written = writeConfiguration([{ ...draft, url: "https://left.over" }]);
    expect(JSON.stringify(written)).toBe(JSON.stringify({ servers: [stored] }));
  });

  it("normalizes only the server whose field was edited", () => {
    const untouched: McpServerV1 = {
      name: "kept",
      transport: "stdio",
      command: "npx",
      args: [""],
      url: null,
      note: "",
    };
    const drafts = nonNull(readConfiguration({ servers: [untouched] }));
    const edited = drafts.map((draft) => ({ ...draft, name: "renamed" }));
    expect(writeConfiguration(edited)).toEqual({
      servers: [
        {
          name: "renamed",
          transport: "stdio",
          command: "npx",
          args: [],
          url: null,
          note: null,
        },
      ],
    });
  });

  it("keeps an untouched server when another is renamed or a server is added", () => {
    const kept: McpServerV1 = {
      name: "kept",
      transport: "stdio",
      command: "npx",
      args: [""],
      url: null,
      note: "",
    };
    const other: McpServerV1 = {
      name: "other",
      transport: "http",
      command: null,
      args: [],
      url: "https://example.test/mcp",
      note: null,
    };
    const [keptDraft, otherDraft] = nonNull(readConfiguration({ servers: [kept, other] }));
    if (keptDraft === undefined || otherDraft === undefined) throw new Error("expected two drafts");
    expect(writeConfiguration([keptDraft, { ...otherDraft, name: "renamed" }])?.servers[0]).toEqual(
      kept,
    );
    expect(writeConfiguration([keptDraft, otherDraft, emptyDraft()])?.servers[0]).toEqual(kept);
  });

  const STDIO_SERVER: McpServerV1 = {
    name: "kept",
    transport: "stdio",
    command: "npx",
    args: [""],
    url: null,
    note: "",
  };
  const HTTP_SERVER: McpServerV1 = {
    name: "other",
    transport: "http",
    command: null,
    args: [],
    url: "https://example.test/mcp",
    note: null,
  };

  it.each([
    ["name", STDIO_SERVER, { name: "renamed" }, { name: "renamed" }],
    ["transport", STDIO_SERVER, { transport: "http" as const }, { transport: "http" }],
    ["command", STDIO_SERVER, { command: "node" }, { command: "node" }],
    ["args", STDIO_SERVER, { args: "-y" }, { args: ["-y"] }],
    ["url", HTTP_SERVER, { url: "https://example.test/new" }, { url: "https://example.test/new" }],
    ["note", STDIO_SERVER, { note: "why" }, { note: "why" }],
  ])("an edit to %s writes the edited value", (_field, stored, patch, expected) => {
    const [draft] = nonNull(readConfiguration({ servers: [stored] }));
    if (draft === undefined) throw new Error("expected a draft");
    expect(writeConfiguration([{ ...draft, ...patch }])?.servers[0]).toMatchObject(expected);
  });
});
