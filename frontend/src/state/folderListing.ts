export interface FolderListing {
  entries: Record<string, unknown>[];
  partial: boolean;
  partial_reasons: string[];
  omitted: number;
}

const record = (value: unknown): Record<string, unknown> | null =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;

export function folderListing(raw: unknown, prefix: string, directOnly: boolean): FolderListing {
  const body = record(raw);
  if (
    !body ||
    typeof prefix !== "string" ||
    !prefix ||
    prefix.startsWith("/") ||
    prefix.endsWith("/") ||
    prefix.split("/").some((part) => !part || part === "." || part === "..") ||
    typeof directOnly !== "boolean" ||
    !Array.isArray(body.entries) ||
    typeof body.partial !== "boolean" ||
    !Array.isArray(body.partial_reasons) ||
    !body.partial_reasons.every((reason) => typeof reason === "string") ||
    (body.omitted !== undefined &&
      (typeof body.omitted !== "number" || !Number.isSafeInteger(body.omitted) || body.omitted < 0))
  )
    throw new Error("invalid_response");

  const selected: Record<string, unknown>[] = [];
  const refs = new Set<string>();
  const folderPrefix = `${prefix}/`;
  for (const value of body.entries) {
    const entry = record(value);
    if (!entry) throw new Error("invalid_response");
    const ref = entry.ref;
    if (typeof ref !== "string" || !ref.startsWith(folderPrefix)) continue;
    const relative = ref.slice(folderPrefix.length);
    if (directOnly && relative.includes("/")) continue;
    if (
      !relative ||
      relative.split("/").some((part) => !part) ||
      refs.has(ref) ||
      (entry.modified !== undefined &&
        (typeof entry.modified !== "number" || !Number.isFinite(entry.modified)))
    )
      throw new Error("invalid_response");
    refs.add(ref);
    selected.push(entry);
  }

  return {
    entries: selected,
    partial: body.partial,
    partial_reasons: body.partial_reasons as string[],
    omitted: (body.omitted as number | undefined) ?? 0,
  };
}
