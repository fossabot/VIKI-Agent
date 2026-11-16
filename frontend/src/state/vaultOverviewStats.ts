/**
 * Pure derivations behind the vault Overview page. No
 * React, no I/O, no imports — `StatsNote` mirrors `OverviewNote` field-for-field
 * (frontend/src/state/vaultApi.ts) by value rather than by import, so this file compiles and
 * tests on its own.
 *
 * "Day" is a plain integer throughout: whole days since the Unix epoch, in UTC. A vault date
 * string and a real clock reading land in that same space via `dayNumber` and `todayNumber`
 * respectively, so the two compare with `===`/`-` and never fight a timezone. `recentGroups` and
 * `growthOf` take that integer as `today`. `ageText` instead takes the real `Date` it was given,
 * because its own inputs (`captured`, `modified`) can carry a time of day that a calendar-day
 * integer would throw away.
 */

export interface StatsNote {
  ref: string;
  state: "ready" | "absent" | "unsupported" | "truncated";
  title: string | null;
  type: string | null;
  status: string | null;
  confidence: string | null;
  tags: string[] | null;
  created: string | null;
  modified: string | null;
  summary: string | null;
  links: number | null;
  complete: boolean;
}

export const FOLDER_SECTIONS = {
  "00-inbox": { key: "inbox" },
  "10-projects": { key: "projects" },
  "20-areas": { key: "areas" },
  "30-knowledge": { key: "knowledge", defaultType: "note" },
  "40-sources": { key: "sources", defaultType: "source" },
  "50-maps": { key: "maps", defaultType: "moc" },
  "60-calendar": { key: "calendar" },
  "70-people": { key: "people" },
  kanban: { key: "kanban" },
} as const;

export interface ListingNote {
  ref: string;
  title: string;
  bytes: number;
  modified: number | undefined;
}

export interface OtherFolderGroup {
  folder: string;
  count: number;
  newest: number | null;
  partial: boolean;
}

const DAY_MS = 86_400_000;

/** A vault date is always "YYYY-MM-DD" (validated before it reaches here): sliced at fixed
 * offsets rather than split and parsed, since there is nothing left to validate. */
export function dayNumber(date: string): number {
  const year = Number(date.slice(0, 4));
  const month = Number(date.slice(5, 7));
  const day = Number(date.slice(8, 10));
  return Date.UTC(year, month - 1, day) / DAY_MS;
}

/** `date`'s own local calendar day, in the same integer space as `dayNumber`. */
export function todayNumber(date: Date): number {
  return Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()) / DAY_MS;
}

export function sectionOf(ref: string): "Knowledge" | "Source" | "Map" | null {
  if (ref.startsWith("30-knowledge/")) return "Knowledge";
  if (ref.startsWith("40-sources/")) return "Source";
  if (ref.startsWith("50-maps/")) return "Map";
  return null;
}

/** A title for a row whose frontmatter has none. `SKILL`/`README`/`index` (any case) name their
 * parent directory, not themselves, so the fallback borrows it — `research / SKILL.md`, rather than
 * the same bare "SKILL" for every skill in the vault. At the vault root there is no parent to
 * borrow, so the filename stands alone. */
export function fallbackTitle(ref: string): string {
  const parts = ref.split("/");
  const filename = parts[parts.length - 1] ?? "";
  const stem = filename.replace(/\.(md|markdown)$/i, "");
  if (!/^(skill|readme|index)$/i.test(stem)) return stem;
  const parent = parts[parts.length - 2];
  return parent ? `${parent} / ${filename}` : filename;
}

/** Group eligible listing refs by their exact first folder component. */
export function otherFolders(
  listing: readonly ListingNote[],
  partial: boolean,
): OtherFolderGroup[] {
  const groups = new Map<string, OtherFolderGroup>();
  for (const row of listing) {
    const slash = row.ref.indexOf("/");
    if (slash < 1) continue;
    const folder = row.ref.slice(0, slash);
    if (
      folder.startsWith(".") ||
      folder === "bot-memory" ||
      folder === "archive" ||
      folder === "90-archive" ||
      Object.hasOwn(FOLDER_SECTIONS, folder)
    )
      continue;
    let group = groups.get(folder);
    if (!group) {
      group = { folder, count: 0, newest: null, partial };
      groups.set(folder, group);
    }
    group.count += 1;
    const modified = row.modified ?? Number.NaN;
    if (Number.isFinite(modified) && modified > 0)
      group.newest = Math.max(group.newest ?? 0, modified);
  }
  return [...groups.values()].sort((a, b) => compareText(a.folder, b.folder));
}

