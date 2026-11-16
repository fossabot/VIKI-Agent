type StructureState = "ready" | "absent" | "unsupported" | "truncated";

export interface DisplayMetadata {
  state: StructureState;
  frontmatter_state: StructureState;
  heading_state: StructureState;
  display_title: string | null;
  title_source: "title" | "name" | "h1" | null;
  tags: string[] | null;
  type: string | null;
  status: string | null;
}

const KEYS = [
  "state",
  "frontmatter_state",
  "heading_state",
  "display_title",
  "title_source",
  "tags",
  "type",
  "status",
];
const STATES = ["ready", "absent", "unsupported", "truncated"];
const text = (value: unknown, maximum: number): value is string =>
  typeof value === "string" &&
  Array.from(value).length <= maximum &&
  !/[\p{Cc}\p{Cs}]/u.test(value) &&
  // Python's whitespace check preserves quoted U+FEFF, unlike JavaScript trim().
  /\P{White_Space}/u.test(value);

export function parseDisplayMetadata(value: unknown): DisplayMetadata | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const item = value as Record<string, unknown>;
  const keys = Object.keys(item);
  if (keys.length !== KEYS.length || keys.some((key) => !KEYS.includes(key))) return null;
  const frontmatter = item.frontmatter_state;
  const heading = item.heading_state;
  if (
    [item.state, frontmatter, heading].some(
      (state) => typeof state !== "string" || !STATES.includes(state),
    )
  )
    return null;
  const title = item.display_title;
  const source = item.title_source;
  const tags = item.tags;
  if (
    (title !== null && !text(title, 512)) ||
    (item.type !== null && !text(item.type, 64)) ||
    (item.status !== null && !text(item.status, 64)) ||
    (source !== null && source !== "title" && source !== "name" && source !== "h1") ||
    (title === null) !== (source === null)
  )
    return null;
  const resolved = frontmatter === "ready" || frontmatter === "absent";
  if (resolved) {
    if (
      !Array.isArray(tags) ||
      tags.length > 16 ||
      !tags.every((tag) => text(tag, 64)) ||
      new Set(tags).size !== tags.length
    )
      return null;
  } else if (
    tags !== null ||
    title !== null ||
    item.type !== null ||
    item.status !== null ||
    heading !== frontmatter
  )
    return null;
  if (
    ((source === "title" || source === "name") && frontmatter !== "ready") ||
    (source === "h1" && (!resolved || heading !== "ready")) ||
    (heading === "ready" && source === null) ||
    (frontmatter === "absent" &&
      (item.type !== null || item.status !== null || (Array.isArray(tags) && tags.length > 0)))
  )
    return null;
  const populated =
    title !== null ||
    item.type !== null ||
    item.status !== null ||
    (Array.isArray(tags) && tags.length > 0);
  const state = [frontmatter, heading].includes("unsupported")
    ? "unsupported"
    : [frontmatter, heading].includes("truncated")
      ? "truncated"
      : populated
        ? "ready"
        : "absent";
  return item.state === state ? (value as DisplayMetadata) : null;
}
