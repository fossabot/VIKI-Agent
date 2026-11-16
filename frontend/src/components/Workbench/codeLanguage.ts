export type LanguageKind = "python" | "javascript" | "jsx" | "typescript" | "tsx" | "json";

const SUFFIXES = new Map<string, LanguageKind>([
  [".py", "python"],
  [".pyi", "python"],
  [".pyw", "python"],
  [".js", "javascript"],
  [".mjs", "javascript"],
  [".cjs", "javascript"],
  [".jsx", "jsx"],
  [".ts", "typescript"],
  [".mts", "typescript"],
  [".cts", "typescript"],
  [".tsx", "tsx"],
  [".json", "json"],
]);

/** A known suffix selects its grammar; unknown files remain plain without loading the editor. */
export function languageKind(ref: string): LanguageKind | null {
  const dot = ref.lastIndexOf(".");
  return dot < 0 ? null : (SUFFIXES.get(ref.slice(dot).toLowerCase()) ?? null);
}
