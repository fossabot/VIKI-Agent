/**
 * VIKI's reply text as markdown, with the vault's threat model applied
 * (src/accept/m2b/markdown.accept.test.tsx pins the contract).
 *
 * Parsing: markdownTree.ts parses the text into a hast tree, with the nesting depth bounded on the
 * mdast before any DOM exists (a table cell's contents walked for depth too) and the node count
 * bounded twice — an early exit on the mdast, each table costed by its padded cells, and again on
 * the finished hast, every node counted, each `pre` weighted for the elements CodeBlock adds around
 * it — so no tree past that cap ever reaches react-markdown. The byte cap below and the parse
 * deadline bound the tree's size and how long a parse can run; the render itself has no deadline.
 * The node cap bounds the elements the render creates, not the layout they need: at-cap shapes
 * rendered in 56-160 ms, but a crafted reply under every cap (30 nested quotes around one 262 KB
 * inline code span) blocks the window for about 1 s each time it renders (both measured in
 * headless Edge 153). In the app that parse itself runs
 * in a worker (markdownParser.ts, provided through MarkdownParserContext) under its own
 * deadline — 250 ms while a reply streams, 2 s once it has settled. This component renders the
 * finished tree through react-markdown with a plugin that substitutes it and an empty source, so the
 * main thread never parses; react-markdown's raw-to-text step, urlTransform and the components
 * map below still run here, exactly as for a direct render. Until an answer comes the text
 * shows plain, marked `aria-busy`; while a reply streams, the last tree stays up instead. A parse
 * that fails, overruns its deadline or nests too deep shows plain. An answer that changes what an
 * already committed text shows dispatches a bubbling `markdownformat` event from the block's root,
 * so the conversation column can keep a followed reply pinned. With no parser in context
 * (tests, and the frozen pins) the parse runs synchronously in the same render.
 *
 * Rules:
 * - react-markdown with remark-gfm and remark-cjk-friendly, and nothing else: no rehype-raw,
 *   no syntax highlighter, no katex, no mermaid. Without rehype-raw, raw HTML in the source is
 *   an mdast "html" node, and the parse turns its raw hast node into text — never an element —
 *   so it needs no filtering here.
 * - Links: the scheme is read from the untouched href (urlTransform is the identity function,
 *   so no sanitizer rewrites it first) before anything else happens to it. Only http:, https:
 *   and mailto: become an anchor; every other scheme — javascript:, data:, vscode:, file: —
 *   renders as text with the URL still visible, so the user sees what the model wrote. A safe
 *   anchor's click is handled here: preventDefault, then onOpenUrl, so the page never navigates;
 *   it carries rel="noreferrer noopener". When `onOpenNoteLink` is given, an href starting with
 *   `NOTE_LINK_PREFIX` renders as a button instead of an anchor, calling it with the decoded
 *   target rather than opening anything.
 * - Images render as their alt text in brackets, never as an <img>: a reply cannot make the page
 *   fetch anything.
 * - Code: a fenced block's `pre` keeps its text verbatim in `.md-code`; a `code` with no
 *   `language-*` className — inline code, since a fence's info string always gives its `code`
 *   one — gets `.md-inline-code`. No highlighter is loaded, so neither ever gets a child span.
 * - Everything else is default react-markdown output under `.md`, sized by Conversation.css.
 * - `streaming` (default false) appends the blinking caret (`.caret`, base.css) after the
 *   rendered markdown, which is how the design shows a reply still arriving.
 *
 * Copy (src/accept/m4s/copy.accept.test.tsx pins it): every fenced block's `pre` — never
 * inline code, which this plugin set never routes through `pre` at all — gets a CopyButton
 * ("Copy code"). It reads `preRef.current.querySelector("code")`'s own textContent at click
 * time rather than the source string handed to this component, so it copies exactly the code the
 * user is looking at (verbatim, no highlighter spans to strip) and never the surrounding prose.
 * The button lives in a sibling `.md-code-bar`, not inside `.md-code` itself — `.md-code`'s only
 * child stays the `<code>` react-markdown rendered, so its own textContent (and the "code keeps
 * its text verbatim" acceptance check) never picks up the button's own label. `CodeBlock` is a
 * module-level component, not a closure inside `Markdown`, so its identity is stable across a
 * streaming reply's re-renders — an inline function here would make react-markdown remount every
 * fenced block on every delta, resetting a copy that just landed back to its idle label.
 */
