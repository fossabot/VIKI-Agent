/**
 * The `/` command menu (src/accept/m2c2/slashMenu.accept.test.tsx pins the contract).
 * One list: the vault's skills first under a "Vault skills" label, then the rest under
 * "Commands".
 *
 * Rules:
 * - It opens above the composer (`.menu`, absolute, the design's 8px radius and shadow) when the
 *   composer's text starts with `/`, `／` or `・` — the last two because a Japanese keyboard
 *   produces them where a slash was meant. It closes on Escape, on a pick, and as soon as the
 *   text no longer starts with one of those characters. (Composer.tsx owns that last part —
 *   this component just renders while it is mounted.)
 * - Items come from `commands` (the CLI's own list, minus terminal-only ones and /clear, which
 *   App filters). Order: `skill` true first, alphabetically, then the rest alphabetically.
 * - Filtering is on what follows the prefix, case-insensitively, matched anywhere in the name
 *   (the design highlights "re" inside "/deep-research"). The matched run always renders in `.hl`,
 *   one row or five, so the tail of every typed query keeps its orange match — the moment it is
 *   most useful. `.hl` splits the name into three nodes, so a test cannot locate its row with
 *   `getByText`, whose default matcher reads only an element's direct text children: the click
 *   test finds the row by its class instead.
 * - Each row (`.mi`) is 34px: the name in mono, fixed 150px, then its description, dim and
 *   truncated to one line. `argument_hint`, when present, follows the name in dim mono.
 * - The highlighted row is `.mi.on`. ArrowDown and ArrowUp move it and wrap; Enter and a click
 *   pick it; the first row starts highlighted so Enter picks the obvious one.
 * - This component owns those keys from a `window` listener in the CAPTURE phase — ahead of
 *   Composer's own React handler in the event order, so Composer never has to know the menu is
 *   open (see Composer.tsx's docstring) — but ONLY while the composer has focus (see
 *   focusIsElsewhere below). Window capture is the first step of the DOM's dispatch, so a
 *   `stopPropagation()` here ends the event for the entire app: every other keyboard-owning
 *   surface (SearchPalette, SessionLimitToast, HeaderMenu, SessionList's row menu)
 *   listens for Escape on `window` in the BUBBLE phase, and React delivers the palette's own
 *   arrows through its root container below `document`. This is the only capture-phase window
 *   listener in the app; anything added later is downstream of it and needs the same guard.
 *   It calls `preventDefault()` + `stopPropagation()` only
 *   for a key it actually consumes: Escape and the arrows always (there is always a highlighted
 *   row, or none, to move between), Enter only when a row is highlighted to pick. Left alone,
 *   the key reaches Composer's own handler unchanged — which is how Enter over zero matching
 *   rows still sends the user's own text (`・milk` with no match), Shift+Enter still writes
 *   a newline instead of picking a row, and the IME's own Enter still only closes its candidate
 *   window instead of picking one.
 * - Picking calls onPick(command), and the composer rewrites its prefix to `/<name> ` — that
 *   rewrite is the ONLY time the user's typing is changed. Typing a token that exactly matches
 *   a command name and then a space rewrites it the same way; anything else is sent unchanged
 *   (`・milk` stays `・milk`).
 * - Every name and description is a text node: they come from the CLI and from the vault's own
 *   skill files.
 */
