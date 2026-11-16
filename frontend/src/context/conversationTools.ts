import { type AssetsApi, AssetsApiError } from "../clients/media/assetsApi";
import { promptCut, rejoinPrompt, type SessionView } from "../state/view";
import { type Attachment, parseAttachments } from "../turnInput";

export const ARCHIVE_FORMAT_VERSION = 1;
export const ARCHIVE_MAX_BYTES = 8 * 1024 * 1024;
export const ARCHIVE_MAX_TURNS = 1_000;
export const ARCHIVE_MAX_TEXT_BYTES = 1 * 1024 * 1024;

export interface ConversationTarget {
  readonly sessionId: string;
  readonly turnId: string;
}

export interface QuoteDraft {
  readonly text: string;
  readonly source: ConversationTarget;
}

export function quoteTurn(target: ConversationTarget, text: string): QuoteDraft {
  const normalized = text.replace(/\r\n?/g, "\n");
  return {
    text: normalized
      .split("\n")
      .map((line) => `> ${line}`)
      .join("\n"),
    source: target,
  };
}

export interface ArchiveTurn extends ConversationTarget {
  readonly role: "user" | "assistant";
  readonly text: string;
  readonly createdAt?: string;
  readonly attachments?: readonly Attachment[];
}

export interface ReadOnlyConversationArchive {
  readonly formatVersion: 1;
  readonly kind: "viki-conversation-archive";
  readonly readOnly: true;
  readonly session: { readonly sessionId: string; readonly title: string };
  readonly turns: readonly ArchiveTurn[];
  readonly media?: readonly ArchiveMedia[];
  /** Set only by `selectedArchive` when a selected prompt is this window's cut copy. Never
   * written: both exports refuse such an archive rather than save part of a prompt as whole. */
  readonly promptCut?: true;
}

export type ArchiveMedia =
  | { readonly asset_id: string; readonly sha256: string; readonly base64: string }
  | { readonly asset_id: string; readonly unavailable: true };

export class ArchiveParseError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ArchiveParseError";
  }
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const ISO = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$/;
const utf8Bytes = (value: string): number => new TextEncoder().encode(value).byteLength;
const string = (value: unknown, field: string, max: number): string => {
  if (typeof value !== "string" || value.length === 0 || utf8Bytes(value) > max) {
    throw new ArchiveParseError(`invalid ${field}`);
  }
  return value;
};

