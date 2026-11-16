import { type ReactNode, useState } from "react";
import { t, tf } from "../../i18n/t";
import type { OverviewNote, VaultOverview } from "../../state/vaultApi";
import {
  FOLDER_SECTIONS,
  growthOf,
  type ListingNote,
  otherFolders,
  sectionOf,
  todayNumber,
  topTags,
} from "../../state/vaultOverviewStats";
import { NoteCard, type NoteCardKind } from "./NoteCard";
import "./VaultSectionPage.css";

export type VaultSectionKey = "knowledge" | "sources" | "maps" | "else";

export interface VaultSectionListingRow {
  ref: string;
  title: string;
  modified?: number;
}

export interface VaultSectionPageProps {
  section: VaultSectionKey;
  listing: readonly VaultSectionListingRow[];
  overview: VaultOverview | null;
  listingPartial: boolean;
  /** False while the listing is still loading or its read failed. An unread listing is not an
   * empty one, so the page then states no count and no empty folder, and shows `listingStatus`
   * where the notes would be. */
  listingReady?: boolean;
  /** The listing's own loading or failure line, shown only while `listingReady` is false. */
  listingStatus?: ReactNode;
  onOpen: (ref: string, kind: NoteCardKind, origin: HTMLElement) => void;
  onBrowse: (folder: string) => void;
  onTag: (tag: string) => void;
}

const PAGE_SIZE = 32;

const COVERED_SECTIONS = {
  knowledge: {
    label: "vault.nav.knowledge",
    description: "vault.intro.knowledge",
    growth: "knowledge",
  },
  sources: {
    label: "vault.nav.sources",
    description: "vault.intro.sources",
    growth: "sources",
  },
  maps: {
    label: "vault.nav.maps",
    description: "vault.intro.maps",
    growth: "maps",
  },
} as const;

function fileTime(value: number | undefined): number | null {
  return value !== undefined && Number.isFinite(value) && value > 0 ? value : null;
}

