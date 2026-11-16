/**
 * Publishes the active chat's `SourcesEnv` (open, peek, resolve, save-as-note) so every source
 * chip can read it through `useSourcesEnv()`. The caller supplies the active workspace's identity
 * and the app-level actions; this hook turns that into the capability object `sourcesEnv.ts`
 * expects, republishing only when the data that shapes its behaviour changes, and tears it down
 * again on unmount.
 */
import { useEffect, useMemo, useRef } from "react";
import type {
  ConversationSearchApi,
  ToolSource,
} from "../../clients/conversations/conversationSearchApi";
import { parseDisplayMetadata } from "../../state/noteMetadata";
import { type NotePeek, peekNote } from "../../state/notePeek";
import type { RootsSnapshot } from "../../state/sessionRoots";
import { noteLabel, turnSources } from "../../state/sources";
import type { VaultReadApi } from "../../state/vaultApi";
import { handoffText, type NoteDraft, noteDraft } from "./saveAsNote";
import { publishSourcesEnv, type SourcesEnv } from "./sourcesEnv";

export interface PublishSourcesInput {
  workspaceId: string | null;
  kind: "vault" | "scratch" | "code";
  chatOnly: boolean;
  roots: RootsSnapshot;
  /** Whether this build tracks session roots at all; when false, `roots.loaded` never settles
   * and `inVault` must not wait on it. */
  rootsTracked: boolean;
  vault?: VaultReadApi;
  search?: ConversationSearchApi;
  save: "editor" | "handoff" | null;
  openNote(ref: string): void;
  openFolder(folder: string): void;
  openUrl(url: string): void;
  openDraft(draft: NoteDraft): void;
  /** Opens a new chat draft asking VIKI to save the note; false at the editor cap ("no room"). */
  handoff(text: string): boolean;
  today?: string;
}

type ToolSourcesFn = NonNullable<ConversationSearchApi["toolSources"]>;

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** The user's local Y-M-D, the same construction as Workbench/Worktrees.tsx's `defaultBranch`:
 * `toISOString` reads UTC, which dates a late-night reply a day early for anyone east of
 * Greenwich. */
function localToday(): string {
  const now = new Date();
  return [now.getFullYear(), now.getMonth() + 1, now.getDate()]
    .map((part, i) => String(part).padStart(i === 0 ? 4 : 2, "0"))
    .join("-");
}

/** The hover card's facts: `display_metadata`'s authored title when it parses, else the same stem
 * label the chip itself shows. Throws when the note's own text is missing — the one fact
 * `peekNote` cannot do without — so a malformed response surfaces as "Couldn't read this note."
 * rather than a half-built card. */
function parsePeek(raw: Record<string, unknown>, ref: string): NotePeek {
  const note = raw.note;
  if (!isPlainObject(note) || typeof note.text !== "string") throw new Error("invalid_response");
  const frontmatter = isPlainObject(note.frontmatter)
    ? (note.frontmatter as Record<string, string>)
    : {};
  const displayMetadata = parseDisplayMetadata(note.display_metadata);
  return peekNote(note.text, frontmatter, displayMetadata?.display_title ?? noteLabel(ref));
}

/** `toolSources`, capped at 16 ids with a fresh request id, mapped by `tool_use_id`. */
async function resolveToolSources(
  toolSources: ToolSourcesFn,
  sid: string,
  ids: readonly string[],
  signal?: AbortSignal,
): Promise<ReadonlyMap<string, ToolSource>> {
  const result = await toolSources(sid, ids.slice(0, 16), crypto.randomUUID(), signal);
  return new Map(result.items.map((item) => [item.tool_use_id, item]));
}

/** True only when this session's sources resolve against the active vault: the active workspace
 * is a vault, not chat-only, with a known id; session-root tracking is either off or has loaded;
 * and its recorded root is this workspace outside a worktree, or complete root coverage proves
 * this is an unrecorded legacy session. */
function computeInVault(
  kind: "vault" | "scratch" | "code",
  chatOnly: boolean,
  workspaceId: string | null,
  roots: RootsSnapshot,
  rootsTracked: boolean,
): (sid: string) => boolean {
  if (kind !== "vault" || chatOnly || workspaceId === null) return () => false;
  if (rootsTracked && !roots.loaded) return () => false;
  return (sid) => {
    const root = roots.roots.get(sid);
    return root
      ? root.workspace_id === workspaceId && root.worktree_id === null
      : !rootsTracked || roots.rootsComplete;
  };
}

