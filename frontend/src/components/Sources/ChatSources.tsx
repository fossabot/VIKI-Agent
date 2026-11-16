/**
 * "Files in this chat": every path VIKI changed or read, page it fetched, and search it ran this
 * session, grouped (Changed/Read/Web/Searches) and deduplicated by `chatDigest` (state/sources.ts)
 * — the same block walk `SourcesStrip.tsx` already does per turn, but folded across the whole
 * chat. A changed/read row opens through `onOpenCode` (a code chat) or `env.openNote` (a vault
 * note), and never from its own label text; a web/search row defers its open target to
 * `SourceChip`, which already treats the raw transcript text as display-only and resolves the
 * real target itself (the open-target rule `SourceChip.tsx` documents for itself).
 */
import { type ReactElement, useMemo, useState } from "react";
import { chatDigest, type DigestEntry, type DigestGroup, NOTE_PATH } from "../../state/sources";
import type { SessionView } from "../../state/view";
import { RailSection } from "../RightRail/RailSection";
import { SourceChip } from "./SourceChip";
import { type SourcesEnv, useSourcesEnv } from "./sourcesEnv";
import "./Sources.css";

export interface ChatSourcesProps {
  view: SessionView;
  /** Opens a changed/read path in the code workbench (a code chat); absent in a vault-only chat,
   * where a note path instead opens through `env.openNote`. */
  onOpenCode?: (path: string, withDiff: boolean) => void;
}

const TITLE = "Files in this chat";
const EMPTY_MESSAGE =
  "No files yet. Files VIKI changes or reads, pages it fetches and searches it runs in this chat show up here.";

const GROUPS: { key: DigestGroup; title: string }[] = [
  { key: "changed", title: "Changed" },
  { key: "read", title: "Read" },
  { key: "web", title: "Web" },
  { key: "search", title: "Searches" },
];

/** Rows a list shows before its "Show all N" reveals the rest. */
const VISIBLE_LIMIT = 5;

/** What every row below needs, threaded once instead of as four separate props. */
interface DigestCtx {
  vault: boolean;
  env: SourcesEnv;
  sessionId: string;
  onOpenCode: ((path: string, withDiff: boolean) => void) | undefined;
}

/** The label's last path segment, and the directory before it (no trailing slash) when there is
 * one — split at the last "/" or "\\", since a code path on Windows may use either. */
function splitLabel(label: string): { name: string; folder: string } {
  const cut = Math.max(label.lastIndexOf("/"), label.lastIndexOf("\\"));
  return cut === -1
    ? { name: label, folder: "" }
    : { name: label.slice(cut + 1), folder: label.slice(0, cut) };
}

/** A changed/read row's name-and-folder, wrapped in whatever can actually open it: `onOpenCode`
 * first (a code chat), then a vault note through `env.openNote` — else plain text. Never a button
 * built from `entry.label` alone; only a resolved workspace path (`entry.path`) ever opens. */
function FileCell({
  entry,
  group,
  ctx,
}: {
  entry: DigestEntry;
  group: DigestGroup;
  ctx: DigestCtx;
}): ReactElement {
  const { vault, env, onOpenCode } = ctx;
  const { name, folder } = splitLabel(entry.label);
  const content = (
    <>
      <span className="digest-name">{name}</span>
      {folder !== "" && <span className="digest-folder">{folder}</span>}
    </>
  );
  const path = entry.path;
  if (path && onOpenCode) {
    return (
      <button
        type="button"
        className="digest-open"
        title={path}
        onClick={() => onOpenCode(path, group === "changed")}
      >
        {content}
      </button>
    );
  }
  if (path && vault && NOTE_PATH.test(path)) {
    return (
      <button type="button" className="digest-open" title={path} onClick={() => env.openNote(path)}>
        {content}
      </button>
    );
  }
  return (
    <span className="digest-text" title={entry.label}>
      {content}
    </span>
  );
}