import {
  Component,
  type ReactElement,
  type ReactNode,
  type Ref,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import "./Conversation.css";
import { CopyButton } from "./Copy";
import {
  type MarkdownParser,
  MarkdownParserContext,
  SETTLED_DEADLINE_MS,
  STREAMING_DEADLINE_MS,
} from "./markdownParser";
import { type HastRoot, type ParseResult, type PlainReason, parseMarkdown } from "./markdownTree";

/** Text larger than this is never parsed and renders as plain text. This bounds size, not parse
 * time: the parser's cost grows faster than input size for some shapes (large tables, crafted
 * delimiter runs), which is what the worker's deadline is for. */
export const MAX_MARKDOWN_BYTES = 256 * 1024;

function byteLength(text: string): number {
  return new TextEncoder().encode(text).byteLength;
}

function PlainText({
  text,
  streaming,
  busy = false,
  reason,
  ref,
}: {
  text: string;
  streaming: boolean;
  busy?: boolean;
  reason?: PlainReason;
  ref?: Ref<HTMLDivElement>;
}): ReactElement {
  return (
    <div className="md jp" aria-busy={busy ? "true" : undefined} ref={ref}>
      <pre className="md-plain" data-reason={reason}>
        {text}
      </pre>
      {streaming ? <span className="caret" aria-hidden="true" /> : null}
    </div>
  );
}

/** AGENTS.md asks for function components; React offers error boundaries only as classes, so
 * this one is the exception. Rendering a tree can still throw, and an uncaught throw here would
 * unmount the whole window, chat included. This boundary catches that JS error and falls back to
 * the raw source in a `<pre>`, a plain text node, never raw HTML. It cannot catch Edge's own
 * renderer crashing on deeply nested lists: that is a Blink layout crash after React has already
 * committed, not a JS exception, so no error boundary sees it. That is why the parse bounds the
 * nesting depth before any DOM exists: a deeper text renders plain and never reaches here.
 *
 * VaultBrowser reuses one Markdown instance across notes with no key on it (keying by text would
 * remount it, and drop the tree it shows, on every streamed token), so `failed` has to
 * clear itself once `text` moves on to a note that never threw — otherwise one crashed note
 * would leave every later note stuck on plain text. `getDerivedStateFromProps` compares the
 * incoming text against the text state was last derived from and resets `failed` when it
 * changes; it does not fire on every render, only when props actually differ from that
 * snapshot, so a caught failure for the *current* text is not immediately undone. */
class MarkdownErrorBoundary extends Component<
  { text: string; streaming: boolean; children: ReactNode },
  { failed: boolean; text: string }
> {
  override state = { failed: false, text: this.props.text };
  static getDerivedStateFromProps(
    props: { text: string },
    state: { failed: boolean; text: string },
  ): { failed: boolean; text: string } | null {
    return props.text === state.text ? null : { failed: false, text: props.text };
  }
  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }
  override render(): ReactNode {
    if (this.state.failed) {
      return <PlainText text={this.props.text} streaming={this.props.streaming} />;
    }
    return this.props.children;
  }
}

export interface MarkdownProps {
  text: string;
  streaming?: boolean;
  onOpenUrl: (url: string) => void;
  onOpenNoteLink?: (target: string) => void;
}

/** The literal prefixes server.py's `_valid_open_url` accepts, in the same order and the same
 * form. Not a set of schemes: "http:" alone passed a scheme test while the server needs
 * "http://", so `[x](http:evil)` drew a link that open.url then dropped — a click that does
 * nothing at all, which is the one failure the user cannot diagnose. */
const SAFE_PREFIXES = ["http://", "https://", "mailto:"];