/** The newest overview row with a usable authored summary, tied by exact ref. */
export function leadOf(rows: readonly StatsNote[]): StatsNote | null {
  return (
    rows
      .filter((row) => row.summary !== null && row.summary.trim() !== "" && noteDay(row) !== null)
      .sort((a, b) => (noteDay(b) ?? 0) - (noteDay(a) ?? 0) || compareText(a.ref, b.ref))[0] ?? null
  );
}

/** Knowledge notes whose listing file time is at least 30 elapsed days old. */
export function rediscover(listing: readonly ListingNote[], today: Date, n = 3): ListingNote[] {
  const cutoff = today.getTime() / 1000 - (30 * DAY_MS) / 1000;
  return listing
    .filter((row) => {
      const modified = row.modified;
      return (
        sectionOf(row.ref) === "Knowledge" &&
        modified !== undefined &&
        Number.isFinite(modified) &&
        modified > 0 &&
        modified <= cutoff
      );
    })
    .sort((a, b) => (a.modified ?? 0) - (b.modified ?? 0) || compareText(a.ref, b.ref))
    .slice(0, Math.max(0, n));
}

function displayTitle(row: StatsNote): string {
  return row.title ?? fallbackTitle(row.ref);
}

function compareText(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

export type RecentVerb = "added" | "updated";
export interface RecentItem {
  row: StatsNote;
  verb: RecentVerb;
}
export interface RecentGroup {
  day: number;
  label: string;
  items: RecentItem[];
}

/** A row's day for recency purposes: the later of `created`/`modified`, or null when neither is
 * set (an undated row takes no part in `recentGroups` or `datedCount`). */
function noteDay(row: StatsNote): number | null {
  const created = row.created !== null ? dayNumber(row.created) : null;
  const modified = row.modified !== null ? dayNumber(row.modified) : null;
  if (created === null && modified === null) return null;
  return Math.max(created ?? -Infinity, modified ?? -Infinity);
}

function dayLabel(day: number, today: number): string {
  if (day === today) return "Today";
  if (day === today - 1) return "Yesterday";
  return new Date(day * DAY_MS).toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}

/**
 * Dated rows, newest day first and by title within a day, folded after the first `limit` into
 * same-day groups. A row's day is the later of its two dates, so a row edited long after it was
 * created surfaces on the edit's day, not its birthday — and is labelled "updated" there rather
 * than "added", which is reserved for a row whose `created` date agrees with the group's day.
 */
export function recentGroups(
  rows: readonly StatsNote[],
  today: number,
  limit: number,
): RecentGroup[] {
  const dated = rows
    .map((row) => ({ row, day: noteDay(row) }))
    .filter((entry): entry is { row: StatsNote; day: number } => entry.day !== null)
    .sort((a, b) => b.day - a.day || compareText(displayTitle(a.row), displayTitle(b.row)))
    .slice(0, limit);

  const groups: RecentGroup[] = [];
  for (const { row, day } of dated) {
    const verb: RecentVerb =
      row.created !== null && dayNumber(row.created) === day ? "added" : "updated";
    const current = groups[groups.length - 1];
    if (current && current.day === day) current.items.push({ row, verb });
    else groups.push({ day, label: dayLabel(day, today), items: [{ row, verb }] });
  }
  return groups;
}

/** How many of `rows` carry a `created` or `modified` date — the universe `recentGroups` draws
 * its (possibly truncated) list from. */
export function datedCount(rows: readonly StatsNote[]): number {
  return rows.filter((row) => row.created !== null || row.modified !== null).length;
}

interface StatusBuckets {
  evergreen: number;
  draft: number;
  other: number;
  none: number;
  unreadable: number;
}
interface ConfidenceBuckets {
  high: number;
  medium: number;
  low: number;
  other: number;
  none: number;
}
export interface KnowledgeGrowth {
  total: number;
  status: StatusBuckets;
  confidence: ConfidenceBuckets;
}
export interface SourceGrowth {
  total: number;
  processed: number;
  unread: number;
  other: number;
}
export interface WeekBucket {
  start: number;
  count: number;
}
export interface Growth {
  knowledge: KnowledgeGrowth;
  sources: SourceGrowth;
  maps: { total: number };
  weeks: WeekBucket[];
  lastFourWeeks: number;
}

/** True for a row whose frontmatter itself could not be read — as opposed to one that read fine
 * but lost only its heading/summary to a later truncation, which keeps its real status. */
function isUnreadable(row: StatsNote): boolean {
  return (row.state === "unsupported" || row.state === "truncated") && row.status === null;
}

function statusBucket(row: StatsNote): keyof StatusBuckets {
  if (isUnreadable(row)) return "unreadable";
  if (row.status === "evergreen") return "evergreen";
  if (row.status === "draft") return "draft";
  if (row.status === null) return "none";
  return "other";
}

function confidenceBucket(row: StatsNote): keyof ConfidenceBuckets {
  if (row.confidence === "high") return "high";
  if (row.confidence === "medium") return "medium";
  if (row.confidence === "low") return "low";
  if (row.confidence === null) return "none";
  return "other";
}

/** The Monday that starts `day`'s week (epoch day 0 was a Thursday, so Monday is the day whose
 * `day % 7 === 4`): shifts `day` back onto it rather than forward, so "this week" always means
 * the week `day` itself falls in. */
function mondayStart(day: number): number {
  return day - ((day + 3) % 7);
}

function weeksOf(rows: readonly StatsNote[], today: number): WeekBucket[] {
  const currentStart = mondayStart(today);
  const starts = Array.from({ length: 12 }, (_, i) => currentStart - (11 - i) * 7);
  const counts = new Map(starts.map((start): [number, number] => [start, 0]));
  for (const row of rows) {
    if (row.created === null) continue;
    const start = mondayStart(dayNumber(row.created));
    const count = counts.get(start);
    if (count !== undefined) counts.set(start, count + 1);
  }
  return starts.map((start) => ({ start, count: counts.get(start) ?? 0 }));
}

/**
 * `knowledge`/`sources`/`maps` each scope to their own section (`sectionOf`); `weeks` and
 * `lastFourWeeks` count `created` across all of `rows` — the combined growth line the three
 * sections' totals feed into — in 12 Monday-start weeks, oldest first.
 */
export function growthOf(rows: readonly StatsNote[], today: number): Growth {
  const knowledgeRows = rows.filter((row) => sectionOf(row.ref) === "Knowledge");
  const sourceRows = rows.filter((row) => sectionOf(row.ref) === "Source");
  const mapRows = rows.filter((row) => sectionOf(row.ref) === "Map");

  const status: StatusBuckets = { evergreen: 0, draft: 0, other: 0, none: 0, unreadable: 0 };
  const confidence: ConfidenceBuckets = { high: 0, medium: 0, low: 0, other: 0, none: 0 };
  for (const row of knowledgeRows) {
    status[statusBucket(row)] += 1;
    confidence[confidenceBucket(row)] += 1;
  }

  const processed = sourceRows.filter((row) => row.status === "processed").length;
  const unread = sourceRows.filter((row) => row.status === "unread").length;
  const weeks = weeksOf(rows, today);

  return {
    knowledge: { total: knowledgeRows.length, status, confidence },
    sources: {
      total: sourceRows.length,
      processed,
      unread,
      other: sourceRows.length - processed - unread,
    },
    maps: { total: mapRows.length },
    weeks,
    lastFourWeeks: weeks.slice(-4).reduce((sum, week) => sum + week.count, 0),
  };
}

export interface TagCount {
  tag: string;
  count: number;
}

export function topTags(rows: readonly StatsNote[], n = 12): TagCount[] {
  const counts = new Map<string, number>();
  for (const row of rows) {
    for (const tag of row.tags ?? []) counts.set(tag, (counts.get(tag) ?? 0) + 1);
  }
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1] || compareText(a[0], b[0]))
    .slice(0, n)
    .map(([tag, count]) => ({ tag, count }));
}

