/**
 * The only file that imports `@codemirror/*` or `@lezer/*` (editorBoundary.unit.test.ts). The
 * rest of the app reaches it through `import("./codemirror")`, so the editor stays out of the
 * entry chunk. Colours come from `codemirror.css` and `var(--…)` tokens, never literals.
 */
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import {
  bracketMatching,
  foldGutter,
  foldKeymap,
  indentOnInput,
  indentUnit,
  type LanguageSupport,
  syntaxHighlighting,
} from "@codemirror/language";
import { highlightSelectionMatches, search, searchKeymap } from "@codemirror/search";
import { Compartment, EditorState, type Extension } from "@codemirror/state";
import {
  drawSelection,
  EditorView,
  highlightActiveLine,
  highlightActiveLineGutter,
  highlightSpecialChars,
  keymap,
  lineNumbers,
} from "@codemirror/view";
import { classHighlighter, highlightTree, tagHighlighter, tags } from "@lezer/highlight";
import { type LanguageKind, languageKind } from "./codeLanguage";
import "./codemirror.css";

export { type LanguageKind, languageKind } from "./codeLanguage";

export interface CodeToken {
  readonly from: number;
  readonly to: number;
  readonly className: string;
}

const functionHighlighter = tagHighlighter([
  {
    tag: [tags.function(tags.variableName), tags.function(tags.propertyName)],
    class: "tok-function",
  },
]);
const NO_TOKENS: readonly CodeToken[] = Object.freeze([]);

export interface EditorOptions {
  parent: HTMLElement;
  text: string;
  /** The saved text: the editor is dirty while its document differs from this. */
  base: string;
  ref: string;
  wrap: boolean;
  fontSize: number;
  /** The accessible name of the editing area. */
  label: string;
  onDirty(dirty: boolean): void;
  /** Current text after each document edit, including edits that stay dirty. */
  onChange?(text: string): void;
  onSave(): void;
}

export interface EditorHandle {
  text(): string;
  setText(text: string): void;
  setBase(text: string): void;
  setWrap(wrap: boolean): void;
  setFontSize(px: number): void;
  focus(): void;
  destroy(): void;
}

/** The file's own indent unit. Enter counts columns and writes them in the unit, so a unit unlike
 * the file's turns its tabs into spaces (or the reverse). Tabs win when more lines start with
 * one; else the commonest 2 to 8 space step between lines; else four spaces in Python and two
 * elsewhere. */
export function indentUnitFor(text: string, kind: LanguageKind | null): string {
  let tabs = 0;
  let spaces = 0;
  let previous = 0;
  const votes = new Map<number, number>();
  for (const line of text.split(/\r\n|\r|\n/)) {
    const lead = /^[ \t]*/.exec(line)?.[0] ?? "";
    if (lead.length === line.length) continue;
    if (lead.startsWith("\t")) tabs++;
    else if (lead.startsWith(" ")) spaces++;
    if (lead.includes("\t")) continue;
    const step = lead.length - previous;
    previous = lead.length;
    if (step >= 2 && step <= 8) votes.set(step, (votes.get(step) ?? 0) + 1);
  }
  if (tabs > spaces) return "\t";
  let best = kind === "python" ? 4 : 2;
  let bestVotes = 0;
  for (const [step, count] of votes) {
    if (count > bestVotes || (count === bestVotes && step < best)) {
      best = step;
      bestVotes = count;
    }
  }
  return " ".repeat(best);
}

/** Each grammar is its own `import()`, so a file type loads only its own pack. The pack brings
 * the language's highlighting tags, indentation and folding. */
export async function languageSupport(kind: LanguageKind | null): Promise<LanguageSupport | null> {
  switch (kind) {
    case "python":
      return (await import("@codemirror/lang-python")).python();
    case "json":
      return (await import("@codemirror/lang-json")).json();
    case "javascript":
    case "jsx":
    case "typescript":
    case "tsx": {
      const { javascript } = await import("@codemirror/lang-javascript");
      return javascript({
        jsx: kind === "jsx" || kind === "tsx",
        typescript: kind === "typescript" || kind === "tsx",
      });
    }
    case null:
      return null;
  }
}

/** Preview coloring is optional work, not a smaller file reader. Large or costly inputs stay
 * plain here; the full editor retains its own incremental parser. Budgets are cooperative:
 * one tokenizer advance can exceed the time budget before control returns to this loop. */
export async function codeTokens(
  ref: string,
  text: string,
  signal?: AbortSignal,
): Promise<readonly CodeToken[]> {
  if (text.length > 1 << 18 || signal?.aborted) return NO_TOKENS;
  try {
    const language = await languageSupport(languageKind(ref));
    if (!language || signal?.aborted) return NO_TOKENS;
    const start = performance.now();
    const stopped = () => signal?.aborted || performance.now() - start >= 20;
    const parse = language.language.parser.startParse(text);
    let tree = null;
    for (let step = 0; step < 32768; step++) {
      if (stopped()) return NO_TOKENS;
      tree = parse.advance();
      if (tree) break;
    }
    if (!tree) return NO_TOKENS;
    const tokens: CodeToken[] = [];
    highlightTree(tree, [classHighlighter, functionHighlighter], (from, to, className) => {
      if (stopped() || tokens.length >= 8192) throw new Error("code_syntax_budget");
      tokens.push(Object.freeze({ from, to, className }));
    });
    return stopped() ? NO_TOKENS : Object.freeze(tokens);
  } catch {
    return NO_TOKENS;
  }
}