import { type ReactElement, type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import type { Command } from "../../protocol";
import { isComposingKeydown } from "./ime";
import "./SlashMenu.css";

export interface SlashMenuProps {
  commands: readonly Command[];
  /** What the user typed after the prefix character. */
  query: string;
  onPick: (command: Command) => void;
  onClose: () => void;
}

/** Case-insensitive "matched anywhere in the name" filter, split skills-then-rest, each group
 * alphabetically. Returns both groups, not their concatenation: the
 * component needs the split itself for the "Vault skills" / "Commands" labels as well as the
 * flat row list, and computing it once here (rather than filtering the combined list back apart
 * afterwards) keeps the skills-then-rest ordering written in exactly one place. */
function orderedMatches(
  commands: readonly Command[],
  query: string,
): { skills: Command[]; rest: Command[] } {
  const needle = query.toLowerCase();
  const matches = commands.filter((c) => c.name.toLowerCase().includes(needle));
  const byName = (a: Command, b: Command) => a.name.localeCompare(b.name);
  return {
    skills: matches.filter((c) => c.skill).sort(byName),
    rest: matches.filter((c) => !c.skill).sort(byName),
  };
}

/** The name as shown in the row: `/`, then the matched run wrapped in `.hl`, always — the
 * design highlights it whether one row matches or five, and the tail of any typed query is
 * usually one row, which is exactly when the user most wants to see what they matched.
 *
 * Assumes ASCII: it finds the match on the lowercased strings but slices the *original* name with
 * those same indices, which only lines up because `toLowerCase()` never changes length or offset
 * for CLI command names and vault skill slugs (both ASCII today). A name or query containing a
 * character whose lowercase form has a different length (e.g. İ) would slice the wrong run. */
function renderName(name: string, query: string): ReactNode {
  if (!query) return `/${name}`;
  const idx = name.toLowerCase().indexOf(query.toLowerCase());
  if (idx === -1) return `/${name}`; // can't happen for a filtered row, but never crash on it
  const before = name.slice(0, idx);
  const match = name.slice(idx, idx + query.length);
  const after = name.slice(idx + query.length);
  return (
    <>
      /{before}
      <span className="hl">{match}</span>
      {after}
    </>
  );
}

/** True when something outside the composer holds focus, in which case none of these keys are
 * this menu's to take. The menu's open state is derived from the composer's TEXT alone, so a
 * half-typed `/wr` left in the box keeps it mounted while the user opens the search palette or
 * a close confirmation on top of it — and a capture-phase stopPropagation() at `window` would
 * then swallow that surface's Escape, arrows and Enter outright.
 *
 * Every such surface takes focus when it opens, or is opened by a click that does, so focus is
 * the honest test. `document.body` (and null) counts as the composer's: clicking the app's own
 * non-focusable chrome blurs there while the menu is legitimately still up, and no overlay in
 * this app leaves focus there — that is also the case the frozen accept test fires from, which
 * dispatches on a bare `window` with nothing focused. Only this menu and its message textarea
 * own command keys; other controls nested in `.composer-wrap` keep their own keyboard input. */
function focusIsElsewhere(menu: HTMLDivElement | null): boolean {
  const focused = document.activeElement;
  const message = menu?.closest(".composer-wrap")?.querySelector("textarea.composer-box");
  return (
    focused !== null && focused !== document.body && focused !== message && !menu?.contains(focused)
  );
}

export function SlashMenu({ commands, query, onPick, onClose }: SlashMenuProps): ReactElement {
  const menuRef = useRef<HTMLDivElement>(null);
  const { skills, rest } = useMemo(() => orderedMatches(commands, query), [commands, query]);
  const rows = useMemo(() => [...skills, ...rest], [skills, rest]);

  const [onIndex, setOnIndex] = useState(0);
  // A new query reshuffles which rows exist at all; the old index would point at the wrong row
  // (or past the end), so land back on the obvious first pick every time it changes.
  // biome-ignore lint/correctness/useExhaustiveDependencies: the reset is keyed on query changing, not on any value the body reads.
  useEffect(() => setOnIndex(0), [query]);

  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent): void {
      if (document.querySelector('[aria-modal="true"]')) return;
      // The IME's own Enter (closing a candidate window) is never this menu's to read — leave it
      // unconsumed for Composer, which already knows to ignore it too.
      if (isComposingKeydown(event)) return;
      if (focusIsElsewhere(menuRef.current)) return;
      if (event.key === "ArrowDown") {
        event.preventDefault();
        event.stopPropagation();
        if (rows.length > 0) setOnIndex((i) => (i + 1) % rows.length);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        event.stopPropagation();
        if (rows.length > 0) setOnIndex((i) => (i - 1 + rows.length) % rows.length);
      } else if (event.key === "Enter" && !event.shiftKey) {
        // Shift+Enter is the user writing a newline, not picking a row — leave it
        // unconsumed so it falls through to the textarea's own default handling.
        const picked = rows[onIndex];
        // No highlighted row (the query matched nothing): this Enter isn't the menu's to take —
        // leave it for Composer to send the user's text exactly as typed.
        if (!picked) return;
        event.preventDefault();
        event.stopPropagation();
        onPick(picked);
      } else if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        onClose();
      }
    }
    // CAPTURE phase: runs before Composer's own React keydown handler in the event order (see
    // this file's and Composer.tsx's docstrings). stopPropagation() on a key this menu consumes
    // then keeps it from ever reaching Composer, rather than relying on Composer to recognise the
    // menu is open and step aside on its own.
    window.addEventListener("keydown", handleKeyDown, true);
    return () => window.removeEventListener("keydown", handleKeyDown, true);
  }, [rows, onIndex, onPick, onClose]);

  function row(command: Command, index: number): ReactElement {
    // A real <button>, not a div with an onClick, so a mouse and a keyboard both reach it — the
    // window-level Enter/arrow handling above is this menu's actual keyboard model, so the
    // button itself opts out of the normal tab order rather than adding a second, competing one.
    return (
      <button
        type="button"
        key={command.name}
        tabIndex={-1}
        className={`mi${index === onIndex ? " on" : ""}`}
        onClick={() => onPick(command)}
      >
        <span className="mono mi-name">{renderName(command.name, query)}</span>
        {command.argument_hint && <span className="mono mi-hint">{command.argument_hint}</span>}
        <span className="d">{command.description}</span>
      </button>
    );
  }

  return (
    <div ref={menuRef} className="menu menu-panel">
      {rows.length === 0 ? (
        <div className="menu-note">No matching commands</div>
      ) : (
        <>
          {skills.length > 0 && <div className="lbl">Vault skills</div>}
          {skills.map((c, i) => row(c, i))}
          {rest.length > 0 && (
            <div className={`lbl${skills.length > 0 ? " lbl-rule" : ""}`}>Commands</div>
          )}
          {rest.map((c, i) => row(c, skills.length + i))}
          <div className="menu-hints jp">
            <span>
              <span className="kbd">↑↓</span> Choose
            </span>
            <span>
              <span className="kbd">Enter</span> Select
            </span>
            <span>
              <span className="kbd">Esc</span> Close
            </span>
            <span className="menu-hints-count">
              {commands.length} commands · {commands.filter((c) => c.skill).length} vault skills
            </span>
          </div>
        </>
      )}
    </div>
  );
}
