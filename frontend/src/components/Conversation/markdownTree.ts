/**
 * The Markdown parse on its own, with no DOM: text in, a plain hast tree out. It loads in a
 * Worker, so it must never touch `document` or `window`.
 *
 * The pipeline is react-markdown's own (remark-parse, the plugins, remark-rehype with
 * `allowDangerousHtml`), reached by calling its default export as a plain function with one
 * extra rehype plugin that captures the tree and hands react-markdown an empty root to finish.
 * Importing unified or remark-parse directly would pull in packages this app does not declare.
 *
 * The returned tree has no `position` keys (so it clones cheaply across a Worker boundary) and
 * no `raw` nodes: raw HTML becomes a text node here, as react-markdown's own `post()` would do on
 * the main thread. The renderer still runs `post()` over it, so links, images and code keep the
 * same safe path as a direct render.
 */
import Markdown, { type Options } from "react-markdown";
import remarkCjkFriendly from "remark-cjk-friendly";
import remarkGfm from "remark-gfm";

/** The deepest mdast level that still parses. About 49 nested list levels or 97 quote levels:
 * Edge's renderer crashes from about 750 list levels, and remark-rehype's recursion overflows
 * the stack near 1,200, so the bound runs on the mdast before remark-rehype ever sees it. */
export const MAX_MARKDOWN_DEPTH = 100;

export const REMARK_PLUGINS = [remarkGfm, remarkCjkFriendly];

export interface HastNode {
  type: string;
  children?: HastNode[];
  [key: string]: unknown;
}

export interface HastRoot extends HastNode {
  type: "root";
  children: HastNode[];
}

export type PlainReason = "depth" | "error" | "timeout" | "unavailable";

export type ParseResult =
  | { kind: "tree"; tree: HastRoot; nodes: number }
  | { kind: "plain"; reason: PlainReason };

type PluginList = NonNullable<Options["remarkPlugins"]>;

interface MdastNode {
  type?: string;
  children?: MdastNode[];
}

class DepthExceeded extends Error {}
class NodeCapExceeded extends Error {}

/** A table cell's actual mdast count undercounts a ragged table: a short row is padded to the
 * header's width only once remark-rehype builds hast, where each padded cell becomes an element
 * and a text node inside a row. Measured on evenly filled tables at scale (5,000x4 -> 75,024 hast
 * nodes for 20,000 cells; 10,000x4 -> 150,024 for 40,000; 100x100 -> 30,612 for 10,000), the ratio
 * settles around 3.1-3.75 hast nodes per padded cell; this rounds up so the budget below stays a
 * safe proxy for hast size without ever building the padded rows to count them for real. */
const TABLE_CELL_NODE_WEIGHT = 4;

/** The node-count budget the depth walk also enforces (see remarkDepthLimit). Chosen by
 * measurement (node 24, this repo's modules, the padded-cell formula above for tables):
 * - 200 KB of ordinary prose (sentences with occasional bold/italic/code/a link) comes in around
 *   14,000-17,000 nodes; the exact figure this cap is asked to clear, "about 30,000", is comfortably
 *   under half this budget.
 * - A 1,000-row, 4-column table costs 1,001 x 4 x 4 = 16,016 by the padded-cell formula (its real
 *   hast size measures 15,024).
 * - 5,000 links cost 15,001, one mdast node per link plus its text child, which is also its exact
 *   hast size (no table or footnote multiplier applies).
 * - The 700x700 padded table (single-cell rows padded out to 700 columns) costs 701 x 700 x 4 =
 *   1,962,800, since each of its 700 body rows carries only one real cell.
 * - The 160 KiB footnote flood (16,384 references to a 10,240-times-redefined footnote) costs
 *   63,489 raw mdast nodes — no table involved, so no weight applies — well above this budget and
 *   well below its own real hast size (147,478): remark-rehype's footnote backreferences add
 *   nodes this walk cannot see, which is why the budget sits far under any adversarial figure
 *   rather than close to it.
 * The same budget is enforced twice. On the mdast, as an early exit before remark-rehype runs:
 * mdast nodes, with each table costed by its padded cells, so a ragged table is refused without
 * ever building its padded rows. On the finished hast, as the bound on what the UI thread
 * renders: every hast node, each `pre` weighted for the elements CodeBlock adds around it
 * (PRE_NODE_WEIGHT). The second catches what the first cannot see — a footnote reference
 * expands to about 8 hast nodes, fences and thematic breaks expand too, and a table cell's
 * contents are never counted on the mdast — so no tree past this cap reaches react-markdown.
 * Neither count bounds time: a parse that stays under both can still run until the worker's
 * deadline (Markdown.tsx). */
