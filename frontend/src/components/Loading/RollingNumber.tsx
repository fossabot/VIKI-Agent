/**
 * Rolling digits. The frozen `src/accept/m3b/app.accept.test.tsx` finds the vault tiles with
 * `findByText`, which matches only a node's own direct text nodes -- so the number itself has to
 * stay one real text node, not split across per-character spans.
 *
 * `RollingNumber` renders a fragment: `value` as one plain text node, then an `aria-hidden`
 * `.roll` overlay built from one empty span per character (no DOM text of its own, so it adds
 * nothing to textContent): a digit is a `.roll-digit` whose `--d` custom property picks the row
 * RollingNumber.css's `::before` shows; any other character (",", "-", ...) is a `.roll-sep`
 * whose `::before` is `content: attr(data-c)`. The CSS positions `.roll` on top of the host
 * element embedding this component (VaultTiles' `.tile-n`, marked `roll-host`), so the overlay
 * paints the roll while the real text node underneath -- hidden, not removed -- keeps textContent
 * and the accessible name exactly `value`, and is what a plain-text query like `findByText` or
 * `.textContent` actually reads.
 *
 * Keys count from the right: a character's key is its distance from the string's end, not its
 * index from the start. A value that grows a new leading digit (9 -> 10) then only adds a new
 * key at the high end -- every existing digit keeps the key, and so the element, it already had,
 * which is what lets RollingNumber.css's transition actually roll instead of restarting on a
 * swap.
 */
import type { CSSProperties } from "react";
import "./RollingNumber.css";

export interface RollingNumberProps {
  value: number | string;
}

export function RollingNumber({ value }: RollingNumberProps) {
  const text = String(value);
  const chars = [...text];
  const lastIndex = chars.length - 1;
  return (
    <>
      {text}
      <span className="roll" aria-hidden="true">
        {chars.map((char, index) => {
          const key = lastIndex - index;
          if (char < "0" || char > "9") {
            return <span key={key} className="roll-sep" data-c={char} />;
          }
          return <span key={key} className="roll-digit" style={{ "--d": char } as CSSProperties} />;
        })}
      </span>
    </>
  );
}