/** Whitespace or a control character anywhere in the href. The server's open.url refuses such a
 * URL outright, and the two checks must agree: a link drawn here that open.url then drops is the
 * one failure the user cannot diagnose — they click, and nothing happens at all. Where they
 * could differ, the stricter side wins, which is why this lives here rather than the server
 * being loosened to match. `schemeOf` trims before matching, so without this a href with a
 * trailing newline would pass here and be dropped there. */
// biome-ignore lint/suspicious/noControlCharactersInRegex: refusing them is the point
const UNSAFE_URL_CHARS = /[\s\x00-\x1f\x7f-\x9f]/;

function isSafeUrl(url: string): boolean {
  if (UNSAFE_URL_CHARS.test(url)) return false;
  const lower = url.toLowerCase();
  return SAFE_PREFIXES.some((prefix) => lower.startsWith(prefix));
}

/** Scheme `wikilinks.ts` uses for a `[[wikilink]]` href that resolves to a note, so the `a`
 * component below can render it as a button that opens the note instead of a dead anchor. */
export const NOTE_LINK_PREFIX = "viki-note:";

/** The target after `NOTE_LINK_PREFIX`, percent-decoded, or null when the href is malformed or
 * decodes to nothing — either way the caller falls through to the unsafe-scheme rendering below. */
function decodeNoteTarget(href: string): string | null {
  try {
    const target = decodeURIComponent(href.slice(NOTE_LINK_PREFIX.length));
    return target.length > 0 ? target : null;
  } catch {
    return null;
  }
}

function CodeBlock({ children }: { children?: ReactNode }): ReactElement {
  const preRef = useRef<HTMLPreElement>(null);
  return (
    <div className="md-code-wrap">
      <div className="md-code-bar">
        <CopyButton
          label="Copy code"
          getText={() => preRef.current?.querySelector("code")?.textContent ?? ""}
        />
      </div>
      <pre className="md-code" ref={preRef}>
        {children}
      </pre>
    </div>
  );
}

/** Identity urlTransform: react-markdown's default sanitizer would already strip a disallowed
 * scheme, but this component needs the untouched href to show the user what was actually
 * written when it refuses to link it. */
function keepUrl(url: string): string {
  return url;
}

function markdownComponents(
  onOpenUrl: (url: string) => void,
  onOpenNoteLink?: (target: string) => void,
): Components {
  return {
    a({ href, children }) {
      if (onOpenNoteLink && href?.startsWith(NOTE_LINK_PREFIX)) {
        const target = decodeNoteTarget(href);
        if (target) {
          return (
            <button type="button" className="md-note-link" onClick={() => onOpenNoteLink(target)}>
              {children}
            </button>
          );
        }
      }
      if (href && isSafeUrl(href)) {
        return (
          <a
            href={href}
            rel="noreferrer noopener"
            onClick={(event) => {
              event.preventDefault();
              onOpenUrl(href);
            }}
            // A middle click never fires onClick, so without this the URL would load inside the
            // app's own Edge profile — the one with no extensions, the user's session cookies
            // absent, and a lifetime tied to the Job that keeps the app alive. Right-click →
            // open in new tab cannot be intercepted without replacing the native menu, so this
            // is deliberately partial: the two paths a click can take are covered, and the menu
            // is left alone.
            onAuxClick={(event) => {
              if (event.button !== 1) return;
              event.preventDefault();
              onOpenUrl(href);
            }}
          >
            {children}
          </a>
        );
      }
      return (
        <>
          {children}
          {href ? ` (${href})` : null}
        </>
      );
    },
    img({ alt }) {
      return <>{`[${alt ?? ""}]`}</>;
    },
    pre: CodeBlock,
    code({ className, children }) {
      const fenced = typeof className === "string" && className.startsWith("language-");
      return <code className={fenced ? undefined : "md-inline-code"}>{children}</code>;
    },
  };
}

/** The parsed tree replaces the empty one react-markdown built from `""`. */
function substituteTree(tree: HastRoot) {
  return () => tree;
}

/** A parse answer with the text it answers; `seq` orders this instance's requests. */
interface Parsed {
  seq: number;
  text: string;
  result: ParseResult;
}

const STALLED: ParseResult = { kind: "plain", reason: "timeout" };