export const MAX_MARKDOWN_NODES = 40_000;

/** CodeBlock (Markdown.tsx) renders each `pre` inside two divs beside a CopyButton, whose label
 * is one text node: 4 DOM nodes the hast tree does not hold, so the hast count adds them. */
const PRE_NODE_WEIGHT = 4;

/** Walks with an explicit stack: the input is hostile, and recursion is what overflows.
 * MAX_MARKDOWN_DEPTH bounds every node's depth, a table cell's contents included. The walk also
 * enforces MAX_MARKDOWN_NODES against mdast nodes, costing a table's cells by the padded width
 * its header declares (TABLE_CELL_NODE_WEIGHT) rather than counting them, so a ragged table's
 * real row lengths cannot hide its true size; the cells' contents are then walked for depth
 * only, never counted again (the hast count in parseMarkdown bounds their size). */
function remarkDepthLimit() {
  return (tree: MdastNode): undefined => {
    // The third field says whether the node counts toward the budget: false inside a table.
    const stack: [MdastNode, number, boolean][] = [[tree, 0, true]];
    let nodes = 0;
    for (let entry = stack.pop(); entry; entry = stack.pop()) {
      const [node, depth, counted] = entry;
      if (depth > MAX_MARKDOWN_DEPTH) throw new DepthExceeded();
      if (counted) nodes += 1;
      if (node.type === "table") {
        const columns = node.children?.[0]?.children?.length ?? 0;
        nodes += (node.children?.length ?? 0) * columns * TABLE_CELL_NODE_WEIGHT;
        if (nodes > MAX_MARKDOWN_NODES) throw new NodeCapExceeded();
        for (const row of node.children ?? []) {
          for (const cell of row.children ?? []) {
            for (const child of cell.children ?? []) stack.push([child, depth + 3, false]);
          }
        }
        continue;
      }
      if (nodes > MAX_MARKDOWN_NODES) throw new NodeCapExceeded();
      for (const child of node.children ?? []) stack.push([child, depth + 1, counted]);
    }
  };
}

function captureTree(sink: { tree?: HastRoot }) {
  return (tree: HastRoot): HastRoot => {
    sink.tree = tree;
    return { type: "root", children: [] };
  };
}

/** Strips positions, turns raw into text and counts nodes, root included, each `pre`
 * PRE_NODE_WEIGHT extra; iterative for the same reason as the depth walk. */
function clean(tree: HastRoot): number {
  let nodes = 0;
  const stack: HastNode[] = [tree];
  for (let node = stack.pop(); node; node = stack.pop()) {
    nodes += node.type === "element" && node.tagName === "pre" ? 1 + PRE_NODE_WEIGHT : 1;
    delete node.position;
    const children = node.children;
    if (!children) continue;
    children.forEach((child, index) => {
      if (child.type === "raw") {
        children[index] = { type: "text", value: child.value };
      }
      stack.push(children[index] as HastNode);
    });
  }
  return nodes;
}

export function parseMarkdown(text: string): ParseResult {
  const sink: { tree?: HastRoot } = {};
  try {
    Markdown({
      children: text,
      remarkPlugins: [remarkDepthLimit, ...REMARK_PLUGINS] as PluginList,
      rehypePlugins: [[captureTree, sink]] as PluginList,
    });
    const tree = sink.tree;
    if (!tree) return { kind: "plain", reason: "error" };
    const nodes = clean(tree);
    if (nodes > MAX_MARKDOWN_NODES) return { kind: "plain", reason: "error" };
    return { kind: "tree", tree, nodes };
  } catch (error) {
    return { kind: "plain", reason: error instanceof DepthExceeded ? "depth" : "error" };
  }
}