/** Publishes the `SourcesEnv` every source chip reads. Rebuilt only on the data that shapes its
 * behaviour (`workspaceId`, `kind`, `chatOnly`, `roots`, `rootsTracked`, `save`, and whether
 * `vault`/`search` are supplied at all) or an operation's client ownership changes. Action
 * callbacks live in a ref so inline closures never force a republish. Publishes `null` on
 * unmount, and again just before a rebuilt env replaces the one just torn down. */
export function usePublishSources(input: PublishSourcesInput): void {
  const latest = useRef(input);
  latest.current = input;

  const { workspaceId, kind, chatOnly, roots, rootsTracked, save } = input;
  const vault = input.vault;
  const search = input.search;
  const toolSources = search?.toolSources;
  // Root/metadata refreshes may replace the env without replacing a pending action's owner.
  const owner = useMemo(
    () => ({ workspaceId, kind, chatOnly, save, vault, search, toolSources }),
    [workspaceId, kind, chatOnly, save, vault, search, toolSources],
  );
  const currentOwner = useRef<typeof owner | null>(owner);
  currentOwner.current = owner;
  useEffect(() => {
    currentOwner.current = owner;
    return () => {
      if (currentOwner.current === owner) currentOwner.current = null;
    };
  }, [owner]);

  const hasPeek = Boolean(input.vault);
  const hasResolve = Boolean(input.search?.toolSources);

  const env = useMemo<SourcesEnv>(() => {
    const inVault = computeInVault(kind, chatOnly, workspaceId, roots, rootsTracked);

    const next: SourcesEnv = {
      workspaceId: workspaceId ?? undefined,
      inVault,
      openNote: (ref) => latest.current.openNote(ref),
      openFolder: (folder) => latest.current.openFolder(folder),
      openUrl: (url) => latest.current.openUrl(url),
    };

    if (hasPeek) {
      next.peek = async (ref, signal) => {
        const vault = latest.current.vault;
        if (!vault || workspaceId === null) throw new Error("unavailable");
        return parsePeek(await vault.note(workspaceId, ref, signal), ref);
      };
    }

    if (hasResolve) {
      next.resolve = async (sid, ids, signal) => {
        const toolSources = latest.current.search?.toolSources;
        if (!toolSources) throw new Error("unavailable");
        return resolveToolSources(toolSources, sid, ids, signal);
      };
    }

    if (save !== null) {
      next.saveAsNote = async (sid, turn) => {
        const sourceRoot = roots.roots.get(sid);
        const sourceInVault = inVault(sid);
        const stillCurrent = (): boolean => {
          const current = latest.current;
          const root = current.roots.roots.get(sid);
          return (
            currentOwner.current === owner &&
            root?.workspace_id === sourceRoot?.workspace_id &&
            root?.worktree_id === sourceRoot?.worktree_id &&
            computeInVault(
              current.kind,
              current.chatOnly,
              current.workspaceId,
              current.roots,
              current.rootsTracked,
            )(sid) === sourceInVault
          );
        };
        if (!stillCurrent()) return "unavailable";
        const sources = turnSources(turn, sourceInVault);
        const ids = sources
          .filter((source) => source.kind === "search" || source.kind === "web")
          .map((source) => source.toolUseId);
        const resolveFn = next.resolve;
        let resolved: ReadonlyMap<string, ToolSource> = new Map();
        if (ids.length > 0 && resolveFn) {
          try {
            resolved = await resolveFn(sid, ids);
          } catch {
            resolved = new Map();
          }
        }
        if (!stillCurrent()) return "unavailable";
        if (save === "editor") {
          const today = latest.current.today ?? localToday();
          const draft = noteDraft(turn, sources, resolved, today);
          if (!draft) return "unavailable";
          latest.current.openDraft(draft);
          return "opened";
        }
        const text = handoffText(turn, sources, resolved);
        if (!text) return "unavailable";
        return latest.current.handoff(text) ? "opened" : "no_room";
      };
    }

    return next;
  }, [workspaceId, kind, chatOnly, roots, rootsTracked, save, hasPeek, hasResolve, owner]);

  useEffect(() => {
    publishSourcesEnv(env);
    return () => publishSourcesEnv(null);
  }, [env]);
}
