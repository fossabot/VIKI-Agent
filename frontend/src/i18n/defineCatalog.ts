/**
 * The generic identity function every catalog module wraps its one English table in, so the table
 * keeps its literal keys (`Key` in `index.ts` is their union) and every value must be a string.
 * This file imports nothing, so no catalog module needs `i18n/index.ts` to reach it — the module
 * graph is `catalog module -> defineCatalog.ts` and, separately, `index.ts -> every catalog
 * module`: a plain DAG, never a cycle. (Were `defineCatalog` defined in `index.ts` itself, every
 * catalog module would import back from the module that imports them; that only resolves for
 * entries that happen to load `index.ts` or `t.ts` first — importing a catalog module directly,
 * as a view's own component does, would hit a TDZ `ReferenceError`. `i18n.unit.test.ts` guards
 * this.)
 */
export function defineCatalog<E extends Record<string, string>>(catalog: { en: E }): { en: E } {
  return catalog;
}