function compareText(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

function compareRecent(a: VaultSectionListingRow, b: VaultSectionListingRow): number {
  const aTime = fileTime(a.modified);
  const bTime = fileTime(b.modified);
  if (aTime !== null && bTime !== null) return bTime - aTime || compareText(a.ref, b.ref);
  if (aTime !== null) return -1;
  if (bTime !== null) return 1;
  return compareText(a.ref, b.ref);
}

function countLabel(count: number, partial: boolean): string {
  return tf(
    count === 1
      ? partial
        ? "vault.count.at_least_one"
        : "vault.count.note"
      : partial
        ? "vault.count.at_least"
        : "vault.count.notes",
    { count },
  );
}

function latestByFolder(
  listing: readonly VaultSectionListingRow[],
): Map<string, VaultSectionListingRow> {
  const latest = new Map<string, VaultSectionListingRow>();
  for (const row of listing) {
    const slash = row.ref.indexOf("/");
    if (slash < 1) continue;
    const folder = row.ref.slice(0, slash);
    const current = latest.get(folder);
    if (!current || compareRecent(row, current) < 0) latest.set(folder, row);
  }
  return latest;
}

function changedDate(modified: number | null): string | null {
  if (modified === null) return null;
  const date = new Date(modified * 1000);
  return Number.isNaN(date.getTime()) ? null : date.toISOString().slice(0, 10);
}

function GrowthSlice({
  section,
  notes,
  partial,
}: {
  section: "knowledge" | "sources" | "maps";
  notes: readonly OverviewNote[];
  partial: boolean;
}) {
  const growth = growthOf(notes, todayNumber(new Date()));
  return (
    <section className="vsp-growth" aria-labelledby="vsp-growth-heading">
      <h2 id="vsp-growth-heading">Growth</h2>
      <p>
        {section === "knowledge"
          ? `${growth.knowledge.total} knowledge notes`
          : section === "sources"
            ? `${growth.sources.total} sources`
            : `${growth.maps.total} maps`}
      </p>
      {section === "knowledge" ? (
        <p className="vsp-meta">
          {`Status: ${growth.knowledge.status.evergreen} evergreen · ${growth.knowledge.status.draft} draft · ${growth.knowledge.status.other} other · ${growth.knowledge.status.none} not set · ${growth.knowledge.status.unreadable} unreadable`}
        </p>
      ) : null}
      {section === "sources" ? (
        <p className="vsp-meta">
          {`Status: ${growth.sources.processed} processed · ${growth.sources.unread} unread · ${growth.sources.other} other`}
        </p>
      ) : null}
      <p className="vsp-meta">
        {t("vault.coverage.source")}
        {partial ? ` · ${t("vault.coverage.partial")}` : ""}
      </p>
    </section>
  );
}

export function VaultSectionPage({
  section,
  listing,
  overview,
  listingPartial,
  listingReady = true,
  listingStatus,
  onOpen,
  onBrowse,
  onTag,
}: VaultSectionPageProps) {
  const [sort, setSort] = useState<"recent" | "name">("recent");
  const [page, setPage] = useState({ section, sort, count: PAGE_SIZE });
  const shown = page.section === section && page.sort === sort ? page.count : PAGE_SIZE;

  if (section === "else") {
    const listingNotes: ListingNote[] = listing.map((row) => ({
      ref: row.ref,
      title: row.title,
      bytes: 0,
      modified: row.modified,
    }));
    const groups = otherFolders(listingNotes, listingPartial);
    const latest = latestByFolder(listing);
    return (
      <main className="vsp">
        <header className="vsp-header">
          <h1>{t("vault.nav.else")}</h1>
          <p>{t("vault.intro.else")}</p>
        </header>
        {!listingReady ? (
          listingStatus
        ) : groups.length === 0 ? (
          <p className="vsp-empty">{t("vault.empty.else")}</p>
        ) : (
          <div className="vsp-groups">
            {groups.slice(0, shown).map((group) => {
              const newest = changedDate(group.newest);
              const latestRow = latest.get(group.folder);
              return (
                <section className="vsp-group" key={group.folder}>
                  <h2>{group.folder}</h2>
                  <code className="vsp-ref">{group.folder}/</code>
                  <p className="vsp-meta">{countLabel(group.count, group.partial)}</p>
                  {newest ? (
                    <p className="vsp-meta">{tf("vault.time.changed", { date: newest })}</p>
                  ) : null}
                  <p className="vsp-meta">{t("vault.listing.unread")}</p>
                  {latestRow ? (
                    <NoteCard
                      noteRef={latestRow.ref}
                      listing={{ title: latestRow.title, modified: latestRow.modified ?? 0 }}
                      variant="compact"
                      kind="row"
                      onOpen={onOpen}
                    />
                  ) : null}
                  <button
                    type="button"
                    className="vsp-action"
                    onClick={() => onBrowse(group.folder)}
                  >
                    {t("vault.action.show_all")}
                  </button>
                </section>
              );
            })}
          </div>
        )}
        {groups.length > shown ? (
          <button
            type="button"
            className="vsp-action"
            onClick={() => setPage({ section, sort, count: shown + PAGE_SIZE })}
          >
            {t("vault.action.show_more")}
          </button>
        ) : null}
      </main>
    );
  }

  const config = COVERED_SECTIONS[section];
  const folder = Object.keys(FOLDER_SECTIONS).find(
    (candidate) => FOLDER_SECTIONS[candidate as keyof typeof FOLDER_SECTIONS].key === section,
  );
  const folderPrefix = `${folder ?? ""}/`;
  const rows = listing.filter((row) => row.ref.startsWith(folderPrefix));
  const sorted = [...rows].sort(
    sort === "recent" ? compareRecent : (a, b) => compareText(a.ref, b.ref),
  );
  const sectionRows = (overview?.notes ?? []).filter(
    (note) =>
      sectionOf(note.ref) ===
      (section === "knowledge" ? "Knowledge" : section === "sources" ? "Source" : "Map"),
  );
  const topicRows = (overview?.notes ?? []).filter((note) => sectionOf(note.ref) !== null);
  const tags = overview ? topTags(topicRows) : [];

  return (
    <main className="vsp">
      <header className="vsp-header">
        <h1>{t(config.label)}</h1>
        <p>{t(config.description)}</p>
        <code className="vsp-ref">{folderPrefix}</code>
        {listingReady ? (
          <p className="vsp-count">{countLabel(rows.length, listingPartial)}</p>
        ) : null}
      </header>
      {overview ? (
        <GrowthSlice section={config.growth} notes={sectionRows} partial={overview.partial} />
      ) : (
        <p className="vsp-meta">{t("vault.card.unread")}</p>
      )}
      <section className="vsp-topics" aria-labelledby="vsp-topics-heading">
        <h2 id="vsp-topics-heading">{t("vault.block.topics")}</h2>
        <p className="vsp-meta">
          {overview
            ? `${t("vault.coverage.source")}${overview.partial ? ` · ${t("vault.coverage.partial")}` : ""}`
            : t("vault.card.unread")}
        </p>
        {tags.length > 0 ? (
          <ul className="vsp-tags">
            {tags.map((tag) => (
              <li key={tag.tag}>
                <button type="button" className="vsp-tag" onClick={() => onTag(tag.tag)}>
                  #{tag.tag} ({tag.count})
                </button>
              </li>
            ))}
          </ul>
        ) : overview ? (
          <p className="vsp-meta">No topics read.</p>
        ) : null}
      </section>
      <section className="vsp-notes" aria-labelledby="vsp-notes-heading">
        <div className="vsp-list-head">
          <h2 id="vsp-notes-heading">{t(config.label)}</h2>
          <label>
            {t("vault.sort.label")}
            <select
              value={sort}
              onChange={(event) => {
                const next = event.currentTarget.value === "name" ? "name" : "recent";
                setSort(next);
                setPage({ section, sort: next, count: PAGE_SIZE });
              }}
            >
              <option value="recent">{t("vault.sort.recent")}</option>
              <option value="name">{t("vault.sort.name")}</option>
            </select>
          </label>
        </div>
        {!listingReady ? (
          listingStatus
        ) : rows.length === 0 ? (
          <p className="vsp-empty">
            {listingPartial ? t("vault.empty.partial") : t("vault.empty.section")} ({folderPrefix})
          </p>
        ) : (
          <ul className="vsp-note-list" aria-label={`Notes in ${folder}`}>
            {sorted.slice(0, shown).map((row) => {
              const metadata = overview?.notes.find((note) => note.ref === row.ref);
              return (
                <li key={row.ref}>
                  <NoteCard
                    {...(metadata ? { overview: metadata } : {})}
                    noteRef={row.ref}
                    listing={{ title: row.title, modified: row.modified ?? 0 }}
                    variant="compact"
                    kind="row"
                    onOpen={onOpen}
                  />
                </li>
              );
            })}
          </ul>
        )}
        {sorted.length > shown ? (
          <button
            type="button"
            className="vsp-action"
            onClick={() => setPage({ section, sort, count: shown + PAGE_SIZE })}
          >
            {t("vault.action.show_more")}
          </button>
        ) : null}
      </section>
    </main>
  );
}