/** What the worker path shows for `text` now, or null while it waits with nothing to keep. An
 * answer is shown only against the text it was asked for or, while streaming, a text that
 * extends it: a streaming text only grows, so that answer is the start of what is on its way. */
function shownResult(
  parser: MarkdownParser,
  text: string,
  streaming: boolean,
  parsed: Parsed | null,
  timedOut: string | null,
): ParseResult | null {
  if (streaming && timedOut !== null && text.startsWith(timedOut)) return STALLED;
  if (parsed?.text === text) return parsed.result;
  const hit = parser.cached(text);
  if (hit) return hit;
  if (streaming && parsed && text.startsWith(parsed.text)) return parsed.result;
  return null;
}

export function Markdown({
  text,
  streaming = false,
  onOpenUrl,
  onOpenNoteLink,
}: MarkdownProps): ReactElement {
  const parser = useContext(MarkdownParserContext);
  const components = useMemo(
    () => markdownComponents(onOpenUrl, onOpenNoteLink),
    [onOpenUrl, onOpenNoteLink],
  );
  const oversize = byteLength(text) > MAX_MARKDOWN_BYTES;
  const direct = useMemo(
    () => (parser || oversize ? null : parseMarkdown(text)),
    [parser, oversize, text],
  );
  const [owner] = useState(() => ({}));
  const [parsed, setParsed] = useState<Parsed | null>(null);
  const lastSeq = useRef(0);
  // A streaming text whose parse overran: a longer text would only overrun again, so while the
  // reply streams its extensions stay plain and are not sent; the settled text is.
  const timedOut = useRef<string | null>(null);

  useEffect(() => {
    if (!parser || oversize) return;
    if (streaming && timedOut.current !== null && text.startsWith(timedOut.current)) return;
    lastSeq.current += 1;
    const seq = lastSeq.current;
    const deadline = streaming ? STREAMING_DEADLINE_MS : SETTLED_DEADLINE_MS;
    parser.request(owner, text, deadline, (result) => {
      if (streaming && result.kind === "plain" && result.reason === "timeout") {
        timedOut.current = text;
      }
      // A job already in the worker still answers after a newer request, and a cache hit
      // answers a newer request at once, so an older answer can land last: it never wins.
      setParsed((prev) => (prev && prev.seq > seq ? prev : { seq, text, result }));
    });
  }, [parser, oversize, owner, text, streaming]);

  // Cancelling also silences a job still in the worker, so no answer sets state after unmount.
  useEffect(() => () => parser?.cancel(owner), [parser, owner]);

  const result = parser ? shownResult(parser, text, streaming, parsed, timedOut.current) : direct;

  // A streaming delta re-renders this component over an unchanged tree; without the memo every
  // delta would walk the whole tree through react-markdown again on this thread.
  const rendered = useMemo(
    () =>
      result?.kind === "tree" ? (
        <ReactMarkdown
          remarkPlugins={[]}
          rehypePlugins={[[substituteTree, result.tree]]}
          urlTransform={keepUrl}
          components={components}
        >
          {""}
        </ReactMarkdown>
      ) : null,
    [result, components],
  );

  // The same text now shows a different answer: it arrived after the text's own commit, so the
  // block re-laid out with nothing the conversation column can see. A new text needs no event,
  // since its commit is already one the column re-measures after.
  const root = useRef<HTMLDivElement>(null);
  const committed = useRef<{ text: string; result: ParseResult | null } | null>(null);
  useLayoutEffect(() => {
    const previous = committed.current;
    committed.current = { text, result };
    if (previous?.text === text && previous.result !== result) {
      root.current?.dispatchEvent(new Event("markdownformat", { bubbles: true }));
    }
  });

  if (oversize || !result) {
    return <PlainText text={text} streaming={streaming} busy={!oversize} ref={root} />;
  }
  if (result.kind === "plain") {
    return <PlainText text={text} streaming={streaming} reason={result.reason} ref={root} />;
  }
  return (
    <MarkdownErrorBoundary text={text} streaming={streaming}>
      <div className="md jp" ref={root}>
        {rendered}
        {streaming ? <span className="caret" aria-hidden="true" /> : null}
      </div>
    </MarkdownErrorBoundary>
  );
}