export function parseReadOnlyArchive(input: unknown): ReadOnlyConversationArchive {
  if (!input || typeof input !== "object" || Array.isArray(input))
    throw new ArchiveParseError("archive must be an object");
  const root = input as Record<string, unknown>;
  if (
    root.format_version !== ARCHIVE_FORMAT_VERSION ||
    root.kind !== "viki-conversation-archive" ||
    root.read_only !== true
  )
    throw new ArchiveParseError("unsupported archive");
  const session = root.session;
  if (!session || typeof session !== "object" || Array.isArray(session))
    throw new ArchiveParseError("invalid session");
  const s = session as Record<string, unknown>;
  const sessionId = string(s.session_id, "session id", 128);
  if (!UUID.test(sessionId)) throw new ArchiveParseError("invalid session id");
  const title = s.title === "" ? "" : string(s.title, "title", 512);
  if (!Array.isArray(root.turns) || root.turns.length > ARCHIVE_MAX_TURNS)
    throw new ArchiveParseError("too many turns");
  const ids = new Set<string>();
  const turns = root.turns.map((raw) => {
    if (!raw || typeof raw !== "object" || Array.isArray(raw))
      throw new ArchiveParseError("invalid turn");
    const item = raw as Record<string, unknown>;
    const turnId = string(item.turn_id, "turn id", 160);
    const key = `${turnId}:${item.role}`;
    if (
      turnId.length > 160 ||
      Array.from(turnId).some((char) => char.charCodeAt(0) < 32) ||
      ids.has(key)
    )
      throw new ArchiveParseError("duplicate or invalid turn id");
    ids.add(key);
    if (item.role !== "user" && item.role !== "assistant")
      throw new ArchiveParseError("invalid role");
    const text = item.text === "" ? "" : string(item.text, "turn text", ARCHIVE_MAX_TEXT_BYTES);
    const createdAt =
      item.created_at === undefined ? undefined : string(item.created_at, "created date", 64);
    if (createdAt !== undefined && !ISO.test(createdAt))
      throw new ArchiveParseError("invalid created date");
    const attachments =
      item.attachments === undefined
        ? undefined
        : (() => {
            if (!Array.isArray(item.attachments) || item.attachments.length > 10)
              throw new ArchiveParseError("invalid attachments");
            const parsed = parseAttachments(item.attachments);
            if (!parsed) throw new ArchiveParseError("invalid attachments");
            return parsed;
          })();
    if (!text && !attachments?.length) throw new ArchiveParseError("empty turn");
    return {
      sessionId,
      turnId,
      role: item.role,
      text,
      ...(createdAt ? { createdAt } : {}),
      ...(attachments ? { attachments } : {}),
    } as ArchiveTurn;
  });
  const references = new Map(
    turns.flatMap((turn) => (turn.attachments ?? []).map((item) => [item.asset_id, item] as const)),
  );
  let media: ArchiveMedia[] | undefined;
  if (root.media !== undefined) {
    if (!Array.isArray(root.media) || root.media.length > 10)
      throw new ArchiveParseError("invalid media manifest");
    const seen = new Set<string>();
    media = root.media.map((raw): ArchiveMedia => {
      if (!raw || typeof raw !== "object") throw new ArchiveParseError("invalid media");
      const row = raw as Record<string, unknown>;
      const id = typeof row.asset_id === "string" ? row.asset_id : "";
      const ref = references.get(id);
      if (!ref || seen.has(id)) throw new ArchiveParseError("invalid media reference");
      seen.add(id);
      if (row.unavailable === true) return { asset_id: id, unavailable: true };
      if (
        typeof row.sha256 !== "string" ||
        !/^[0-9a-f]{64}$/.test(row.sha256) ||
        typeof row.base64 !== "string" ||
        !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(row.base64) ||
        row.base64.length !== Math.ceil(ref.byte_size / 3) * 4
      )
        throw new ArchiveParseError("invalid media bytes");
      return { asset_id: id, sha256: row.sha256, base64: row.base64 };
    });
    if (seen.size !== references.size) throw new ArchiveParseError("incomplete media manifest");
  }
  return {
    formatVersion: 1,
    kind: "viki-conversation-archive",
    readOnly: true,
    session: { sessionId, title },
    turns,
    ...(media ? { media } : {}),
  };
}

export function parseReadOnlyArchiveJson(json: string): ReadOnlyConversationArchive {
  if (utf8Bytes(json) > ARCHIVE_MAX_BYTES) throw new ArchiveParseError("archive is too large");
  try {
    return parseReadOnlyArchive(JSON.parse(json));
  } catch (error) {
    if (error instanceof ArchiveParseError) throw error;
    throw new ArchiveParseError("invalid archive JSON");
  }
}

function refuseCutPrompt(archive: ReadOnlyConversationArchive): void {
  if (archive.promptCut) throw new ArchiveParseError("a selected prompt was cut short");
}

export function exportConversationArchive(archive: ReadOnlyConversationArchive): string {
  refuseCutPrompt(archive);
  return JSON.stringify({
    format_version: 1,
    kind: "viki-conversation-archive",
    read_only: true,
    session: { session_id: archive.session.sessionId, title: archive.session.title },
    turns: archive.turns.map((turn) => ({
      turn_id: turn.turnId,
      role: turn.role,
      text: turn.text,
      ...(turn.createdAt ? { created_at: turn.createdAt } : {}),
      ...(turn.attachments ? { attachments: turn.attachments } : {}),
    })),
    media:
      archive.media ??
      [
        ...new Set(
          archive.turns.flatMap((turn) => (turn.attachments ?? []).map((item) => item.asset_id)),
        ),
      ].map((asset_id) => ({ asset_id, unavailable: true })),
  });
}

/** Only selected user text, assistant prose and explicit attachments leave this view. A selected
 * prompt this window holds only a cut copy of marks the archive, so every export refuses it. */