const colours = EditorView.theme({
  "&": { color: "var(--t0)", backgroundColor: "var(--s0)" },
  "&.cm-focused": { outline: "2px solid var(--or)" },
  ".cm-scroller": { fontFamily: "var(--font-mono)", lineHeight: "1.5" },
  ".cm-cursor, .cm-dropCursor": { borderLeftColor: "var(--t0)" },
  ".cm-selectionBackground": { backgroundColor: "var(--code-selection)" },
  "&.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground": {
    backgroundColor: "var(--code-selection)",
  },
  ".cm-activeLine": { backgroundColor: "var(--fill-hi)" },
  ".cm-gutters": {
    backgroundColor: "var(--s1)",
    color: "var(--t3)",
    borderRight: "1px solid var(--b1)",
  },
  ".cm-activeLineGutter": { backgroundColor: "var(--fill-hi)", color: "var(--t0)" },
  ".cm-foldPlaceholder": {
    backgroundColor: "var(--fill-hi)",
    color: "var(--t2)",
    border: "1px solid var(--b1)",
  },
  ".cm-specialChar": { color: "var(--err)" },
  ".cm-matchingBracket, .cm-nonmatchingBracket": { backgroundColor: "var(--code-selection)" },
  ".cm-selectionMatch": { backgroundColor: "var(--fill-hi)" },
  ".cm-searchMatch": { backgroundColor: "var(--fill-hi)", outline: "1px solid var(--t3)" },
  ".cm-searchMatch.cm-searchMatch-selected": {
    backgroundColor: "var(--code-selection)",
    outline: "1px solid var(--or)",
  },
  ".cm-panels": {
    color: "var(--t0)",
    backgroundColor: "var(--s1)",
    borderColor: "var(--b1)",
  },
  ".cm-panels.cm-panels-top": { borderBottom: "1px solid var(--b1)" },
  ".cm-panel.cm-search .cm-textfield": {
    color: "var(--t0)",
    backgroundColor: "var(--s0)",
    border: "1px solid var(--b1)",
  },
  ".cm-panel.cm-search .cm-button": {
    color: "var(--t0)",
    backgroundColor: "var(--fill-hi)",
    backgroundImage: "none",
    border: "1px solid var(--b1)",
  },
});

const fontSizeTheme = (px: number): Extension => EditorView.theme({ "&": { fontSize: `${px}px` } });

const wrapExtension = (wrap: boolean): Extension => (wrap ? EditorView.lineWrapping : []);

/** Creates the editor in `parent`; resolves once the file type's grammar has loaded. */
export async function createEditor(options: EditorOptions): Promise<EditorHandle> {
  const kind = languageKind(options.ref);
  const language = await languageSupport(kind);
  const wrap = new Compartment();
  const fontSize = new Compartment();
  let baseDoc = EditorState.create({ doc: options.base }).doc;
  let reportedDirty = false;

  const reportDirty = (state: EditorState) => {
    const dirty = !state.doc.eq(baseDoc);
    if (dirty === reportedDirty) return;
    reportedDirty = dirty;
    options.onDirty(dirty);
  };

  const state = EditorState.create({
    doc: options.text,
    extensions: [
      lineNumbers(),
      highlightActiveLineGutter(),
      highlightSpecialChars(),
      history(),
      drawSelection(),
      bracketMatching(),
      highlightActiveLine(),
      highlightSelectionMatches(),
      search({ top: true }),
      syntaxHighlighting(classHighlighter),
      syntaxHighlighting(functionHighlighter),
      language ?? [],
      indentUnit.of(indentUnitFor(options.base, kind)),
      indentOnInput(),
      foldGutter(),
      wrap.of(wrapExtension(options.wrap)),
      fontSize.of(fontSizeTheme(options.fontSize)),
      colours,
      EditorView.contentAttributes.of({ "aria-label": options.label }),
      EditorView.updateListener.of((update) => {
        if (update.docChanged) {
          reportDirty(update.state);
          options.onChange?.(update.state.doc.toString());
        }
      }),
      keymap.of([
        {
          key: "Mod-s",
          run: () => {
            options.onSave();
            return true;
          },
          preventDefault: true,
        },
        {
          // Esc then Tab leaves the editor. Returns false so Esc still closes the search panel.
          key: "Escape",
          run: (view) => {
            view.setTabFocusMode(2000);
            return false;
          },
        },
        ...defaultKeymap,
        ...historyKeymap,
        ...searchKeymap,
        ...foldKeymap,
        indentWithTab,
      ]),
    ],
  });
  const view = new EditorView({ state, parent: options.parent });
  reportDirty(view.state);

  return {
    text: () => view.state.doc.toString(),
    setText: (text) =>
      view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: text } }),
    setBase: (text) => {
      baseDoc = view.state.toText(text);
      reportDirty(view.state);
    },
    setWrap: (value) => view.dispatch({ effects: wrap.reconfigure(wrapExtension(value)) }),
    setFontSize: (px) => view.dispatch({ effects: fontSize.reconfigure(fontSizeTheme(px)) }),
    focus: () => view.focus(),
    destroy: () => view.destroy(),
  };
}