/** One entry's row: a file cell for Changed/Read, a `SourceChip` (or plain text, outside the
 * vault) for Web/Searches — then every row's shared `×count` and agent-list bits. */
function EntryRow({
  entry,
  group,
  ctx,
}: {
  entry: DigestEntry;
  group: DigestGroup;
  ctx: DigestCtx;
}): ReactElement {
  const isFile = group === "changed" || group === "read";
  return (
    <li className="digest-row">
      {isFile ? (
        <FileCell entry={entry} group={group} ctx={ctx} />
      ) : entry.source ? (
        <SourceChip source={entry.source} sessionId={ctx.sessionId} />
      ) : (
        <span className="digest-text">{entry.label}</span>
      )}
      {entry.count > 1 && <span className="digest-times">{`×${entry.count}`}</span>}
      {entry.agents.length > 0 && <span className="digest-agent">{entry.agents.join(", ")}</span>}
    </li>
  );
}

/** A group's own list, or its "By agents" fold's list: the first five, then a "Show all N" that
 * reveals the rest. */
function EntryList({
  entries,
  group,
  ctx,
}: {
  entries: DigestEntry[];
  group: DigestGroup;
  ctx: DigestCtx;
}): ReactElement {
  const [expanded, setExpanded] = useState(false);
  const visible = expanded ? entries : entries.slice(0, VISIBLE_LIMIT);
  return (
    <ul className="digest-list">
      {visible.map((entry) => (
        <EntryRow key={entry.key} entry={entry} group={group} ctx={ctx} />
      ))}
      {!expanded && entries.length > VISIBLE_LIMIT && (
        <li>
          <button type="button" className="btn txt sm" onClick={() => setExpanded(true)}>
            {`Show all ${entries.length}`}
          </button>
        </li>
      )}
    </ul>
  );
}

/** One group: its own entries (anything VIKI itself touched at least once), plus, when any
 * sighting was agent-only, a closed "By agents N" fold for those. */
function DigestGroupSection({
  group,
  title,
  entries,
  ctx,
}: {
  group: DigestGroup;
  title: string;
  entries: DigestEntry[];
  ctx: DigestCtx;
}): ReactElement {
  const main: DigestEntry[] = [];
  const agentOnly: DigestEntry[] = [];
  for (const entry of entries) (entry.viki ? main : agentOnly).push(entry);
  return (
    <details className="digest-group" open={group === "changed"}>
      <summary>
        {`${title} `}
        <span className="digest-count">{entries.length}</span>
      </summary>
      {main.length > 0 && <EntryList entries={main} group={group} ctx={ctx} />}
      {agentOnly.length > 0 && (
        <details className="digest-agents">
          <summary>{`By agents ${agentOnly.length}`}</summary>
          <EntryList entries={agentOnly} group={group} ctx={ctx} />
        </details>
      )}
    </details>
  );
}

export function ChatSources({ view, onOpenCode }: ChatSourcesProps): ReactElement | null {
  const env = useSourcesEnv();
  const vault = env ? env.inVault(view.sessionId) : false;
  const digest = useMemo(() => chatDigest(view.turns, vault), [view.turns, vault]);

  if (!env) return null;

  const groups = GROUPS.filter(({ key }) => digest[key].length > 0);
  const ctx: DigestCtx = { vault, env, sessionId: view.sessionId, onOpenCode };

  return (
    <RailSection title={TITLE} className="chat-sources">
      {groups.length === 0 ? (
        <p>{EMPTY_MESSAGE}</p>
      ) : (
        <div className="chat-digest">
          {groups.map(({ key, title }) => (
            <DigestGroupSection
              key={JSON.stringify([env.workspaceId, view.sessionId, key])}
              group={key}
              title={title}
              entries={digest[key]}
              ctx={ctx}
            />
          ))}
        </div>
      )}
    </RailSection>
  );
}