export function selectedArchive(
  view: SessionView,
  selected: ReadonlySet<string>,
  title: string,
): ReadOnlyConversationArchive {
  const turns: ArchiveTurn[] = [];
  let cut = false;
  for (const turn of view.turns) {
    if (!selected.has(turn.id) || turn.status === "running") continue;
    const target = { sessionId: view.sessionId, turnId: turn.id };
    cut ||= promptCut(turn);
    const prompt = turn.user ? rejoinPrompt(turn.user) : "";
    if (turn.user && (prompt || turn.user.attachments?.length))
      turns.push({
        ...target,
        role: "user",
        text: prompt,
        ...(turn.user.at && Number.isFinite(Date.parse(turn.user.at))
          ? { createdAt: new Date(turn.user.at).toISOString() }
          : {}),
        ...(turn.user.attachments?.length ? { attachments: turn.user.attachments } : {}),
      });
    const text = turn.blocks
      .flatMap((block) => (block.kind === "text" ? [block.text] : []))
      .join("\n\n");
    if (text) turns.push({ ...target, role: "assistant", text });
  }
  return {
    formatVersion: 1,
    kind: "viki-conversation-archive",
    readOnly: true,
    session: { sessionId: view.sessionId, title },
    turns,
    ...(cut ? { promptCut: true as const } : {}),
  };
}

export async function withArchiveMedia(
  archive: ReadOnlyConversationArchive,
  api: AssetsApi,
  signal?: AbortSignal,
): Promise<ReadOnlyConversationArchive> {
  const refs = new Map(
    archive.turns.flatMap((turn) =>
      (turn.attachments ?? []).map((item) => [item.asset_id, item] as const),
    ),
  );
  const media: ArchiveMedia[] = [];
  let estimatedBytes = utf8Bytes(exportConversationArchive(archive));
  for (const asset of refs.values()) {
    estimatedBytes += Math.ceil(asset.byte_size / 3) * 4 + 128;
    if (estimatedBytes > ARCHIVE_MAX_BYTES) throw new ArchiveParseError("archive is too large");
    try {
      const blob = await api.read(asset.asset_id, signal);
      if (blob.size !== asset.byte_size || blob.type !== asset.media_type)
        throw new ArchiveParseError("attachment changed");
      const buffer = await blob.arrayBuffer();
      const bytes = new Uint8Array(buffer);
      const sha256 = Array.from(
        new Uint8Array(await crypto.subtle.digest("SHA-256", buffer)),
        (byte) => byte.toString(16).padStart(2, "0"),
      ).join("");
      let binary = "";
      for (let offset = 0; offset < bytes.length; offset += 16_384)
        binary += String.fromCharCode(...bytes.subarray(offset, offset + 16_384));
      media.push({ asset_id: asset.asset_id, sha256, base64: btoa(binary) });
    } catch (error) {
      if (error instanceof AssetsApiError && [404, 410].includes(error.status))
        media.push({ asset_id: asset.asset_id, unavailable: true });
      else throw error;
    }
    if (utf8Bytes(exportConversationArchive({ ...archive, media })) > ARCHIVE_MAX_BYTES)
      throw new ArchiveParseError("archive is too large");
  }
  return { ...archive, media };
}

/**
 * Derives a stable archive id from a file's raw bytes: SHA-256, first 16 bytes, version nibble
 * set to 4 and variant bits set to `10` (RFC 4122-shaped), so importing the same file twice
 * always posts the same id and a retry is idempotent on the server.
 */
export async function deriveArchiveId(bytes: ArrayBuffer): Promise<string> {
  const id = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)).slice(0, 16);
  id[6] = ((id[6] ?? 0) & 0x0f) | 0x40;
  id[8] = ((id[8] ?? 0) & 0x3f) | 0x80;
  const hex = [...id].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export function exportConversationMarkdown(archive: ReadOnlyConversationArchive): string {
  refuseCutPrompt(archive);
  const lines = [
    `# ${archive.session.title}`,
    "",
    `> Read-only archive · ${archive.session.sessionId}`,
    "",
  ];
  for (const turn of archive.turns) {
    lines.push(`## ${turn.role === "user" ? "You" : "VIKI"}`, "", turn.text, "");
    if (turn.attachments?.length)
      lines.push(
        `Attachments: ${turn.attachments.map((item) => `${item.name} (${item.asset_id})`).join(", ")}`,
        "",
      );
  }
  return lines.join("\n");
}
