import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ReactMarkdown, { type Options } from "react-markdown";
import { describe, expect, it, vi } from "vitest";
import { type HastNode, type HastRoot, parseMarkdown, REMARK_PLUGINS } from "./markdownTree";

function treeOf(text: string): HastRoot {
  const result = parseMarkdown(text);
  if (result.kind !== "tree") throw new Error(`expected a tree, got ${result.reason}`);
  return result.tree;
}

function allNodes(tree: HastRoot): HastNode[] {
  const out: HastNode[] = [];
  const stack: HastNode[] = [tree];
  for (let node = stack.pop(); node; node = stack.pop()) {
    out.push(node);
    stack.push(...(node.children ?? []));
  }
  return out;
}

function keepUrl(url: string): string {
  return url;
}

function directMarkup(text: string): string {
  return renderToStaticMarkup(
    createElement(ReactMarkdown, { remarkPlugins: REMARK_PLUGINS, urlTransform: keepUrl }, text),
  );
}

function handedOffMarkup(tree: HastRoot): string {
  const rehypePlugins = [[() => () => tree]] as NonNullable<Options["rehypePlugins"]>;
  return renderToStaticMarkup(
    createElement(ReactMarkdown, { remarkPlugins: [], rehypePlugins, urlTransform: keepUrl }, ""),
  );
}