/** Map notes, most-linked first (an absent link count sorts as 0 rather than first or last on the
 * luck of `undefined`), then by title. */
export function mapsOf(rows: readonly StatsNote[]): StatsNote[] {
  return rows
    .filter((row) => sectionOf(row.ref) === "Map")
    .sort(
      (a, b) => (b.links ?? 0) - (a.links ?? 0) || compareText(displayTitle(a), displayTitle(b)),
    );
}

/**
 * "today"/"yesterday"/"N days ago" (under two weeks)/"N weeks ago", for an inbox capture's age.
 * Prefers `captured`; falls back to `modified` when `captured` is absent or unparseable; "" when
 * neither parses. `today` before the item's own date — clock skew, or a future-dated capture —
 * reads as "today" rather than a negative count.
 */
export function ageText(captured: string | null, modified: string | null, today: Date): string {
  const parsed = captured !== null ? Date.parse(captured) : NaN;
  const ts = Number.isNaN(parsed) ? (modified !== null ? Date.parse(modified) : NaN) : parsed;
  if (Number.isNaN(ts)) return "";
  const wholeDays = Math.max(0, Math.floor((today.getTime() - ts) / DAY_MS));
  if (wholeDays === 0) return "today";
  if (wholeDays === 1) return "yesterday";
  if (wholeDays < 14) return `${wholeDays} days ago`;
  return `${Math.floor(wholeDays / 7)} weeks ago`;
}
