// The pure model behind the profile editor's MCP server list: it turns the
// profile document's records into the line-oriented drafts the form edits,
// and turns them back, both directions strict. No React.

export const MAX_MCP_SERVERS = 8;

type Transport = "stdio" | "http";

export type McpServerV1 = {
  name: string;
  transport: Transport;
  command: string | null;
  args: string[];
  url: string | null;
  note: string | null;
};

// A type alias, not an interface, so a record stays assignable to the
// profile document's Record<string, unknown> slot.
export type McpConfigurationV1 = { servers: McpServerV1[] };

// One row of the form: every field is a plain string the user typed, and
// args holds one argument per line. original is the record readServer parsed
// this draft from: an untouched draft writes original back exactly as
// stored (key order included), while an edited draft is rebuilt from its
// typed fields, through the lossy string round trip (a blank args line, an
// empty note versus a null one).
export type McpServerDraft = {
  name: string;
  transport: Transport;
  command: string;
  args: string;
  url: string;
  note: string;
  original?: McpServerV1;
};

export function emptyDraft(): McpServerDraft {
  return {
    name: "",
    transport: "stdio",
    command: "",
    args: "",
    url: "",
    note: "",
  };
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

// The editable, string-typed fields readServer derives from a parsed record.
function toEditableFields(server: McpServerV1): Omit<McpServerDraft, "original"> {
  return {
    name: server.name,
    transport: server.transport,
    command: server.command === null ? "" : server.command,
    args: server.args.join("\n"),
    url: server.url === null ? "" : server.url,
    note: server.note === null ? "" : server.note,
  };
}

function readServer(value: unknown): McpServerDraft | null {
  if (!isPlainObject(value) || Object.keys(value).length !== 6) {
    return null;
  }
  const { name, transport, command, args, url, note } = value;
  if (typeof name !== "string") return null;
  if (transport !== "stdio" && transport !== "http") return null;
  if (command !== null && typeof command !== "string") return null;
  if (!Array.isArray(args) || !args.every((item) => typeof item === "string")) {
    return null;
  }
  if (url !== null && typeof url !== "string") return null;
  if (note !== null && typeof note !== "string") return null;
  const original = value as McpServerV1;
  return { ...toEditableFields(original), original };
}

// Strict on purpose: only a value holding exactly one key, servers, whose
// records are all well-formed is an MCP configuration at all; anything else
// is not this editor's concern and stays untouched.
export function readConfiguration(value: unknown): McpServerDraft[] | null {
  if (!isPlainObject(value) || Object.keys(value).length !== 1 || !Array.isArray(value.servers)) {
    return null;
  }
  const drafts: McpServerDraft[] = [];
  for (const server of value.servers) {
    const draft = readServer(server);
    if (draft === null) return null;
    drafts.push(draft);
  }
  return drafts;
}

// Back from the form to the document. An empty list means no configuration
// in the document, so it is null. Text is copied as typed: no trimming.
export function writeConfiguration(drafts: McpServerDraft[]): McpConfigurationV1 | null {
  if (drafts.length === 0) return null;
  const servers: McpServerV1[] = drafts.map((draft) => {
    if (draft.original !== undefined) {
      const editable = toEditableFields(draft.original);
      // Only the stored transport's own fields count: text left in the other
      // transport's hidden fields does not make the row dirty.
      const sameTransportFields =
        draft.transport === "http"
          ? editable.url === draft.url
          : editable.command === draft.command && editable.args === draft.args;
      if (
        editable.name === draft.name &&
        editable.transport === draft.transport &&
        sameTransportFields &&
        editable.note === draft.note
      ) {
        return draft.original;
      }
    }
    const note = draft.note === "" ? null : draft.note;
    // An edited or new record is rebuilt in the key order name, transport,
    // command, args, url, note; only an untouched record, returned above,
    // matches the stored bytes.
    if (draft.transport === "http") {
      return {
        name: draft.name,
        transport: "http",
        command: null,
        args: [],
        url: draft.url,
        note,
      };
    }
    return {
      name: draft.name,
      transport: "stdio",
      command: draft.command,
      args: draft.args.split(/\r?\n/).filter((line) => line !== ""),
      url: null,
      note,
    };
  });
  return { servers };
}

// Maps a changed-path string to the row and field the form must touch. The
// optional element index (args[3]) belongs to the field, not to the row.
const FIELD_PATH = /^tools\.mcp_configuration\.servers\[(\d+)\]\.([a-z_]+)(\[\d+\])?$/;

export function fieldIndex(field: string): { index: number; key: string } | null {
  const match = FIELD_PATH.exec(field);
  if (match === null) return null;
  const index = match[1];
  const key = match[2];
  if (index === undefined || key === undefined) return null;
  return { index: Number(index), key };
}