describe("parseMarkdown", () => {
  it("bounds nesting depth on the mdast", () => {
    expect(parseMarkdown(`${"- ".repeat(49)}a`).kind).toBe("tree");
    expect(parseMarkdown(`${"- ".repeat(50)}a`)).toEqual({ kind: "plain", reason: "depth" });
    expect(parseMarkdown(`${"- ".repeat(1000)}a`).kind).toBe("plain");
    expect(parseMarkdown(`${"> ".repeat(1800)}a`)).toEqual({ kind: "plain", reason: "depth" });
  });

  it("parses emphasis and decodes entities", () => {
    const nodes = allNodes(treeOf("**b** &copy;"));
    const strong = nodes.find((node) => node.type === "element" && node.tagName === "strong");
    expect(strong?.children).toEqual([{ type: "text", value: "b" }]);
    expect(nodes.some((node) => node.type === "text" && String(node.value).includes("©"))).toBe(
      true,
    );
  });

  it("turns raw HTML into text, never an element", () => {
    const nodes = allNodes(treeOf("<script>x</script>"));
    expect(nodes.some((node) => node.type === "raw")).toBe(false);
    expect(nodes.some((node) => node.type === "element" && node.tagName === "script")).toBe(false);
    expect(nodes.some((node) => node.type === "text" && node.value === "<script>x</script>")).toBe(
      true,
    );
  });

  it("strips positions, counts every node and survives structuredClone", () => {
    const result = parseMarkdown(
      "# t\n\n- [x] a\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\nx[^1]\n\n[^1]: n",
    );
    if (result.kind !== "tree") throw new Error("expected a tree");
    const nodes = allNodes(result.tree);
    expect(nodes.some((node) => "position" in node)).toBe(false);
    expect(result.nodes).toBe(nodes.length);
    expect(structuredClone(result.tree)).toEqual(result.tree);
  });

  it("counts each code block's pre four extra, for the wrapper and copy button it renders in", () => {
    const result = parseMarkdown("a\n\n```\nx\n```\n\n```ts\ny\n```");
    if (result.kind !== "tree") throw new Error("expected a tree");
    const nodes = allNodes(result.tree);
    const pres = nodes.filter((node) => node.type === "element" && node.tagName === "pre");
    expect(pres).toHaveLength(2);
    expect(result.nodes).toBe(nodes.length + 2 * 4);
  });

  // CI runners take several seconds for these parses; the bound under test is the node count.
  it.each([
    ["a footnote reference run", `${"[^a]".repeat(39990)}\n\n[^a]: x\n`],
    ["a run of empty fences", "```\n```\n".repeat(32768)],
    ["a run of thematic breaks", "---\n\n".repeat(39999)],
    ["a one-cell table of inline code", `| ${"`a` ".repeat(30000)}|\n|---|\n`],
  ])(
    "refuses %s: under the mdast budget, but its hast tree passes the cap",
    (_name, text) => {
      expect(parseMarkdown(text)).toEqual({ kind: "plain", reason: "error" });
    },
    30_000,
  );

  it("pins the hast cap from both sides", () => {
    // Each thematic break is an hr and a newline text in hast (the root stands in for the last
    // newline), so n breaks are exactly 2n nodes: 20,000 sit on the cap, 20,001 pass it.
    const onCap = parseMarkdown("---\n\n".repeat(20000));
    expect(onCap.kind === "tree" ? onCap.nodes : onCap.reason).toBe(40000);
    expect(parseMarkdown("---\n\n".repeat(20001))).toEqual({ kind: "plain", reason: "error" });
  }, 30_000);

  it("bounds nesting depth inside a table cell", () => {
    const deep = `| ${"*".repeat(1150)}a${"*".repeat(1150)} |\n|---|\n`;
    expect(parseMarkdown(deep)).toEqual({ kind: "plain", reason: "depth" });
    expect(parseMarkdown("| **a** |\n|---|\n").kind).toBe("tree");
  });

  it.each([
    ["raw HTML with a script", "<div>hi</div>\n\n<script>alert(1)</script>\n\ninline <b>x</b>"],
    ["a safe and an unsafe link", "[ok](https://example.com) and [bad](javascript:alert(1))"],
    ["an image", "![alt text](https://example.com/x.png)"],
    ["a GFM table", "| a | b |\n|:--|--:|\n| 1 | 2 |"],
    ["a task list", "- [x] done\n- [ ] todo"],
    ["a footnote", "Text[^1]\n\n[^1]: The note."],
    ["fenced code with meta", '```ts title="x.ts"\nconst a = 1;\n```'],
    ["strikethrough", "~~gone~~ kept"],
    ["an autolink", "see https://example.com and <https://example.org>"],
  ])("hands off %s with the same markup as a direct render", (_name, text) => {
    expect(handedOffMarkup(treeOf(text))).toBe(directMarkup(text));
  });

  // CI runners take 2 to 7 s for this parse; the bound under test is the node count, not the time.
  it("bounds the tree's node count, below the render freeze the security lens measured", () => {
    // A ragged table: each of the 700 body rows is a single bare "a" with no pipe, so
    // remark-rehype pads it out to the header's 700 columns once it builds hast — the real cost
    // this walk estimates from the header's width instead of paying to build those padded rows.
    const header = `|${"a|".repeat(700)}`;
    const separator = `|${"-|".repeat(700)}`;
    const rows = Array.from({ length: 700 }, () => "a").join("\n");
    expect(parseMarkdown(`${header}\n${separator}\n${rows}`)).toEqual({
      kind: "plain",
      reason: "error",
    });

    // 16,384 references to a footnote redefined 10,240 times: remark-rehype's own backreference
    // handling multiplies this past its raw mdast count, which is already well over the cap.
    const flood = "[^a] ".repeat(16384) + "[^a]: x\n".repeat(10240);
    expect(parseMarkdown(flood)).toEqual({ kind: "plain", reason: "error" });
  }, 30_000);

  it("still returns a tree for a legitimately large table or a long reply", () => {
    const table = (rows: number) =>
      [
        "| a | b | c | d |",
        "| - | - | - | - |",
        ...Array.from({ length: rows }, (_, i) => `| ${i} | ${i} | ${i} | ${i} |`),
      ].join("\n");
    expect(parseMarkdown(table(1000)).kind).toBe("tree");
    // Costed at 38,416 by its padded cells, with a hast tree under the cap: a tree only while the
    // cells' contents, walked for depth, are not counted a second time.
    expect(parseMarkdown(table(2400)).kind).toBe("tree");

    const sentence =
      "The quick brown fox jumps over the lazy dog. **Bold** text and *italic* text and " +
      "`code` span here, plus a [link](https://example.com/x) in the middle of the sentence.\n\n";
    let prose = "";
    while (new TextEncoder().encode(prose).byteLength < 200 * 1024) prose += sentence;
    expect(parseMarkdown(prose).kind).toBe("tree");

    expect(parseMarkdown("[a](https://example.com) ".repeat(5000)).kind).toBe("tree");
  }, 30_000);

  it("turns a RangeError inside the parse into plain error", async () => {
    vi.resetModules();
    vi.doMock("remark-gfm", () => ({
      default: () => () => {
        throw new RangeError("Maximum call stack size exceeded");
      },
    }));
    try {
      const fresh = await import("./markdownTree");
      expect(fresh.parseMarkdown("a")).toEqual({ kind: "plain", reason: "error" });
    } finally {
      vi.doUnmock("remark-gfm");
    }
  });
});
